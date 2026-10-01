"""原生设置窗口（tkinter）。

线程规则
--------
tkinter **只能在主线程**创建和操作。本模块提供两种用法：

* :func:`run_settings_window` —— 当前就在主线程时直接调用（CLI 命令、首次运行）。
* :func:`open_window_process` —— 调用方在后台线程（托盘、FastAPI）时，
  fork 一个独立子进程去开窗口；子进程天然拥有自己的主线程。

网络与推理这类耗时操作一律丢到工作线程，再通过队列把结果投回主线程 ——
在工作线程里直接碰控件会随机崩溃。
"""

from __future__ import annotations

import logging
import os
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..config import AppConfig, is_frozen
from .settings_model import (
    BACKEND_LABELS,
    DEFAULT_ENDPOINT,
    SUGGESTED_REPOS,
    ConnectionResult,
    SettingsDraft,
    detect_data_root,
    download_model_to_config,
    import_model_to_config,
    layout_from_root,
    model_status,
    save_draft,
    test_connection,
)

logger = logging.getLogger(__name__)

SOURCE_ROOT = Path(__file__).resolve().parent.parent.parent

WINDOW_TITLE = "邮件助手 · 设置"

#: 中文字体候选：按平台常见顺序挑第一个装了的
_CJK_FONTS = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Source Han Sans SC",
    "WenQuanYi Micro Hei",
    "Droid Sans Fallback",
    "SimHei",
)


def _pick_cjk_font(root: tk.Misc) -> str | None:
    try:
        from tkinter import font as tkfont

        available = {name.lower() for name in tkfont.families(root)}
    except Exception:  # noqa: BLE001 - 取不到字体列表就用默认
        return None
    for name in _CJK_FONTS:
        if name.lower() in available:
            return name
    return None


class SettingsWindow:
    """四页式设置窗口。"""

    def __init__(self, config: AppConfig, *, require_auth: bool = False) -> None:
        self.config = config
        self.require_auth = require_auth
        self.saved = False
        self._closed = False
        self._results: queue.Queue[Callable[[], None]] = queue.Queue()
        self._busy = False

        auth_present, auth_backend = self._probe_secret()
        self.draft = SettingsDraft.from_config(
            config, auth_code_present=auth_present, auth_backend=auth_backend
        )
        _, self.layout_mismatches = detect_data_root(config)

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.minsize(720, 560)
        self.root.protocol("WM_DELETE_WINDOW", self._on_cancel)

        family = _pick_cjk_font(self.root)
        if family:
            try:
                from tkinter import font as tkfont

                for logical in ("TkDefaultFont", "TkTextFont", "TkMenuFont"):
                    tkfont.nametofont(logical).configure(family=family)
                self.root.option_add("*Font", (family, 10))
            except Exception:  # noqa: BLE001
                logger.debug("设置中文字体失败，使用默认字体", exc_info=True)

        self._build()
        self._center()
        self.root.after(80, self._pump)

    # ------------------------------------------------------------------
    # 密钥探测
    # ------------------------------------------------------------------

    def _probe_secret(self) -> tuple[bool, str]:
        try:
            from ..secret_store import SecretStore

            store = SecretStore(self.config)
            ref = self.config.email.auth_code_ref
            present = bool(store.get(ref))
            return present, store.backend_name(ref) if present else ""
        except Exception:  # noqa: BLE001 - 密钥库不可用不该拦住设置界面
            logger.debug("探测授权码失败", exc_info=True)
            return False, ""

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        self._build_mail_tab()
        self._build_storage_tab()
        self._build_sync_tab()
        self._build_model_tab()

        self.status = tk.StringVar(value="")
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Label(bar, textvariable=self.status, foreground="#555").pack(side="left")
        self.save_btn = ttk.Button(bar, text="保存并关闭", command=self._on_save_close)
        self.save_btn.pack(side="right")
        ttk.Button(bar, text="仅保存", command=self._on_save_only).pack(side="right", padx=6)
        ttk.Button(bar, text="取消", command=self._on_cancel).pack(side="right")

    # ---- 第一页：邮箱接入 ----

    def _build_mail_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text=" 邮箱接入 ")

        grid = ttk.Frame(tab)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)

        self.var_address = tk.StringVar(value=self.draft.address)
        self.var_server = tk.StringVar(value=self.draft.imap_server)
        self.var_port = tk.StringVar(value=str(self.draft.imap_port))
        self.var_ssl = tk.BooleanVar(value=self.draft.use_ssl)
        self.var_auth = tk.StringVar(value="")
        self.var_show_auth = tk.BooleanVar(value=False)

        def row(r: int, label: str) -> None:
            ttk.Label(grid, text=label).grid(row=r, column=0, sticky="w", pady=6, padx=(0, 10))

        row(0, "邮箱账号")
        ttk.Entry(grid, textvariable=self.var_address).grid(row=0, column=1, sticky="ew")
        ttk.Label(grid, text="例：you@yourcorp.com", foreground="#888").grid(
            row=0, column=2, sticky="w", padx=(8, 0)
        )

        row(1, "IMAP 服务器")
        ttk.Entry(grid, textvariable=self.var_server).grid(row=1, column=1, sticky="ew")
        ttk.Label(grid, text="腾讯企业邮箱：imap.exmail.qq.com", foreground="#888").grid(
            row=1, column=2, sticky="w", padx=(8, 0)
        )

        row(2, "端口")
        port_box = ttk.Frame(grid)
        port_box.grid(row=2, column=1, sticky="w")
        ttk.Entry(port_box, textvariable=self.var_port, width=8).pack(side="left")
        ttk.Checkbutton(port_box, text="使用 SSL/TLS", variable=self.var_ssl).pack(
            side="left", padx=(12, 0)
        )
        ttk.Label(grid, text="SSL 一般用 993", foreground="#888").grid(
            row=2, column=2, sticky="w", padx=(8, 0)
        )

        row(3, "客户端授权码")
        auth_box = ttk.Frame(grid)
        auth_box.grid(row=3, column=1, sticky="ew")
        auth_box.columnconfigure(0, weight=1)
        self.auth_entry = ttk.Entry(auth_box, textvariable=self.var_auth, show="•")
        self.auth_entry.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(
            auth_box, text="显示", variable=self.var_show_auth, command=self._toggle_auth
        ).grid(row=0, column=1, padx=(8, 0))

        if self.draft.auth_code_present:
            hint = f"已保存（{self.draft.auth_backend or '密钥库'}）· 留空表示不修改"
            color = "#0a7d28"
        else:
            hint = "尚未保存 · 不是网页登录密码"
            color = "#b06000"
        ttk.Label(grid, text=hint, foreground=color).grid(
            row=3, column=2, sticky="w", padx=(8, 0)
        )

        note = (
            "授权码在腾讯企业邮箱「设置 → 邮箱绑定 → 客户端专用密码」里生成，\n"
            "它只写入系统密钥库 / 本地加密文件，不会写进 config.yaml。"
        )
        ttk.Label(tab, text=note, foreground="#666", justify="left").pack(
            anchor="w", pady=(14, 0)
        )

        ttk.Separator(tab).pack(fill="x", pady=14)

        test_bar = ttk.Frame(tab)
        test_bar.pack(fill="x")
        self.test_btn = ttk.Button(test_bar, text="测试连接", command=self._on_test)
        self.test_btn.pack(side="left")
        self.test_label = ttk.Label(test_bar, text="", foreground="#555", wraplength=520)
        self.test_label.pack(side="left", padx=(12, 0), fill="x", expand=True)

    # ---- 第二页：数据存放 ----

    def _build_storage_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text=" 数据存放 ")

        self.var_root = tk.StringVar(value=self.draft.data_root)

        ttk.Label(tab, text="数据根目录").pack(anchor="w")
        row = ttk.Frame(tab)
        row.pack(fill="x", pady=(6, 0))
        row.columnconfigure(0, weight=1)
        ttk.Entry(row, textvariable=self.var_root).grid(row=0, column=0, sticky="ew")
        ttk.Button(row, text="浏览…", command=self._on_browse_root).grid(
            row=0, column=1, padx=(8, 0)
        )
        def _on_root_changed(*_: object) -> None:
            self._refresh_layout_preview()
            # 模型目录是数据根目录的子目录，根目录一变状态就过期了
            self._refresh_model_status()

        self.var_root.trace_add("write", _on_root_changed)

        ttk.Label(
            tab,
            text="归档、附件、数据库、向量库、备份和模型都放在这个目录下，只填一个即可。",
            foreground="#666",
        ).pack(anchor="w", pady=(6, 0))

        box = ttk.LabelFrame(tab, text="实际落盘位置", padding=10)
        box.pack(fill="both", expand=True, pady=(14, 0))
        self.layout_text = tk.Text(box, height=9, wrap="none", relief="flat", background="#f7f7f7")
        self.layout_text.pack(fill="both", expand=True)
        self.layout_text.configure(state="disabled")

        self.layout_warn = ttk.Label(tab, text="", foreground="#b06000", wraplength=640,
                                     justify="left")
        self.layout_warn.pack(anchor="w", pady=(10, 0))
        if self.layout_mismatches:
            self.layout_warn.configure(
                text="检测到原有的自定义路径，保存后会统一到上面的标准布局：\n  · "
                + "\n  · ".join(self.layout_mismatches)
            )
        self._refresh_layout_preview()

    def _refresh_layout_preview(self) -> None:
        """展示**解析后**的绝对路径。

        只把第一行解析掉、其余留相对路径，会让人以为它们不在同一个目录下，
        所以这里统一解析：相对路径按项目基准展开，和运行时 ``AppConfig.resolve``
        的行为保持一致。
        """
        from .settings_model import project_base

        paths = layout_from_root(self.var_root.get())
        lines = [
            ("邮件归档", "archive_dir"),
            ("附件目录", "attachment_dir"),
            ("数据库", "sqlite_path"),
            ("向量库", "chroma_dir"),
            ("备份", "backup_dir"),
            ("嵌入模型", "model_dir"),
        ]
        self.layout_text.configure(state="normal")
        self.layout_text.delete("1.0", "end")
        for title, key in lines:
            raw = paths.get(key, "")
            try:
                p = Path(raw).expanduser()
                if not p.is_absolute():
                    p = project_base() / p
                shown = str(p.resolve())
            except (OSError, ValueError):
                shown = raw
            self.layout_text.insert("end", f"{title:<8}{shown}\n")
        self.layout_text.configure(state="disabled")

    # ---- 第三页：同步 ----

    def _build_sync_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text=" 同步 ")

        left = ttk.LabelFrame(tab, text="同步哪些文件夹（不选 = 全部）", padding=10)
        left.pack(side="left", fill="both", expand=True)
        self.folder_list = tk.Listbox(left, selectmode="extended", height=10, exportselection=False)
        self.folder_list.pack(fill="both", expand=True)
        for name in self.draft.folders:
            self.folder_list.insert("end", name)

        btns = ttk.Frame(left)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="从服务器获取", command=self._on_fetch_folders).pack(side="left")
        ttk.Button(btns, text="清空(=全部)", command=lambda: self.folder_list.selection_clear(0, "end")).pack(
            side="left", padx=(6, 0)
        )

        right = ttk.Frame(tab)
        right.pack(side="left", fill="both", expand=True, padx=(16, 0))
        # 同一个父容器里不能混用 pack 和 grid（Tcl 会直接抛 TclError），
        # 因此这一侧上下全部用 grid，靠行号递增排版。
        right.columnconfigure(1, weight=1)

        self.var_exclude = tk.StringVar(value=", ".join(self.draft.exclude_folders))
        ttk.Label(right, text="排除文件夹（逗号分隔）").grid(row=0, column=0, sticky="w")
        ttk.Entry(right, textvariable=self.var_exclude).grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(6, 14)
        )

        self.var_interval = tk.StringVar(value=str(self.draft.interval_minutes))
        self.var_workers = tk.StringVar(value=str(self.draft.fetch_workers))
        self.var_batch = tk.StringVar(value=str(self.draft.fetch_batch_size))
        self.var_attach_mb = tk.StringVar(value=str(self.draft.max_attachment_size_mb))
        self.var_per_run = tk.StringVar(value=str(self.draft.max_messages_per_run))
        self.var_download_attach = tk.BooleanVar(value=self.draft.download_attachments)
        self.var_reconcile = tk.BooleanVar(value=self.draft.reconcile_deletions)

        def field(r: int, label: str, var: tk.StringVar, hint: str = "") -> None:
            ttk.Label(right, text=label).grid(row=r, column=0, sticky="w", pady=4)
            ttk.Entry(right, textvariable=var, width=10).grid(
                row=r, column=1, sticky="w", padx=(10, 8)
            )
            if hint:
                ttk.Label(right, text=hint, foreground="#888").grid(row=r, column=2, sticky="w")

        field(2, "同步间隔（分钟）", self.var_interval)
        field(3, "并发下载连接数", self.var_workers, "1-16，建议 3-5")
        field(4, "分批拉取大小", self.var_batch, "每批多少封")
        field(5, "附件大小上限（MB）", self.var_attach_mb, "超过只记元数据")
        field(6, "单次同步封数上限", self.var_per_run, "0 = 不限制")

        ttk.Checkbutton(right, text="下载附件", variable=self.var_download_attach).grid(
            row=7, column=0, columnspan=3, sticky="w", pady=(10, 0)
        )
        ttk.Checkbutton(
            right, text="定期与服务器比对，标记已删除邮件", variable=self.var_reconcile
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(4, 0))

    # ---- 第四页：嵌入模型 ----

    def _build_model_tab(self) -> None:
        tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(tab, text=" 检索模型 ")

        self.var_backend = tk.StringVar(value=self.draft.embedding_backend)
        ttk.Label(tab, text="嵌入后端").pack(anchor="w")
        combo = ttk.Combobox(
            tab,
            textvariable=self.var_backend,
            values=list(BACKEND_LABELS),
            state="readonly",
            width=34,
        )
        combo.pack(anchor="w", pady=(6, 0))
        self.backend_hint = ttk.Label(tab, text="", foreground="#666", wraplength=640)
        self.backend_hint.pack(anchor="w", pady=(4, 0))
        combo.bind("<<ComboboxSelected>>", lambda *_: self._refresh_backend_hint())
        self._refresh_backend_hint()

        ttk.Separator(tab).pack(fill="x", pady=14)

        ttk.Label(tab, text="模型状态").pack(anchor="w")
        self.model_label = ttk.Label(tab, text="", wraplength=640, justify="left")
        self.model_label.pack(anchor="w", pady=(6, 0))
        self.progress = ttk.Progressbar(tab, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(10, 0))

        self.var_repo = tk.StringVar(value=self.draft.model_repo)
        self.var_endpoint = tk.StringVar(value=self.draft.model_endpoint)

        repo_row = ttk.Frame(tab)
        repo_row.pack(fill="x", pady=(14, 0))
        repo_row.columnconfigure(1, weight=1)
        ttk.Label(repo_row, text="模型仓库").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Combobox(
            repo_row, textvariable=self.var_repo, values=list(SUGGESTED_REPOS)
        ).grid(row=0, column=1, sticky="ew")
        ttk.Label(repo_row, text="下载源").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(6, 0))
        ttk.Entry(repo_row, textvariable=self.var_endpoint).grid(row=1, column=1, sticky="ew", pady=(6, 0))

        actions = ttk.Frame(tab)
        actions.pack(fill="x", pady=(14, 0))
        self.download_btn = ttk.Button(actions, text="下载模型", command=self._on_download_model)
        self.download_btn.pack(side="left")
        self.import_btn = ttk.Button(actions, text="从本地目录导入…", command=self._on_import_model)
        self.import_btn.pack(side="left", padx=(8, 0))

        ttk.Label(
            tab,
            text=(
                "提示：公开的社区 ONNX 导出，池化方式可能与官方 sentence-transformers 不一致，\n"
                "检索区分度会明显变差。生产环境建议用「从本地目录导入」导入本项目自带的模型，\n"
                "导入后可用 python scripts/calibrate_threshold.py 验证区分度。"
            ),
            foreground="#b06000",
            justify="left",
        ).pack(anchor="w", pady=(14, 0))

        self._refresh_model_status()

    def _refresh_backend_hint(self) -> None:
        hints = {
            "auto": "推荐。优先 ONNX，缺模型时逐级降级并在日志里告警。",
            "onnx": "只跑 ONNX；模型不可用会直接报错，不会静默退化。",
            "sentence-transformers": "需要 PyTorch（约 900MB），一般不必选。",
            "hashing": "占位实现，没有真实语义，只适合离线演示。",
        }
        self.backend_hint.configure(text=hints.get(self.var_backend.get(), ""))

    def _refresh_model_status(self, status: Any = None) -> None:
        # 这一页的控件可能还没建好（数据目录的 trace 会提前触发），拿不到就跳过
        if not hasattr(self, "model_label") or not hasattr(self, "var_repo"):
            return
        if status is None:
            try:
                status = model_status(self._current_config_preview())
            except Exception as exc:  # noqa: BLE001
                self.model_label.configure(text=f"检查模型失败：{exc}", foreground="#b00020")
                return
        if status.ready:
            size = sum(f.stat().st_size for f in status.path.glob("*") if f.is_file())
            note = ""
            if self.var_repo.get().strip() in SUGGESTED_REPOS:
                note = "\n⚠️ 来自社区导出，检索区分度可能偏弱，建议改用本项目自带模型导入"
            self.model_label.configure(
                text=f"✓ 可用（{size / 1048576:.1f} MB）\n{status.path}{note}",
                foreground="#0a7d28",
            )
        else:
            missing = "、".join(status.missing) or "未知"
            self.model_label.configure(
                text=f"✗ 不可用，缺少：{missing}\n{status.path}", foreground="#b00020"
            )

    def _current_config_preview(self) -> AppConfig:
        """拿当前界面上的数据目录去查模型状态，而不是磁盘上的旧配置。"""
        cfg = self.config.model_copy(deep=True)
        paths = layout_from_root(self.var_root.get())
        cfg.embedding.model_dir = paths["model_dir"]
        return cfg

    # ------------------------------------------------------------------
    # 主线程 / 工作线程协作
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        while True:
            try:
                fn = self._results.get_nowait()
            except queue.Empty:
                break
            try:
                fn()
            except Exception:  # noqa: BLE001
                logger.exception("处理界面回调失败")
        if not self._closed:
            self.root.after(80, self._pump)

    def _post(self, fn: Callable[[], None]) -> None:
        """从工作线程把回调投递回主线程。"""
        self._results.put(fn)

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for widget in (self.test_btn, self.download_btn, self.import_btn, self.save_btn):
            try:
                widget.configure(state=state)
            except Exception:  # noqa: BLE001
                pass
        self.status.set(message)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _toggle_auth(self) -> None:
        self.auth_entry.configure(show="" if self.var_show_auth.get() else "•")

    def _on_browse_root(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择数据存放目录",
            initialdir=self.var_root.get() or None,
            mustexist=False,
        )
        if chosen:
            self.var_root.set(chosen)
            self._refresh_model_status()

    def _collect(self) -> SettingsDraft:
        def as_int(var: tk.StringVar, default: int) -> int:
            try:
                return int(str(var.get()).strip())
            except (TypeError, ValueError):
                return default

        def as_float(var: tk.StringVar, default: float) -> float:
            try:
                return float(str(var.get()).strip())
            except (TypeError, ValueError):
                return default

        folders = [self.folder_list.get(i) for i in self.folder_list.curselection()]
        excludes = [s.strip() for s in self.var_exclude.get().split(",") if s.strip()]
        return self.draft.copy(
            address=self.var_address.get(),
            imap_server=self.var_server.get(),
            imap_port=as_int(self.var_port, 993),
            use_ssl=bool(self.var_ssl.get()),
            auth_code=self.var_auth.get().strip(),
            data_root=self.var_root.get().strip(),
            folders=folders,
            exclude_folders=excludes,
            interval_minutes=as_int(self.var_interval, 10),
            fetch_workers=as_int(self.var_workers, 3),
            fetch_batch_size=as_int(self.var_batch, 50),
            max_attachment_size_mb=as_float(self.var_attach_mb, 50.0),
            max_messages_per_run=as_int(self.var_per_run, 0),
            download_attachments=bool(self.var_download_attach.get()),
            reconcile_deletions=bool(self.var_reconcile.get()),
            embedding_backend=self.var_backend.get(),
            model_repo=self.var_repo.get(),
            model_endpoint=self.var_endpoint.get(),
        )

    def _on_test(self) -> None:
        draft = self._collect()
        errors = [e for e in draft.validate() if "授权码" not in e]
        if errors:
            messagebox.showerror("请先修正", "\n".join(errors), parent=self.root)
            return
        auth_code = draft.auth_code or self._existing_auth_code()
        if not auth_code:
            messagebox.showwarning(
                "缺少授权码", "请先填写客户端授权码，再测试连接。", parent=self.root
            )
            return

        self._set_busy(True, "正在连接服务器…")
        self.test_label.configure(text="连接中…", foreground="#555")

        def work() -> None:
            result = test_connection(draft, auth_code=auth_code, base_config=self.config)
            self._post(lambda: self._on_test_done(result))

        threading.Thread(target=work, name="settings-test", daemon=True).start()

    def _existing_auth_code(self) -> str:
        try:
            from ..secret_store import SecretStore

            return SecretStore(self.config).get(self.config.email.auth_code_ref) or ""
        except Exception:  # noqa: BLE001
            return ""

    def _on_test_done(self, result: ConnectionResult) -> None:
        self._set_busy(False, "")
        if result.ok:
            self.test_label.configure(text=f"✓ {result.message}", foreground="#0a7d28")
        else:
            self.test_label.configure(text="✗ 连接失败", foreground="#b00020")
            messagebox.showerror("连接失败", result.message, parent=self.root)

    def _on_fetch_folders(self) -> None:
        draft = self._collect()
        auth_code = draft.auth_code or self._existing_auth_code()
        if not auth_code:
            messagebox.showwarning("缺少授权码", "请先填写授权码并测试连接。", parent=self.root)
            return
        self._set_busy(True, "正在获取文件夹列表…")

        def work() -> None:
            result = test_connection(draft, auth_code=auth_code, base_config=self.config)
            self._post(lambda: self._on_folders_done(result))

        threading.Thread(target=work, name="settings-folders", daemon=True).start()

    def _on_folders_done(self, result: ConnectionResult) -> None:
        self._set_busy(False, "")
        if not result.ok:
            messagebox.showerror("获取失败", result.message, parent=self.root)
            return
        selected = {self.folder_list.get(i) for i in self.folder_list.curselection()}
        self.folder_list.delete(0, "end")
        for name in result.folders:
            self.folder_list.insert("end", name)
            if name in selected:
                self.folder_list.selection_set("end")
        self.status.set(f"已获取 {len(result.folders)} 个文件夹")

    def _on_download_model(self) -> None:
        draft = self._collect()
        if not draft.model_repo.strip():
            messagebox.showwarning("请填写仓库", "模型仓库不能为空。", parent=self.root)
            return
        self._set_busy(True, "正在下载模型…")
        self.progress.configure(value=0)

        def progress(name: str, done: int, total: int) -> None:
            pct = int(done * 100 / total) if total else 0
            self._post(lambda: (
                self.progress.configure(value=pct),
                self.status.set(f"下载 {name} … {pct}%"),
            ))

        def work() -> None:
            try:
                status = download_model_to_config(draft, self.config, on_progress=progress)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                self._post(lambda: self._on_model_failed(message))
                return
            self._post(lambda: self._on_model_done(status))

        threading.Thread(target=work, name="settings-model", daemon=True).start()

    def _on_import_model(self) -> None:
        source = filedialog.askdirectory(title="选择包含 model.onnx 的模型目录", mustexist=True)
        if not source:
            return
        draft = self._collect()
        self._set_busy(True, "正在导入模型…")

        def work() -> None:
            try:
                status = import_model_to_config(source, draft, self.config)
            except Exception as exc:  # noqa: BLE001
                message = str(exc)
                self._post(lambda: self._on_model_failed(message))
                return
            self._post(lambda: self._on_model_done(status))

        threading.Thread(target=work, name="settings-model-import", daemon=True).start()

    def _on_model_failed(self, message: str) -> None:
        self._set_busy(False, "")
        self._refresh_model_status()
        messagebox.showerror("模型获取失败", message, parent=self.root)

    def _on_model_done(self, status: Any) -> None:
        self._set_busy(False, "模型已就绪")
        self._refresh_model_status(status)

    def _on_save_only(self) -> None:
        if self._do_save():
            self.draft = self._collect()
            self.require_auth = False

    def _on_save_close(self) -> None:
        if self._do_save():
            self.saved = True
            self._close()

    def _do_save(self) -> bool:
        draft = self._collect()
        errors = draft.validate(require_auth_code=self.require_auth)
        if errors:
            messagebox.showerror("配置有误", "\n".join(f"· {e}" for e in errors), parent=self.root)
            return False
        try:
            result = save_draft(draft, self.config, require_auth_code=self.require_auth)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", str(exc), parent=self.root)
            return False

        bits = [f"配置已保存到\n{result.config_path}"]
        if result.secret_backend:
            bits.append(f"授权码已写入：{result.secret_backend}")
        if result.warnings:
            bits.append("\n".join(result.warnings))
        self.status.set("已保存")
        messagebox.showinfo("保存成功", "\n\n".join(bits), parent=self.root)
        return True

    def _on_cancel(self) -> None:
        if self.saved:
            self._close()
            return
        if messagebox.askyesno("放弃修改？", "还没有保存，确定关闭吗？", parent=self.root):
            self._close()

    # ------------------------------------------------------------------

    def _center(self) -> None:
        self.root.update_idletasks()
        w = max(self.root.winfo_reqwidth(), 780)
        h = max(self.root.winfo_reqheight(), 600)
        x = max(0, (self.root.winfo_screenwidth() - w) // 2)
        y = max(0, (self.root.winfo_screenheight() - h) // 3)
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def _close(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> bool:
        self.root.mainloop()
        return self.saved


def run_settings_window(config: AppConfig, *, require_auth: bool = False) -> bool:
    """在当前（主）线程打开设置窗口，阻塞到窗口关闭。"""
    window = SettingsWindow(config, require_auth=require_auth)
    return window.run()


# ----------------------------------------------------------------------
# 独立子进程
# ----------------------------------------------------------------------


def settings_command(config_path: str | None = None) -> list[str]:
    """构造「打开设置窗口」的子进程命令（兼容打包后的 exe）。

    ``--config`` 是**全局**选项，必须排在子命令**前面**；
    写成 ``_settings-gui --config X`` 会被 argparse 直接拒掉
    （unrecognized arguments），用户点什么都没反应。
    """
    prefix: list[str] = []
    if config_path:
        prefix = ["--config", str(config_path)]
    if is_frozen():
        # 打包后 sys.executable 就是本程序，直接复用自身的子命令
        return [sys.executable, *prefix, "_settings-gui"]
    # 源码运行：main.py 在**源码树根**，不是运行根目录（后者会随
    # EMAIL_ASSISTANT_HOME 变化，用它拼路径会永远找不到 main.py）
    return [sys.executable, str(SOURCE_ROOT / "main.py"), *prefix, "_settings-gui"]


def window_available() -> bool:
    """当前环境有没有图形界面。"""
    if sys.platform in ("win32", "darwin"):
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def open_window_process(*, config_path: str | None = None, wait: bool = False,
                        timeout: float | None = None) -> bool:
    """在独立子进程里打开设置窗口。

    供**后台线程**（托盘菜单、FastAPI 工作线程）调用：这些线程不能直接
    创建 Tk 窗口，而子进程天然拥有自己的主线程。
    """
    import subprocess

    if not window_available():
        logger.warning("当前环境没有图形界面，无法打开设置窗口")
        return False

    try:
        proc = subprocess.Popen(settings_command(config_path))
    except OSError as exc:
        logger.error("打开设置窗口失败：%s", exc)
        return False
    if not wait:
        return True
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning("设置窗口超时未关闭")
    return True
