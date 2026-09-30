"""工具函数测试：文件名安全化、原子写入、Token 估算。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src.utils import (
    ChunkedFileWriter,
    atomic_write_bytes,
    atomic_write_text,
    dedupe_path,
    estimate_tokens,
    human_size,
    sanitize_filename,
    sanitize_relative_path,
    sha256_bytes,
)


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        "raw",
        [
            'a<b>c:d"e/f\\g|h?i*j',
            "trailing dots...",
            "  leading spaces  ",
            "tab\tand\nnewline",
        ],
    )
    def test_removes_illegal_characters(self, raw: str) -> None:
        result = sanitize_filename(raw)
        assert not any(ch in result for ch in '<>:"/\\|?*')
        assert not result.endswith(".")
        assert not result.startswith(" ")

    def test_windows_reserved_names(self) -> None:
        for name in ("CON", "PRN", "AUX", "NUL", "COM1", "LPT9"):
            assert sanitize_filename(name) != name
        # 带扩展名的保留名同样需要处理
        assert sanitize_filename("CON.txt").upper() != "CON.TXT"

    def test_cjk_truncated_by_utf8_bytes(self) -> None:
        """中文标题按 UTF-8 字节截断，不能出现半个字符。"""
        title = "非常长的中文邮件主题" * 40
        result = sanitize_filename(title, max_bytes=60)
        assert len(result.encode("utf-8")) <= 60
        result.encode("utf-8").decode("utf-8")  # 不应抛异常

    def test_empty_falls_back(self) -> None:
        assert sanitize_filename("") == "untitled"
        assert sanitize_filename("...") == "untitled"
        assert sanitize_filename("   ", fallback="xx") == "xx"

    def test_preserves_normal_names(self) -> None:
        assert sanitize_filename("季度财报Q1.xlsx") == "季度财报Q1.xlsx"


class TestSanitizeRelativePath:
    def test_blocks_traversal(self) -> None:
        result = sanitize_relative_path(["..", "..", "etc", "passwd"])
        assert ".." not in result.parts
        assert "etc" in result.parts

    def test_handles_delimiters(self) -> None:
        result = sanitize_relative_path(["客户/2024/发票"])
        assert result == Path("客户") / "2024" / "发票"

    def test_empty_returns_inbox(self) -> None:
        assert sanitize_relative_path([]) == Path("INBOX")
        assert sanitize_relative_path([".", ".."]) == Path("INBOX")


class TestAtomicWrite:
    def test_writes_and_replaces(self, tmp_path: Path) -> None:
        target = tmp_path / "sub" / "file.md"
        atomic_write_text(target, "第一次")
        assert target.read_text(encoding="utf-8") == "第一次"
        atomic_write_text(target, "第二次")
        assert target.read_text(encoding="utf-8") == "第二次"

    def test_no_tmp_left_behind(self, tmp_path: Path) -> None:
        target = tmp_path / "file.bin"
        atomic_write_bytes(target, b"payload")
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_permissions_on_posix(self, tmp_path: Path) -> None:
        if os.name == "nt":
            pytest.skip("POSIX 专用")
        target = tmp_path / "secret.txt"
        atomic_write_text(target, "secret")
        assert oct(target.stat().st_mode)[-3:] == "600"


class TestChunkedFileWriter:
    def test_commit_success(self, tmp_path: Path) -> None:
        target = tmp_path / "att.bin"
        payload = b"0123456789" * 100
        with ChunkedFileWriter(target, expected_size=len(payload), chunk_size=64) as writer:
            for i in range(0, len(payload), 64):
                writer.write(payload[i : i + 64])
            digest = writer.sha256
        assert target.read_bytes() == payload
        assert digest == sha256_bytes(payload)
        assert not list(tmp_path.glob("*.tmp"))

    def test_size_mismatch_removes_tmp(self, tmp_path: Path) -> None:
        """§11.2 临时文件机制：大小校验失败必须删除半成品。"""
        target = tmp_path / "att.bin"
        with pytest.raises(OSError, match="大小校验失败"):
            with ChunkedFileWriter(target, expected_size=999) as writer:
                writer.write(b"short")
        assert not target.exists()
        assert not list(tmp_path.glob("*.tmp"))

    def test_exception_aborts_cleanly(self, tmp_path: Path) -> None:
        target = tmp_path / "att.bin"
        with pytest.raises(RuntimeError):
            with ChunkedFileWriter(target, expected_size=10) as writer:
                writer.write(b"12345")
                raise RuntimeError("中断")
        assert not target.exists()
        assert not list(tmp_path.glob("*.tmp"))

    def test_final_file_is_complete(self, tmp_path: Path) -> None:
        """只有完整写入后目标文件才出现（不会看到半成品）。"""
        target = tmp_path / "att.bin"
        seen_during: list[bool] = []
        with ChunkedFileWriter(target, expected_size=6) as writer:
            writer.write(b"abc")
            seen_during.append(target.exists())
            writer.write(b"def")
        assert seen_during == [False]
        assert target.read_bytes() == b"abcdef"


class TestDedupePath:
    def test_returns_same_when_free(self, tmp_path: Path) -> None:
        target = tmp_path / "a.md"
        assert dedupe_path(target) == target

    def test_appends_counter(self, tmp_path: Path) -> None:
        target = tmp_path / "a.md"
        target.write_text("x", encoding="utf-8")
        assert dedupe_path(target).name == "a_1.md"
        (tmp_path / "a_1.md").write_text("y", encoding="utf-8")
        assert dedupe_path(target).name == "a_2.md"


class TestEstimateTokens:
    def test_empty(self) -> None:
        assert estimate_tokens("") == 0

    def test_chinese_is_roughly_one_per_char(self) -> None:
        text = "这是一段中文测试文本"
        assert 8 <= estimate_tokens(text) <= 16

    def test_english_words(self) -> None:
        assert estimate_tokens("hello world example") >= 3

    def test_monotonic(self) -> None:
        short = estimate_tokens("短文本")
        long = estimate_tokens("短文本" * 50)
        assert long > short

    def test_mixed_content(self) -> None:
        assert estimate_tokens("邮件email123测试") > 0


class TestHumanSize:
    @pytest.mark.parametrize(
        "value,expected",
        [(0, "0B"), (512, "512B"), (2048, "2.0KB"), (5 * 1024**2, "5.0MB")],
    )
    def test_formats(self, value: int, expected: str) -> None:
        assert human_size(value) == expected
