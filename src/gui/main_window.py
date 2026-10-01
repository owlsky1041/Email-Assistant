"""程序主窗口：同步状态面板 + 检索 + 邮件详情。

线程规则
--------
tkinter 只能在主线程操作。这里所有耗时动作（同步、检索、读正文）都丢到
工作线程，再用队列把结果投回主线程执行 —— 在工作线程里直接碰控件会随机崩溃。

进度面板比较特殊：它不靠事件推送，而是**每 500ms 轮询一次**
``context.progress.snapshot()``。原因是同步可能由托盘/定时任务在别的入口
触发，轮询能同时覆盖"这个窗口自己发起的同步"和"后台自己跑起来的同步"。
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable

from ..config import AppConfig, is_frozen
from . import window_available
from .main_model import (
    AttachmentRow,
    DashboardView,
    MessageDetail,
    SearchRow,
    corpus_stats,
    format_size,
    load_detail,
    run_search,
)

logger = logging.getLogger(__name__)

SOURCE_ROOT = Path(__file__).resolve().parent.parent.parent

WINDOW_TITLE = "邮件助手"
POLL_INTERVAL_MS = 500

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
    except Exception:  # noqa: BLE001
        return None
    for name in _CJK_FONTS:
        if name.lower() in available:
            return name
    return None


class MainWindow:
    """主窗口。

    :param context: 已装配好的 :class:`~src.context.AppContext`。
    :param autosync: 打开窗口时是否立刻跑一次增量同步。
    """

    def __init__(self, context: Any, *, autosync: bool = False) -> None:
        self.context = context
        self.config: AppConfig = context.config
        self._closed = False
        self._results: queue.Queue[Callable[[], None]] = queue.Queue()
        self._busy = {"sync": False, "search": False, "detail": False}
        self._rows: list[SearchRow] = []
        self._attachments: list[AttachmentRow] = []
        self._last_snapshot_sig = ""

        self.root = tk.Tk()
        self.root.title(WINDOW_TITLE)
        self.root.minsize(900, 600)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        family = _pick_cjk_font(self.root)
        if family:
            try:
                from tkinter import font as tkfont

                for logical in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                    tkfont.nametofont(logical).configure(family=family)
                self.root.option_add("*Font", (family, 10))
            except Exception:  # noqa: BLE001
                logger.debug("设置中文字体失败", exc_info=True)

        self._build()
        self._center()
        self.root.after(POLL_INTERVAL_MS, self._pump)
        self._refresh_stats()
        if autosync:
            self.root.after(400, self._on_sync)

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------

    def _build(self) -> None:
        outer = ttk.Frame(self.root, padding=10)
        outer.pack(fill="both", expand=True)

        self._build_dashboard(outer)

        ttk.Separator(outer).pack(fill="x", pady=(8, 8))

        self._build_search(outer)

    # ---- 状态面板 ----

    def _build_dashboard(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="同步状态", padding=10)
        box.pack(fill="x")

        # 第一行：语料规模
        stats = ttk.Frame(box)
        stats.pack(fill="x")
        self.var_messages = tk.StringVar(value="0")
        self.var_chunks = tk.StringVar(value="0")
        self.var_vectors = tk.StringVar(value="0")
        self.var_pending = tk.StringVar(value="0")
        for label, var, tip in (
            ("邮件总数", self.var_messages, "封"),
            ("切片", self.var_chunks, "个"),
            ("向量", self.var_vectors, "条"),
            ("待索引", self.var_pending, "封"),
        ):
            cell = ttk.Frame(stats)
            cell.pack(side="left", padx=(0, 28))
            ttk.Label(cell, text=label, foreground="#666").pack(anchor="w")
            ttk.Label(cell, textvariable=var, font=("TkDefaultFont", 15, "bold")).pack(anchor="w")

        # 第二行：运行状态 + 进度条
        line = ttk.Frame(box)
        line.pack(fill="x", pady=(10, 0))
        self.var_phase = tk.StringVar(value="空闲")
        self.var_folder = tk.StringVar(value="")
        ttk.Label(line, textvariable=self.var_phase).pack(side="left")
        ttk.Label(line, textvariable=self.var_folder, foreground="#666").pack(
            side="left", padx=(10, 0)
        )

        bar = ttk.Frame(box)
        bar.pack(fill="x", pady=(6, 0))
        self.progress = ttk.Progressbar(bar, mode="determinate", maximum=100)
        self.progress.pack(side="left", fill="x", expand=True)
        self.var_percent = tk.StringVar(value="0%")
        ttk.Label(bar, textvariable=self.var_percent, width=8, anchor="e").pack(
            side="left", padx=(8, 0)
        )

        # 第三行：正在处理的邮件
        self.var_current = tk.StringVar(value="尚未同步")
        ttk.Label(box, textvariable=self.var_current, foreground="#0a4d8c",
                  wraplength=880, justify="left").pack(anchor="w", pady=(8, 0))

        # 第四行：速率与计数
        detail = ttk.Frame(box)
        detail.pack(fill="x", pady=(4, 0))
        self.var_rate = tk.StringVar(value="速率 —")
        self.var_eta = tk.StringVar(value="剩余 —")
        self.var_counters = tk.StringVar(value="")
        for var in (self.var_rate, self.var_eta, self.var_counters):
            ttk.Label(detail, textvariable=var, foreground="#666").pack(side="left", padx=(0, 20))

        # 第五行：操作按钮
        actions = ttk.Frame(box)
        actions.pack(fill="x", pady=(10, 0))
        self.sync_btn = ttk.Button(actions, text="立即同步", command=self._on_sync)
        self.sync_btn.pack(side="left")
        self.full_btn = ttk.Button(actions, text="全量重扫", command=lambda: self._on_sync(full=True))
        self.full_btn.pack(side="left", padx=(6, 0))
        self.index_btn = ttk.Button(actions, text="建立索引", command=self._on_index)
        self.index_btn.pack(side="left", padx=(6, 0))
        ttk.Button(actions, text="打开归档目录", command=self._on_open_archive).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(actions, text="设置…", command=self._on_open_settings).pack(
            side="left", padx=(6, 0)
        )
        self.status_label = ttk.Label(actions, text="", foreground="#555")
        self.status_label.pack(side="right")

    # ---- 检索 ----

    def _build_search(self, parent: ttk.Frame) -> None:
        box = ttk.LabelFrame(parent, text="检索", padding=10)
        box.pack(fill="both", expand=True)

        row = ttk.Frame(box)
        row.pack(fill="x")
        self.var_query = tk.StringVar(value="")
        self.entry = ttk.Entry(row, textvariable=self.var_query)
        self.entry.pack(side="left", fill="x", expand=True)
        self.entry.bind("<Return>", lambda _e: self._on_search())
        self.search_btn = ttk.Button(row, text="检索", command=self._on_search)
        self.search_btn.pack(side="left", padx=(8, 0))
        self.var_limit = tk.StringVar(value="30")
        ttk.Label(row, text="条数").pack(side="left", padx=(10, 4))
        ttk.Combobox(
            row, textvariable=self.var_limit, values=("10", "20", "30", "50", "100"),
            width=5, state="readonly",
        ).pack(side="left")

        split = ttk.PanedWindow(box, orient="horizontal")
        split.pack(fill="both", expand=True, pady=(10, 0))

        # 左：结果表
        left = ttk.Frame(split)
        split.add(left, weight=3)
        columns = ("subject", "sender", "date", "score")
        self.tree = ttk.Treeview(left, columns=columns, show="headings", selectmode="browse")
        for key, title, width, anchor in (
            ("subject", "主题", 260, "w"),
            ("sender", "发件人", 150, "w"),
            ("date", "时间", 130, "w"),
            ("score", "分数", 60, "e"),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, anchor=anchor, stretch=(key == "subject"))
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select_result)
        self.tree.bind("<Double-1>", lambda _e: self._on_open_markdown())

        # 右：邮件详情
        right = ttk.Frame(split)
        split.add(right, weight=4)

        self.var_detail_title = tk.StringVar(value="在上方输入关键字检索")
        ttk.Label(right, textvariable=self.var_detail_title, font=("TkDefaultFont", 11, "bold"),
                  wraplength=460, justify="left").pack(anchor="w")

        self.var_detail_meta = tk.StringVar(value="")
        ttk.Label(right, textvariable=self.var_detail_meta, foreground="#666",
                  wraplength=460, justify="left").pack(anchor="w", pady=(4, 6))

        body_box = ttk.Frame(right)
        body_box.pack(fill="both", expand=True)
        self.body_text = tk.Text(body_box, wrap="word", height=12, relief="solid", borderwidth=1)
        body_scroll = ttk.Scrollbar(body_box, orient="vertical", command=self.body_text.yview)
        self.body_text.configure(yscrollcommand=body_scroll.set)
        self.body_text.pack(side="left", fill="both", expand=True)
        body_scroll.pack(side="right", fill="y")
        self.body_text.configure(state="disabled")

        att_box = ttk.LabelFrame(right, text="附件", padding=6)
        att_box.pack(fill="x", pady=(8, 0))
        self.att_tree = ttk.Treeview(
            att_box, columns=("name", "size", "ok"), show="headings", height=4, selectmode="browse"
        )
        for key, title, width, anchor in (
            ("name", "文件名", 260, "w"),
            ("size", "大小", 80, "e"),
            ("ok", "状态", 50, "center"),
        ):
            self.att_tree.heading(key, text=title)
            self.att_tree.column(key, width=width, anchor=anchor)
        self.att_tree.pack(fill="x")
        self.att_tree.bind("<Double-1>", lambda _e: self._on_open_attachment())

        att_actions = ttk.Frame(att_box)
        att_actions.pack(fill="x", pady=(6, 0))
        ttk.Button(att_actions, text="打开附件", command=self._on_open_attachment).pack(side="left")
        ttk.Button(att_actions, text="打开所在目录", command=self._on_open_attachment_dir).pack(
            side="left", padx=(6, 0)
        )
        ttk.Button(att_actions, text="打开归档 .md", command=self._on_open_markdown).pack(
            side="left", padx=(6, 0)
        )
        self.var_att_hint = tk.StringVar(value="")
        ttk.Label(att_box, textvariable=self.var_att_hint, foreground="#666").pack(
            anchor="w", pady=(4, 0)
        )

    # ------------------------------------------------------------------
    # 线程协作
    # ------------------------------------------------------------------

    def _post(self, fn: Callable[[], None]) -> None:
        self._results.put(fn)

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
            self._tick()
            self.root.after(POLL_INTERVAL_MS, self._pump)

    def _tick(self) -> None:
        """每半秒刷新一次状态面板。"""
        try:
            snapshot = self.context.progress.snapshot(event_limit=10)
        except Exception:  # noqa: BLE001
            logger.debug("读取进度快照失败", exc_info=True)
            return
        view = DashboardView.from_snapshot(snapshot)
        self._apply_view(view)

        # 同步刚结束：计数需要刷新
        sig = f"{snapshot.get('running')}:{snapshot.get('finished_at')}"
        if sig != self._last_snapshot_sig:
            self._last_snapshot_sig = sig
            if not snapshot.get("running"):
                self._refresh_stats()
                self._set_busy("sync", False)

    def _apply_view(self, view: DashboardView) -> None:
        self.var_phase.set(f"状态：{view.phase_label}")
        self.var_folder.set(view.folders_progress)
        self.progress.configure(value=min(100.0, max(0.0, view.percent)))
        self.var_percent.set(f"{view.percent:.0f}%")
        if view.total:
            self.var_percent.set(f"{view.processed}/{view.total}")
        self.var_current.set(view.current_message or ("尚未同步" if not view.running else "准备中…"))
        self.var_rate.set(f"速率 {view.rate_text}")
        self.var_eta.set(f"剩余 {view.eta_text}")
        self.var_counters.set(view.counters_text)
        if view.last_error:
            self.status_label.configure(text=f"最近错误：{view.last_error[:60]}", foreground="#b00020")

    def _set_busy(self, key: str, busy: bool, message: str = "") -> None:
        self._busy[key] = busy
        if key == "sync":
            state = "disabled" if busy else "normal"
            for btn in (self.sync_btn, self.full_btn, self.index_btn):
                try:
                    btn.configure(state=state)
                except Exception:  # noqa: BLE001
                    pass
        if key == "search":
            try:
                self.search_btn.configure(state="disabled" if busy else "normal")
            except Exception:  # noqa: BLE001
                pass
        if message:
            self.status_label.configure(text=message, foreground="#555")

    def _refresh_stats(self) -> None:
        stats = corpus_stats(self.context)
        self.var_messages.set(str(stats["messages"]))
        self.var_chunks.set(str(stats["chunks"]))
        self.var_vectors.set(str(stats["vectors"]))
        self.var_pending.set(str(stats["pending_index"]))

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------

    def _on_sync(self, *, full: bool = False) -> None:
        if self._busy["sync"]:
            return
        if self.context.sync.is_running:
            self.status_label.configure(text="已有同步在进行中", foreground="#b06000")
            return
        self._set_busy("sync", True, "正在全量重扫…" if full else "正在同步…")

        def work() -> None:
            try:
                if full:
                    self.context.sync.sync_all(full=True)
                else:
                    self.context.sync.sync_all()
            except Exception as exc:  # noqa: BLE001
                logger.exception("同步失败")
                self._post(lambda: self.status_label.configure(
                    text=f"同步失败：{exc}", foreground="#b00020"))
            finally:
                self._post(lambda: self._set_busy("sync", False))

        threading.Thread(target=work, name="main-sync", daemon=True).start()

    def _on_index(self) -> None:
        if self._busy["sync"]:
            return
        self._set_busy("sync", True, "正在建立索引…")

        def work() -> None:
            try:
                stats = self.context.indexer.index_pending()
                text = f"索引完成：{stats.chunks} 切片"
                self._post(lambda: self.status_label.configure(text=text, foreground="#0a7d28"))
            except Exception as exc:  # noqa: BLE001
                logger.exception("建立索引失败")
                self._post(lambda: self.status_label.configure(
                    text=f"索引失败：{exc}", foreground="#b00020"))
            finally:
                self._post(self._refresh_stats)
                self._post(lambda: self._set_busy("sync", False))

        threading.Thread(target=work, name="main-index", daemon=True).start()

    # ------------------------------------------------------------------
    # 检索与详情
    # ------------------------------------------------------------------

    def _on_search(self) -> None:
        query = self.var_query.get().strip()
        if not query:
            return
        if self._busy["search"]:
            return
        try:
            limit = int(self.var_limit.get())
        except ValueError:
            limit = 30

        self._set_busy("search", True, "正在检索…")
        self.tree.delete(*self.tree.get_children())
        self._rows = []

        def work() -> None:
            try:
                rows = run_search(self.context, query, limit=limit)
            except Exception as exc:  # noqa: BLE001
                logger.exception("检索失败")
                self._post(lambda: self._on_search_failed(exc))
                return
            self._post(lambda: self._on_search_done(rows, query))

        threading.Thread(target=work, name="main-search", daemon=True).start()

    def _on_search_failed(self, exc: Exception) -> None:
        self._set_busy("search", False, f"检索失败：{exc}")

    def _on_search_done(self, rows: list[SearchRow], query: str) -> None:
        self._rows = rows
        for index, row in enumerate(rows):
            self.tree.insert("", "end", iid=str(index), values=row.to_tree_values())
        self._set_busy("search", False, f"「{query}」命中 {len(rows)} 条")
        if rows:
            first = self.tree.get_children()[0]
            self.tree.selection_set(first)
            self.tree.focus(first)

    def _on_select_result(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        try:
            row = self._rows[int(selection[0])]
        except (ValueError, IndexError):
            return
        self._load_detail(row.message_id)

    def _load_detail(self, message_id: str) -> None:
        self.var_detail_title.set("正在读取…")
        self.var_detail_meta.set("")
        self.var_att_hint.set("")
        self._set_body("")
        self.att_tree.delete(*self.att_tree.get_children())
        self._attachments = []

        def work() -> None:
            try:
                detail = load_detail(self.context, message_id)
            except Exception as exc:  # noqa: BLE001
                logger.exception("读取邮件详情失败")
                self._post(lambda: self._on_detail_failed(exc))
                return
            self._post(lambda: self._on_detail_done(detail))

        threading.Thread(target=work, name="main-detail", daemon=True).start()

    def _on_detail_failed(self, exc: Exception) -> None:
        self.var_detail_title.set(f"读取失败：{exc}")

    def _on_detail_done(self, detail: MessageDetail) -> None:
        if not detail.found:
            self.var_detail_title.set(detail.error or "未找到该邮件")
            return
        self.var_detail_title.set(detail.subject or "(无主题)")
        meta = [f"{k}：{v}" for k, v in detail.header_lines() if k != "主题"]
        if detail.body_truncated:
            meta.append("（正文过长，仅显示前 20 万字符）")
        self.var_detail_meta.set("\n".join(meta))
        self._set_body(detail.body)

        self._attachments = detail.attachments
        for index, att in enumerate(self._attachments):
            self.att_tree.insert("", "end", iid=str(index), values=att.to_tree_values())
        if self._attachments:
            missing = sum(1 for a in self._attachments if not a.exists)
            self.var_att_hint.set(
                f"共 {len(self._attachments)} 个附件"
                + (f"，其中 {missing} 个文件缺失" if missing else "")
                + "（双击打开）"
            )
        else:
            self.var_att_hint.set("这封邮件没有附件")

    def _set_body(self, text: str) -> None:
        self.body_text.configure(state="normal")
        self.body_text.delete("1.0", "end")
        self.body_text.insert("1.0", text)
        self.body_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # 打开文件
    # ------------------------------------------------------------------

    def _selected_attachment(self) -> AttachmentRow | None:
        selection = self.att_tree.selection()
        if not selection:
            return None
        try:
            return self._attachments[int(selection[0])]
        except (ValueError, IndexError):
            return None

    def _on_open_attachment(self) -> None:
        from ..tray_app import open_local_path

        att = self._selected_attachment()
        if att is None:
            self.status_label.configure(text="请先选中一个附件", foreground="#b06000")
            return
        if not att.exists:
            messagebox.showwarning(
                "文件缺失",
                f"附件文件不在磁盘上：\n{att.local_path}\n\n"
                "可以执行 `main.py verify-blobs --repair` 尝试从内容仓库重建。",
                parent=self.root,
            )
            return
        open_local_path(att.local_path)

    def _on_open_attachment_dir(self) -> None:
        from ..tray_app import open_local_path

        att = self._selected_attachment()
        if att is None or not att.local_path:
            self.status_label.configure(text="请先选中一个附件", foreground="#b06000")
            return
        open_local_path(Path(att.local_path).parent)

    def _selected_markdown(self) -> str:
        selection = self.tree.selection()
        if not selection:
            return ""
        try:
            index = int(selection[0])
        except ValueError:
            return ""
        if not (0 <= index < len(self._rows)):
            return ""
        detail = load_detail(self.context, self._rows[index].message_id)
        return detail.markdown_path

    def _on_open_markdown(self) -> None:
        from ..tray_app import open_local_path

        path = self._selected_markdown()
        if not path or not Path(path).is_file():
            self.status_label.configure(text="没有可打开的归档文件", foreground="#b06000")
            return
        open_local_path(path)

    def _on_open_archive(self) -> None:
        from ..tray_app import open_local_path

        open_local_path(self.config.archive_path)

    def _on_open_settings(self) -> None:
        from .settings_window import open_window_process

        source = getattr(self.config, "source_path", None)
        open_window_process(config_path=str(source) if source else None)

    # ------------------------------------------------------------------

    def _center(self) -> None:
        self.root.update_idletasks()
        width = max(self.root.winfo_reqwidth(), 1040)
        height = max(self.root.winfo_reqheight(), 720)
        x = max(0, (self.root.winfo_screenwidth() - width) // 2)
        y = max(0, (self.root.winfo_screenheight() - height) // 3)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def _on_close(self) -> None:
        self._closed = True
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    def run(self) -> int:
        self.root.mainloop()
        return 0


def run_main_window(context: Any, *, autosync: bool = False) -> int:
    """在当前（主）线程打开主窗口，阻塞到关闭。"""
    return MainWindow(context, autosync=autosync).run()


# ----------------------------------------------------------------------
# 独立子进程入口
# ----------------------------------------------------------------------


def main_window_command(config_path: str | None = None, *, autosync: bool = False) -> list[str]:
    """构造「打开主窗口」的子进程命令（兼容打包后的 exe）。

    ``--config`` 是全局选项，必须排在子命令**前面**。
    """
    prefix: list[str] = []
    if config_path:
        prefix = ["--config", str(config_path)]
    tail = ["_main-gui"] + (["--sync"] if autosync else [])
    if is_frozen():
        return [sys.executable, *prefix, *tail]
    return [sys.executable, str(SOURCE_ROOT / "main.py"), *prefix, *tail]
