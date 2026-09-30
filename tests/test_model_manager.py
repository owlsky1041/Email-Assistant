"""嵌入模型管理测试：状态检查、导入、zip 安全、下载原子性。"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from src.model_manager import (
    ModelError,
    check_model_dir,
    import_model,
    resolve_endpoint,
)


def make_model_dir(root: Path, *, size: int = 8 * 1024 * 1024) -> Path:
    """造一个"看起来像真模型"的目录（model.onnx 需 > 5MB 才不被判为外部数据）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "model.onnx").write_bytes(b"\x00" * size)
    (root / "tokenizer.json").write_text('{"version":"1.0"}', encoding="utf-8")
    (root / "config.json").write_text("{}", encoding="utf-8")
    return root


class TestCheckModelDir:
    def test_ready_directory(self, tmp_path: Path) -> None:
        make_model_dir(tmp_path / "m")
        status = check_model_dir(tmp_path / "m")
        assert status.ready is True
        assert "model.onnx" in status.files
        assert status.size_bytes > 0
        assert "就绪" in status.note

    def test_missing_directory(self, tmp_path: Path) -> None:
        status = check_model_dir(tmp_path / "absent")
        assert status.ready is False
        assert "不存在" in status.note

    def test_missing_required_files(self, tmp_path: Path) -> None:
        (tmp_path / "m").mkdir()
        (tmp_path / "m" / "model.onnx").write_bytes(b"x" * 100)
        status = check_model_dir(tmp_path / "m")
        assert status.ready is False
        assert "tokenizer.json" in status.missing

    def test_detects_external_data(self, tmp_path: Path) -> None:
        """外部权重导出必须被识别：只拷 model.onnx 会在推理时才炸。"""
        d = tmp_path / "m"
        make_model_dir(d)
        (d / "model.onnx_data").write_bytes(b"\x00" * 1024)
        status = check_model_dir(d)
        assert "外部权重" in status.note

    def test_tiny_onnx_flagged_as_suspicious(self, tmp_path: Path) -> None:
        d = tmp_path / "m"
        d.mkdir()
        (d / "model.onnx").write_bytes(b"x" * 1024)  # 1KB，不可能装下真实权重
        (d / "tokenizer.json").write_text("{}", encoding="utf-8")
        status = check_model_dir(d)
        assert status.ready is False
        assert "外部权重" in status.note

    def test_to_dict_shape(self, tmp_path: Path) -> None:
        make_model_dir(tmp_path / "m")
        payload = check_model_dir(tmp_path / "m").to_dict()
        for key in ("path", "ready", "files", "missing", "size_mb", "note"):
            assert key in payload


class TestImportModel:
    def test_import_from_directory(self, tmp_path: Path) -> None:
        source = make_model_dir(tmp_path / "src")
        status = import_model(source, tmp_path / "dst")
        assert status.ready is True
        assert (tmp_path / "dst" / "model.onnx").is_file()
        assert (tmp_path / "dst" / "tokenizer.json").is_file()

    def test_import_skips_existing_without_force(self, tmp_path: Path) -> None:
        source = make_model_dir(tmp_path / "src", size=6 * 1024 * 1024)
        dst = tmp_path / "dst"
        import_model(source, dst)
        marker = dst / "model.onnx"
        mtime = marker.stat().st_mtime

        # 改小源文件，不带 force 时不应覆盖
        (source / "model.onnx").write_bytes(b"\x00" * (7 * 1024 * 1024))
        import_model(source, dst)
        assert marker.stat().st_mtime == mtime

        import_model(source, dst, overwrite=True)
        assert marker.stat().st_size == 7 * 1024 * 1024

    def test_import_from_zip(self, tmp_path: Path) -> None:
        archive = tmp_path / "model.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("model.onnx", b"\x00" * (6 * 1024 * 1024))
            zf.writestr("tokenizer.json", "{}")
        status = import_model(archive, tmp_path / "dst")
        assert status.ready is True

    def test_import_from_zip_with_nested_dir(self, tmp_path: Path) -> None:
        """压缩包多套一层目录时应自动摊平。"""
        archive = tmp_path / "model.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("bge-small-zh-v1.5/model.onnx", b"\x00" * (6 * 1024 * 1024))
            zf.writestr("bge-small-zh-v1.5/tokenizer.json", "{}")
        status = import_model(archive, tmp_path / "dst")
        assert status.ready is True
        assert (tmp_path / "dst" / "model.onnx").is_file()

    def test_import_rejects_missing_source(self, tmp_path: Path) -> None:
        with pytest.raises(ModelError, match="不存在"):
            import_model(tmp_path / "absent", tmp_path / "dst")

    def test_import_rejects_unsupported_file(self, tmp_path: Path) -> None:
        bad = tmp_path / "model.tar"
        bad.write_bytes(b"x")
        with pytest.raises(ModelError, match="不支持"):
            import_model(bad, tmp_path / "dst")

    def test_zip_slip_blocked(self, tmp_path: Path) -> None:
        """回归：恶意 zip 不能写出目标目录之外。"""
        archive = tmp_path / "evil.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("../../escaped.txt", "pwned")
        with pytest.raises(ModelError, match="非法路径"):
            import_model(archive, tmp_path / "dst")
        assert not (tmp_path.parent / "escaped.txt").exists()

    def test_import_ignores_unrelated_files(self, tmp_path: Path) -> None:
        source = make_model_dir(tmp_path / "src")
        (source / "README.md").write_text("hi", encoding="utf-8")
        (source / "pytorch_model.bin").write_bytes(b"\x00" * 100)
        import_model(source, tmp_path / "dst")
        assert not (tmp_path / "dst" / "pytorch_model.bin").exists()


class TestResolveEndpoint:
    def test_explicit_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_ENDPOINT", "https://env.example")
        assert resolve_endpoint("https://explicit.example") == "https://explicit.example"

    def test_env_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.com/")
        assert resolve_endpoint(None) == "https://hf-mirror.com"

    def test_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HF_ENDPOINT", raising=False)
        assert resolve_endpoint(None) == "https://huggingface.co"


class TestDownloadModel:
    def test_requires_url_or_repo(self, tmp_path: Path) -> None:
        from src.model_manager import download_model

        with pytest.raises(ModelError, match="--url 或 --repo"):
            download_model(tmp_path / "dst")

    def test_download_zip_from_url(self, tmp_path: Path) -> None:
        """用 file:// 起一个真实下载流程，验证解压与原子写入。"""
        from src.model_manager import download_model

        archive = tmp_path / "bundle.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("model.onnx", b"\x00" * (6 * 1024 * 1024))
            zf.writestr("tokenizer.json", "{}")

        status = download_model(tmp_path / "dst", url=archive.as_uri())
        assert status.ready is True
        assert not list((tmp_path / "dst").glob("*.tmp"))
        assert not list((tmp_path / "dst").glob("*.zip"))

    def test_failed_download_leaves_no_tmp(self, tmp_path: Path) -> None:
        """回归：下载失败必须清理 .tmp，不能留下半个文件。"""
        from src.model_manager import download_model

        with pytest.raises(ModelError):
            download_model(tmp_path / "dst", url=(tmp_path / "absent.zip").as_uri())
        dst = tmp_path / "dst"
        if dst.exists():
            assert list(dst.glob("*.tmp")) == []
