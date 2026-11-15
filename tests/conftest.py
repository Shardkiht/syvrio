from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from syvrio.infra.config import Config, LLMConfig, RetrievalConfig
from syvrio.memory.extractor import StaticExtractor
from syvrio.memory.models import Candidate, ExtractionResult
from syvrio.memory.service import MemoryService


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
            monkeypatch.setattr(
                module.questionary, name, lambda *args, **kwargs: FakeQuestion()
            )

    return _patch


@pytest.fixture(autouse=True)
def isolate_user_config(monkeypatch, tmp_path):
    """测试永远解析到测试自己的临时配置/记忆库/Runtime，任何代码路径都碰不到本机真实数据。

    起因：源码回退（突变验证）等场景下，写配置可能落到 default_config_path()，
    曾把 pytest 临时 vault 写进用户真实 config.toml。环境变量是最后一道闸。
    Runtime 隔离同理：锁文件、向量缓存等必须落进临时目录，不得写 ~/.syvrio/runtime。
    """
    monkeypatch.setenv("SYVRIO_CONFIG", str(tmp_path / "config.toml"))
    monkeypatch.setenv("SYVRIO_VAULT", str(tmp_path / "default-vault"))
    # Runtime 放在 tmp_path 的兄弟目录：不少测试直接把 tmp_path 当 vault，
    # 而 validate() 拒绝 Runtime 嵌套在 Vault 内部（V2 的边界守卫）。
    monkeypatch.setenv("SYVRIO_RUNTIME", str(tmp_path.with_name(tmp_path.name + "-runtime")))


@pytest.fixture(autouse=True)
def isolate_llm_credentials(monkeypatch):
    """测试禁止读取本机 keyring / 真实 Key，避免误调模型和产生费用。"""
    fake = types.ModuleType("keyring")
    fake.get_password = lambda service, name: None
    fake.set_password = lambda service, name, value: None
    monkeypatch.setitem(sys.modules, "keyring", fake)
    for name in ("DEEPSEEK_API_KEY", "SILICONFLOW_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY", "SPARK_API_KEY"):
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


def extractor(*candidates: Candidate) -> StaticExtractor:
    return StaticExtractor(ExtractionResult(memories=list(candidates)))
