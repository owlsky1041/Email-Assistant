"""内容寻址附件仓库（blob store）的测试。

设计前提是**只增不删**：blob 是派生数据，归档目录里已经有全部内容。
所以补建操作中途失败最多是少建几个 blob，绝不会损坏已有归档 ——
这一点必须被测试锁住。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from src.blob_maintenance import migrate_blobs, verify_blobs
from src.blob_store import BlobStore, normalize_digest, sha256_file
from src.config import AppConfig
from src.context import AppContext
from src.models import AttachmentMeta, ParsedMessage

PAYLOAD = b"%PDF-1.4 " + b"blob-test-content" * 100


class TestDigestValidation:
    def test_rejects_non_hex(self) -> None:
        with pytest.raises(ValueError):
            normalize_digest("zz" * 32)

    def test_rejects_wrong_length(self) -> None:
        with pytest.raises(ValueError):
            normalize_digest("abc")

    def test_lowercases(self) -> None:
        assert normalize_digest("A" * 64) == "a" * 64

    def test_path_traversal_is_impossible(self) -> None:
        """哈希被当作路径片段，必须挡死 ../ 之类的输入。"""
        for bad in ("../" * 10, "..", "/etc/passwd", "a" * 63 + "/"):
            with pytest.raises(ValueError):
                normalize_digest(bad)


class TestSharding:
    def test_path_is_sharded(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path)
        digest = "ab" + "cd" + "e" * 60
        path = store.path_for(digest)
        assert path == tmp_path / "ab" / "cd" / digest

    def test_put_is_idempotent(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path)
        first = store.put_bytes(PAYLOAD)
        inode = first.stat().st_ino
        second = store.put_bytes(PAYLOAD)
        assert first == second
        assert second.stat().st_ino == inode, "重复写入不应产生新 inode"

    def test_concurrent_put_yields_one_file(self, tmp_path: Path) -> None:
        """并发写同一份内容不能互相破坏。"""
        store = BlobStore(tmp_path)
        results: list[Path] = []
        barrier = threading.Barrier(4)

        def worker() -> None:
            barrier.wait(timeout=5)
            results.append(store.put_bytes(PAYLOAD))

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(set(results)) == 1
        assert results[0].read_bytes() == PAYLOAD
        assert not list(tmp_path.rglob("*.tmp")), "临时文件必须被清理"

    def test_no_stray_tmp_left(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path)
        store.put_bytes(PAYLOAD)
        assert not list(tmp_path.rglob("*.tmp"))

    def test_concurrent_put_never_replaces_existing_blob(self, tmp_path: Path) -> None:
        """回归：并发写同一份内容时不能覆盖别人已经落位的 blob。

        早先 put_bytes 无条件 os.replace：两个线程各写各的临时文件，后到的
        那个会把先到的 blob 换掉。于是已经硬链接到旧 blob 的附件与后来的
        附件指向**不同 inode** —— 去重静默失效、磁盘占用翻倍，而且只在
        并发下偶发。
        """
        store = BlobStore(tmp_path)
        digest = _digest_of(PAYLOAD)
        linked: list[Path] = []
        barrier = threading.Barrier(6)
        lock = threading.Lock()

        def worker(index: int) -> None:
            barrier.wait(timeout=5)
            store.put_bytes(PAYLOAD, digest=digest)
            # 模拟"写完之后立刻硬链接到人工目录"
            target = tmp_path / f"copy-{index}.bin"
            path = store.link_into(digest, target)
            assert path is not None
            with lock:
                linked.append(path)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(linked) == 6
        inodes = {p.stat().st_ino for p in linked}
        assert len(inodes) == 1, (
            f"并发写入后有 {len(inodes)} 个 inode，说明 blob 被覆盖过"
        )
        assert all(p.read_bytes() == PAYLOAD for p in linked)


class TestVerify:
    def test_detects_corruption(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path)
        path = store.put_bytes(PAYLOAD)
        assert store.verify(store.path_for.__self__ and sha256_file(path))
        path.write_bytes(b"tampered")
        assert not store.verify(sha256_file(Path("__missing__")) if False else _digest_of(PAYLOAD))

    def test_missing_blob_fails_verify(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path)
        assert not store.verify("0" * 64)

    def test_deep_false_only_checks_existence(self, tmp_path: Path) -> None:
        store = BlobStore(tmp_path)
        digest = _digest_of(PAYLOAD)
        path = store.put_bytes(PAYLOAD)
        path.write_bytes(b"tampered")
        assert store.verify(digest, deep=False)
        assert not store.verify(digest, deep=True)


def _digest_of(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


class TestExporterUsesBlob:
    def _message(self, uid: str, name: str, payload: bytes) -> ParsedMessage:
        message = ParsedMessage(
            uid=uid, folder="INBOX", subject=f"第 {uid} 封",
            body_markdown="正文", body_text="正文",
        )
        message.attachments = [
            AttachmentMeta(filename=name, part_index=0, size_bytes=len(payload))
        ]
        message.attachment_payloads = {0: payload}
        return message

    def test_attachment_is_hardlink_to_blob(self, context: AppContext) -> None:
        """人工目录里的附件必须是 blob 的硬链接，而不是另写一份。"""
        result = context.sync.exporter.export(
            self._message("1", "技术附件.pdf", PAYLOAD), account="a@x.com"
        )
        saved = result.attachments[0]
        blob = context.sync.blob_store.path_for(_digest_of(PAYLOAD))

        assert blob.is_file()
        assert Path(saved.local_path).is_file()
        if _hardlink_supported(context.sync.blob_store.root):
            assert blob.stat().st_ino == Path(saved.local_path).stat().st_ino

    def test_duplicate_content_shares_single_inode(self, context: AppContext) -> None:
        paths = []
        for i, name in enumerate(("a.pdf", "b.pdf", "c.pdf"), start=1):
            result = context.sync.exporter.export(
                self._message(str(i), name, PAYLOAD), account="a@x.com"
            )
            paths.append(Path(result.attachments[0].local_path))

        if not _hardlink_supported(context.sync.blob_store.root):
            pytest.skip("当前文件系统不支持硬链接")
        inodes = {p.stat().st_ino for p in paths}
        assert len(inodes) == 1, f"三份相同内容占了 {len(inodes)} 个 inode"

    def test_different_content_separate_blobs(self, context: AppContext) -> None:
        for i, payload in enumerate((PAYLOAD, PAYLOAD + b"x"), start=1):
            context.sync.exporter.export(
                self._message(str(i), f"f{i}.bin", payload), account="a@x.com"
            )
        blobs = list(context.sync.blob_store.iter_blobs())
        assert len(blobs) == 2

    def test_direct_write_still_works_without_blob_store(
        self, context: AppContext
    ) -> None:
        """没有 blob 仓库时（独立使用 exporter）也必须照常落盘。"""
        from src.markdown_exporter import MarkdownExporter

        exporter = MarkdownExporter(context.config)
        result = exporter.export(
            self._message("9", "solo.bin", PAYLOAD), account="a@x.com"
        )
        saved = result.attachments[0]
        assert saved.downloaded
        assert Path(saved.local_path).read_bytes() == PAYLOAD


def _hardlink_supported(directory: Path) -> bool:
    directory.mkdir(parents=True, exist_ok=True)
    probe = directory / ".probe-link"
    other = directory / ".probe-link2"
    try:
        probe.write_bytes(b"x")
        os.link(probe, other)
        return True
    except OSError:
        return False
    finally:
        for p in (probe, other):
            p.unlink(missing_ok=True)


class TestMaintenance:
    def _seed_legacy_attachment(self, context: AppContext) -> Path:
        """造一条"blob 里还没有"的历史附件记录。"""
        result = context.sync.exporter.export(
            ParsedMessage(
                uid="1", folder="INBOX", subject="历史", body_markdown="b", body_text="b",
            ),
            account="a@x.com",
        )
        # 直接用另一个 exporter（无 blob）写出，模拟迁移前的归档
        from src.markdown_exporter import MarkdownExporter
        from src.utils import sha256_bytes

        legacy = MarkdownExporter(context.config)
        message = ParsedMessage(
            uid="2", folder="INBOX", subject="历史2", body_markdown="b", body_text="b",
        )
        message.attachments = [
            AttachmentMeta(filename="legacy.pdf", part_index=0, size_bytes=len(PAYLOAD))
        ]
        message.attachment_payloads = {0: PAYLOAD}
        out = legacy.export(message, account="a@x.com")
        saved = out.attachments[0]
        record = context.sync._to_record(message, out, duplicate_of=None)
        context.db.insert_message(record, out.attachments)
        assert result is not None
        return Path(saved.local_path)

    def test_migrate_builds_blobs_without_touching_files(
        self, context: AppContext
    ) -> None:
        path = self._seed_legacy_attachment(context)
        before = path.read_bytes()
        inode_before = path.stat().st_ino

        # 先把已有的 blob 清掉，制造"需要迁移"的状态
        import shutil

        shutil.rmtree(context.config.blob_path, ignore_errors=True)

        stats = migrate_blobs(context.config, context.db)

        assert stats.scanned >= 1
        assert stats.adopted >= 1
        assert not stats.missing
        # 原文件必须毫发无损
        assert path.read_bytes() == before
        if _hardlink_supported(context.config.blob_path):
            assert path.stat().st_ino == inode_before, "迁移不该替换原文件"

    def test_migrate_is_idempotent(self, context: AppContext) -> None:
        self._seed_legacy_attachment(context)
        first = migrate_blobs(context.config, context.db)
        second = migrate_blobs(context.config, context.db)
        assert second.adopted == 0
        assert second.already == first.scanned

    def test_migrate_reports_missing_file(self, context: AppContext) -> None:
        path = self._seed_legacy_attachment(context)
        path.unlink()
        stats = migrate_blobs(context.config, context.db)
        assert stats.missing >= 1
        assert any("文件缺失" in e for e in stats.errors)

    def test_verify_passes_on_healthy_archive(self, context: AppContext) -> None:
        self._seed_legacy_attachment(context)
        migrate_blobs(context.config, context.db)
        stats = verify_blobs(context.config, context.db)
        assert stats.healthy, stats.describe()
        assert stats.missing_blob == 0

    def test_unmigrated_archive_is_healthy_but_flagged(self, context: AppContext) -> None:
        """还没迁移进 blob 仓库 ≠ 数据损坏，不能报红。"""
        self._seed_legacy_attachment(context)
        import shutil

        shutil.rmtree(context.config.blob_path, ignore_errors=True)
        stats = verify_blobs(context.config, context.db)
        assert stats.healthy, "文件都在、内容也对，就是健康"
        assert stats.missing_blob >= 1, "但要提示还需要 migrate-blobs"

    def test_verify_detects_deleted_attachment(self, context: AppContext) -> None:
        path = self._seed_legacy_attachment(context)
        migrate_blobs(context.config, context.db)
        path.unlink()
        stats = verify_blobs(context.config, context.db, deep=False)
        assert not stats.healthy
        assert stats.missing_file >= 1

    def test_verify_detects_corrupted_content(self, context: AppContext) -> None:
        path = self._seed_legacy_attachment(context)
        migrate_blobs(context.config, context.db)
        path.write_bytes(b"tampered content")
        stats = verify_blobs(context.config, context.db, deep=True)
        assert stats.corrupted >= 1

    def test_repair_restores_deleted_attachment(self, context: AppContext) -> None:
        """归档文件被误删时，能从 blob 重建。"""
        path = self._seed_legacy_attachment(context)
        expected = path.read_bytes()
        migrate_blobs(context.config, context.db)
        path.unlink()

        stats = verify_blobs(context.config, context.db, deep=True, repair=True)

        assert stats.repaired >= 1
        assert path.is_file()
        assert path.read_bytes() == expected
        assert stats.healthy, stats.describe()

    def test_relink_merges_duplicate_inodes(self, context: AppContext) -> None:
        """迁移后发现"内容相同但各占一份 inode"的，--relink 应合并掉。"""
        # 用无 blob 的 exporter 写出两份同内容附件，模拟迁移前就存在的重复
        from src.markdown_exporter import MarkdownExporter

        paths = []
        for uid, name in (("1", "dup-a.pdf"), ("2", "dup-b.pdf")):
            # 每条都用一个**全新的** exporter：同一个实例的内存索引会把
            # 第二份直接硬链接过去，那就模拟不出"迁移前"的独立重复了
            legacy = MarkdownExporter(context.config)
            message = ParsedMessage(
                uid=uid, folder="INBOX", subject=f"s{uid}",
                body_markdown="b", body_text="b",
            )
            message.attachments = [
                AttachmentMeta(filename=name, part_index=0, size_bytes=len(PAYLOAD))
            ]
            message.attachment_payloads = {0: PAYLOAD}
            out = legacy.export(message, account="a@x.com")
            record = context.sync._to_record(message, out, duplicate_of=None)
            context.db.insert_message(record, out.attachments)
            paths.append(Path(out.attachments[0].local_path))

        if not _hardlink_supported(context.config.blob_path):
            pytest.skip("当前文件系统不支持硬链接")
        assert paths[0].stat().st_ino != paths[1].stat().st_ino, "前提：两份是独立 inode"

        first = migrate_blobs(context.config, context.db)
        assert first.relinkable >= 1
        assert first.relinked == 0, "默认不该改动已有文件"

        second = migrate_blobs(context.config, context.db, relink=True)
        assert second.relinked >= 1
        assert paths[0].stat().st_ino == paths[1].stat().st_ino, "应合并为同一条链接"
        assert paths[0].read_bytes() == PAYLOAD

    def test_relink_keeps_content_intact(self, context: AppContext) -> None:
        path = self._seed_legacy_attachment(context)
        migrate_blobs(context.config, context.db, relink=True)
        assert path.is_file()
        assert path.read_bytes() == PAYLOAD

    def test_verify_limit(self, context: AppContext) -> None:
        self._seed_legacy_attachment(context)
        migrate_blobs(context.config, context.db)
        stats = verify_blobs(context.config, context.db, limit=1)
        assert stats.checked == 1
