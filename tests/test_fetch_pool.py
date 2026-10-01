"""并发下载的资源占用回归。

背景
----
``_process_parallel`` 早先**每个批次**都新建一个 ``ThreadPoolExecutor``。
线程一结束，挂在 ``threading.local()`` 上的 IMAP 连接就失去了复用可能，
而 ``close_all()`` 又要等整个文件夹同步结束才调用 —— 于是每个批次都新建
一批连接、旧连接还一直挂着不释放。

实测 53 封邮件建立了 45 条连接（约 2.8 条/批次）；按这个斜率外推，
10,000 封邮件约 3,537 条，TLS+LOGIN 握手均值 2.44 秒，仅握手约 2.4 小时，
而且腾讯企业邮箱对单账号并发连接数有限制。

这里锁死三条不变量：
1. 连接数只与 **workers** 有关，与批次数量无关；
2. 连接跨文件夹复用，不随文件夹数量线性增长；
3. 同步结束后不残留已连接的 socket。
"""

from __future__ import annotations

from typing import Any

import pytest

import src.sync_service as sync_module
from src.context import AppContext
from tests.conftest import FakeImapClient, build_eml


def _mailbox(count: int, *, folders: int = 1) -> dict[str, dict[str, bytes]]:
    """造 ``folders`` 个文件夹、每个 ``count`` 封邮件。"""
    return {
        "INBOX" if i == 0 else f"INBOX/{i}": {
            str(uid): build_eml(
                subject=f"第 {uid} 封",
                text=f"正文 {uid}",
                message_id=f"<m{i}-{uid}@corp.com>",
            )
            for uid in range(1, count + 1)
        }
        for i in range(folders)
    }


class _CountingClient:
    """每次实例化都记账的假客户端。

    所有实例各自持有一份独立的 ``FakeImapClient``，因此"建立了多少条连接"
    就等于实例数量。
    """

    instances: list["_CountingClient"] = []

    def __init__(self, mailbox: dict[str, dict[str, bytes]], *args: Any, **kwargs: Any) -> None:
        self._impl = FakeImapClient(mailbox)
        _CountingClient.instances.append(self)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._impl, name)

    # 魔术方法由类型而非实例查找，__getattr__ 兜不住，必须显式转发
    def __enter__(self) -> "_CountingClient":
        self._impl.__enter__()
        return self

    def __exit__(self, *exc: Any) -> bool:
        return bool(self._impl.__exit__(*exc))


@pytest.fixture(autouse=True)
def _reset_counter():
    _CountingClient.instances = []
    yield
    _CountingClient.instances = []


def _install(monkeypatch: pytest.MonkeyPatch, mailbox: dict[str, dict[str, bytes]]) -> None:
    """把 ImapClient 换成记账版。

    注意：真实代码调用的是 ``ImapClient(config, auth_code)``，
    所以工厂必须自己补上 mailbox，不能把 config 当成 mailbox 传进去。
    """
    monkeypatch.setattr(
        sync_module,
        "ImapClient",
        lambda *a, **kw: _CountingClient(mailbox, *a, **kw),
    )


def _main_client(mailbox: dict[str, dict[str, bytes]] | None = None) -> _CountingClient:
    """模拟 sync_folder 的调用方自己持有的那条主连接。"""
    return _CountingClient(mailbox or {})


class TestConnectionReuse:
    def test_connection_count_does_not_follow_batch_count(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """批次从 1 个涨到 20 个，连接数必须不变。"""
        _install(monkeypatch, _mailbox(20))
        context.config.sync.fetch_workers = 3

        counts: list[int] = []
        for batch_size, folder in ((20, "INBOX"), (1, "INBOX/1")):
            _CountingClient.instances = []
            context.config.sync.fetch_batch_size = batch_size
            context.sync.sync_folder(_main_client(), folder, index=False)
            counts.append(len(_CountingClient.instances))

        assert counts[0] == counts[1], (
            f"1 个批次用了 {counts[0]} 条连接，20 个批次用了 {counts[1]} 条；"
            "连接数不应随批次数量增长（线程池/连接池没被复用）"
        )
        assert counts[0] <= context.config.sync.fetch_workers + 1

    def test_pool_reused_across_folders(self, context: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
        """5 个文件夹不应变成 5 份连接池。"""
        mailbox = _mailbox(6, folders=5)
        _install(monkeypatch, mailbox)
        context.config.sync.fetch_workers = 3
        context.config.sync.fetch_batch_size = 3

        result = context.sync.sync_all(index=False)

        assert result.archived == 30
        created = len(_CountingClient.instances)
        assert created <= context.config.sync.fetch_workers + 1, (
            f"5 个文件夹用了 {created} 条连接，说明连接池没有跨文件夹复用"
        )

    def test_single_worker_has_no_pool(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """workers=1 走顺序路径，不额外建池。"""
        _install(monkeypatch, _mailbox(10))
        context.config.sync.fetch_workers = 1
        context.config.sync.fetch_batch_size = 3

        context.sync.sync_folder(_main_client(), "INBOX", index=False)

        assert len(_CountingClient.instances) == 1, "顺序路径不该建立工作连接"

    def test_no_connection_left_connected(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同步结束后所有连接都要断开，不能泄漏 socket。"""
        _install(monkeypatch, _mailbox(8))
        context.config.sync.fetch_workers = 3
        context.config.sync.fetch_batch_size = 4

        context.sync.sync_folder(_main_client(), "INBOX", index=False)

        leaked = [c for c in _CountingClient.instances if c._impl.connected]
        assert not leaked, f"同步结束后仍有 {len(leaked)} 条连接未断开"


class TestFetchPool:
    def test_same_thread_reuses_one_connection(self, context: AppContext, monkeypatch: pytest.MonkeyPatch) -> None:
        """threading.local 的意义：同线程重复取用只有一条连接。"""
        _install(monkeypatch, {})
        pool = sync_module._FetchPool(context.config, "code", context.cancel, 2)
        try:
            first = pool.client_pool.get()
            second = pool.client_pool.get()
            assert first is second
            assert pool.connections_created == 1
        finally:
            pool.close()

    def test_different_threads_get_separate_connections(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, {})
        import threading

        pool = sync_module._FetchPool(context.config, "code", context.cancel, 3)
        try:
            barrier = threading.Barrier(3)

            def worker() -> None:
                barrier.wait(timeout=5)
                pool.client_pool.get()

            threads = [threading.Thread(target=worker) for _ in range(3)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

            assert pool.connections_created == 3
        finally:
            pool.close()

    def test_close_is_idempotent(self, context: AppContext) -> None:
        pool = sync_module._FetchPool(context.config, "code", context.cancel, 2)
        pool.close()
        pool.close()  # 再关一次不应抛异常

    def test_close_disconnects_everything(
        self, context: AppContext, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install(monkeypatch, {})
        pool = sync_module._FetchPool(context.config, "code", context.cancel, 2)
        pool.client_pool.get()
        pool.close()
        assert all(not c._impl.connected for c in _CountingClient.instances)
