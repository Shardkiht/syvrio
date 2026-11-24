from __future__ import annotations

import asyncio
import os
import sys
import types
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from syvrio.infra.config import Config, LLMConfig, RetrievalConfig
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import Candidate, ExtractionResult
from syvrio.memory.service import MemoryService


def _pipe_loop_factory() -> asyncio.AbstractEventLoop:
    import _winapi
    from asyncio import windows_utils

    class PipeLoop(asyncio.ProactorEventLoop):
        def _make_self_pipe(self) -> None:
            reader, writer = windows_utils.pipe(overlapped=(True, False))
            self._ssock = windows_utils.PipeHandle(reader)
            self._csock = windows_utils.PipeHandle(writer)
            self._internal_fds += 1

        def _write_to_self(self) -> None:
            writer = self._csock
            if writer is not None:
                try:
                    _winapi.WriteFile(writer.fileno(), b"\0")
                except OSError:
                    if not self.is_closed():
                        raise

    # Windows 的 signal 唤醒只支持 socket；在辅助线程构造以跳过信号注册。
    # 仍在测试主线程运行正常 Proactor，只把线程唤醒改为本地管道。
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(PipeLoop).result()


def pytest_configure(config: pytest.Config) -> None:
    if sys.platform == "win32" and os.environ.get("SYVRIO_TEST_PIPE_LOOP") == "1":

        class PipeLoopPolicy(asyncio.WindowsProactorEventLoopPolicy):
            def new_event_loop(self) -> asyncio.AbstractEventLoop:
                return _pipe_loop_factory()

        # 同步测试中的 asyncio.run()（如 questionary）也需要使用同一唤醒机制。
        asyncio.set_event_loop_policy(PipeLoopPolicy())


class _FakeControl:
    def get_pointed_at(self):
        return SimpleNamespace(value=None)


class _FakeLayout:
    def find_all_controls(self):
        return [_FakeControl()]


class _FakeApplication:
    def __init__(self):
        self.key_bindings = None
        self.layout = _FakeLayout()


class FakeQuestion:
    """替代 questionary 控件，避免测试环境尝试创建终端。"""

    def __init__(self):
        self.application = _FakeApplication()

    def ask(self, kbi_msg=""):
        return None


@pytest.fixture
def patch_questionary(monkeypatch):
    def _patch(module):
        for name in ("select", "text", "password", "confirm"):
            monkeypatch.setattr(module.questionary, name, lambda *args, **kwargs: FakeQuestion())

    return _patch


@pytest.fixture(autouse=True)
def isolate_user_config(monkeypatch, tmp_path, tmp_path_factory):
    """测试永远解析到测试自己的临时配置/记忆库，任何代码路径都碰不到本机真实配置。

    起因：源码回退（突变验证）等场景下，写配置可能落到 default_config_path()，
    曾把 pytest 临时 vault 写进用户真实 config.toml。环境变量是最后一道闸。
    """
    (tmp_path / "config.toml").touch()
    monkeypatch.setenv("SYVRIO_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("SYVRIO_VAULT", str(tmp_path / "default-vault"))
    monkeypatch.setenv("SYVRIO_RUNTIME", str(tmp_path_factory.mktemp("runtime")))


@pytest.fixture(autouse=True)
def isolate_llm_credentials(monkeypatch, tmp_path):
    """测试禁止读取本机 keyring / 真实 Key，避免误调模型和产生费用。"""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.delenv("SYVRIO_CREDENTIALS_DIR", raising=False)
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, name: None
    fake.set_password = lambda service, name, value: None
    monkeypatch.setitem(sys.modules, "keyring", fake)
    for name in (
        "DEEPSEEK_API_KEY",
        "SILICONFLOW_API_KEY",
        "DASHSCOPE_API_KEY",
        "QIANWEN_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "SPARK_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def config(tmp_path):
    # 测试默认 keyword，避免依赖 fastembed 是否安装。
    return Config(
        vault=tmp_path / "vault",
        llm=LLMConfig(),
        retrieval=RetrievalConfig(method="keyword"),
    )


@pytest.fixture
def service(config):
    return MemoryService(config)


@pytest.fixture
def resident_backend(service):
    """使用终端的常规 HTTP 客户端访问临时常驻服务。"""
    import secrets
    import socket
    import threading
    import time
    from uuid import uuid4

    import uvicorn

    from syvrio.app.web.backend import WebBackend
    from syvrio.app.web.server import create_app
    from syvrio.backend.client import ResidentClient

    service.config.search.auto_start = False
    service.config.git.auto_sync = False
    service.init_vault()
    owner = WebBackend(service=service)
    token = secrets.token_urlsafe(24)
    api = create_app(owner, resident_token=token)
    server = uvicorn.Server(uvicorn.Config(api, access_log=False, log_level="error"))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"

        class Client(ResidentClient):
            def __init__(self):
                self.config, self.paths = service.config, service.paths
                self.base, self.token, self.view = base, token, uuid4().hex

        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield owner, Client()
        finally:
            server.should_exit = True
            thread.join(15)
            assert not thread.is_alive()


def extractor(*candidates: Candidate) -> StaticExtractor:
    return StaticExtractor(ExtractionResult(memories=list(candidates)))
