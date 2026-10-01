"""设置界面（原生窗口）的逻辑层测试。

窗口本身需要显示器，但**所有会写数据的逻辑**都在 ``settings_model`` 里，
因此这里覆盖的是真正要紧的部分：路径派生、校验、保存、密钥隔离。

回归背景
--------
托盘「设置…」和 ``main.py settings`` 以前都会把用户丢到浏览器里填表。
改成原生窗口后，落盘路径必须和以前完全一致，否则老用户一保存就会
把数据目录搬走。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import AppConfig, load_config
from src.gui.settings_model import (
    DEFAULT_DATA_ROOT,
    SettingsDraft,
    _explain_error,
    detect_data_root,
    layout_from_root,
    portable_path,
    save_draft,
)


class TestLayout:
    def test_derives_all_paths_from_single_root(self) -> None:
        paths = layout_from_root("/srv/mail")
        assert paths["archive_dir"] == "/srv/mail/mail_archive"
        assert paths["attachment_dir"] == "/srv/mail/attachments"
        assert paths["sqlite_path"] == "/srv/mail/sqlite/mail.db"
        assert paths["chroma_dir"] == "/srv/mail/chromadb"
        assert paths["backup_dir"] == "/srv/mail/backups"
        assert paths["model_dir"] == "/srv/mail/models"

    def test_relative_root_stays_relative(self) -> None:
        paths = layout_from_root("./data")
        assert not Path(paths["archive_dir"]).is_absolute()

    def test_does_not_relocate_logs(self) -> None:
        """日志不是用户要定位的数据，不能顺手搬走。"""
        assert "log_dir" not in layout_from_root("./data")

    def test_portable_path_round_trips_under_project(self, tmp_path: Path) -> None:
        base = AppConfig.resolve(".")
        assert portable_path(base / "data") == "./data"

    def test_portable_path_keeps_outside_paths_absolute(self) -> None:
        assert portable_path("/srv/elsewhere") == "/srv/elsewhere"


class TestDetectRoot:
    def test_default_config_is_consistent(self, tmp_config: AppConfig) -> None:
        # tmp_config 的路径是散开的，应被识别为"非标准布局"
        _, mismatches = detect_data_root(tmp_config)
        assert mismatches, "散开的路径必须报告为不一致"

    def test_standard_layout_has_no_mismatch(self, tmp_path: Path) -> None:
        root = tmp_path / "data"
        cfg = AppConfig()
        cfg.storage.archive_dir = str(root / "mail_archive")
        cfg.storage.attachment_dir = str(root / "attachments")
        cfg.storage.sqlite_path = str(root / "sqlite" / "mail.db")
        cfg.storage.chroma_dir = str(root / "chromadb")
        cfg.storage.backup_dir = str(root / "backups")
        cfg.embedding.model_dir = str(root / "models")
        _, mismatches = detect_data_root(cfg)
        assert mismatches == []

    def test_default_config_reports_default_root(self) -> None:
        root, mismatches = detect_data_root(AppConfig())
        assert root == DEFAULT_DATA_ROOT
        # 全新配置不该吓唬用户
        assert mismatches == []


class TestDraft:
    def test_from_config_copies_fields(self, tmp_config: AppConfig) -> None:
        draft = SettingsDraft.from_config(tmp_config)
        assert draft.address == "tester@corp.com"
        assert draft.imap_port == 993
        assert draft.fetch_workers == tmp_config.sync.fetch_workers

    def test_auth_code_never_prefilled(self, tmp_config: AppConfig) -> None:
        draft = SettingsDraft.from_config(tmp_config, auth_code_present=True)
        assert draft.auth_code == ""
        assert draft.auth_code_present is True

    def test_validate_rejects_bad_address(self) -> None:
        draft = SettingsDraft(address="not-an-address")
        assert any("邮箱账号格式" in e for e in draft.validate())

    def test_validate_rejects_bad_port(self) -> None:
        draft = SettingsDraft(address="a@b.com", imap_port=99999)
        assert any("端口" in e for e in draft.validate())

    def test_validate_requires_auth_code_only_when_asked(self) -> None:
        draft = SettingsDraft(address="a@b.com", auth_code="")
        assert draft.validate(require_auth_code=False) == []
        assert any("授权码" in e for e in draft.validate(require_auth_code=True))

    def test_existing_auth_code_satisfies_requirement(self) -> None:
        draft = SettingsDraft(address="a@b.com", auth_code="", auth_code_present=True)
        assert draft.validate(require_auth_code=True) == []

    def test_validate_rejects_workers_out_of_range(self) -> None:
        draft = SettingsDraft(address="a@b.com", fetch_workers=99)
        assert any("并发" in e for e in draft.validate())

    def test_validate_rejects_zero_attachment_limit(self) -> None:
        draft = SettingsDraft(address="a@b.com", max_attachment_size_mb=0)
        assert any("附件大小" in e for e in draft.validate())

    def test_patch_carries_onnx_repo(self) -> None:
        """模型仓库必须能存下来，否则用户选了仓库重启就丢。"""
        draft = SettingsDraft(model_repo="owner/repo", model_endpoint="https://mirror")
        patch = draft.to_patch()
        assert patch["embedding"]["onnx_repo"] == "owner/repo"
        assert patch["embedding"]["onnx_endpoint"] == "https://mirror"

    def test_patch_does_not_touch_log_dir(self) -> None:
        assert "log" not in SettingsDraft().to_patch()


class TestSave:
    def test_writes_config_and_keeps_auth_code_out_of_yaml(
        self, tmp_config: AppConfig
    ) -> None:
        draft = SettingsDraft.from_config(tmp_config)
        draft.address = "changed@corp.com"
        draft.auth_code = "super-secret-code"

        result = save_draft(draft, tmp_config, require_auth_code=True)

        text = Path(result.config_path).read_text(encoding="utf-8")
        assert "changed@corp.com" in text
        assert "super-secret-code" not in text, "授权码绝不能落进 YAML"
        assert result.secret_backend

        reloaded = load_config(result.config_path)
        assert reloaded.email.address == "changed@corp.com"

        from src.secret_store import SecretStore

        assert SecretStore(reloaded).get(reloaded.email.auth_code_ref) == "super-secret-code"

    def test_creates_data_directories(self, tmp_config: AppConfig, tmp_path: Path) -> None:
        draft = SettingsDraft.from_config(tmp_config)
        root = tmp_path / "fresh"
        draft.data_root = str(root)
        save_draft(draft, tmp_config)

        for sub in ("mail_archive", "attachments", "backups", "models", "sqlite"):
            assert (root / sub).is_dir(), f"缺少目录 {sub}"

    def test_blank_auth_code_leaves_stored_one_alone(self, tmp_config: AppConfig) -> None:
        from src.secret_store import SecretStore

        store = SecretStore(tmp_config)
        store.set(tmp_config.email.auth_code_ref, "original-code")

        draft = SettingsDraft.from_config(tmp_config, auth_code_present=True)
        save_draft(draft, tmp_config)  # auth_code 留空

        assert store.get(tmp_config.email.auth_code_ref) == "original-code"

    def test_validation_errors_block_save(self, tmp_config: AppConfig) -> None:
        draft = SettingsDraft.from_config(tmp_config)
        draft.address = "broken"
        with pytest.raises(ValueError, match="邮箱账号"):
            save_draft(draft, tmp_config)

    def test_warns_when_normalising_custom_layout(self, tmp_config: AppConfig) -> None:
        draft = SettingsDraft.from_config(tmp_config)
        result = save_draft(draft, tmp_config)
        assert any("标准布局" in w for w in result.warnings)


class TestSubprocessCommand:
    """托盘/设置 API 走的是「派生子进程开窗口」，命令行必须拼对。"""

    def test_global_config_flag_precedes_subcommand(self) -> None:
        """回归：--config 是全局选项，放在子命令后面会被 argparse 直接拒掉。

        症状是用户点「设置…」完全没反应，只在 stderr 里留一行
        ``unrecognized arguments``。
        """
        from src.gui.settings_window import settings_command

        cmd = settings_command("/tmp/some config.yaml")
        assert "--config" in cmd and "_settings-gui" in cmd
        assert cmd.index("--config") < cmd.index("_settings-gui")
        assert cmd[cmd.index("--config") + 1] == "/tmp/some config.yaml"

    def test_command_without_config_still_valid(self) -> None:
        from src.gui.settings_window import settings_command

        cmd = settings_command()
        assert cmd[-1] == "_settings-gui"
        assert "--config" not in cmd


class TestErrorExplanation:
    @pytest.mark.parametrize(
        "raw, needle",
        [
            ("LOGIN failed", "授权码"),
            ("getaddrinfo failed", "找不到服务器"),
            ("timed out", "超时"),
            ("[SSL: CERTIFICATE_VERIFY_FAILED]", "TLS"),
            ("Connection refused", "端口"),
            ("something odd", "连接失败"),
        ],
    )
    def test_translates_common_failures(self, raw: str, needle: str) -> None:
        assert needle in _explain_error(RuntimeError(raw))
