from __future__ import annotations

import pytest

from syvrio.infra.retrieval.matching import bigram_coverage, has_literal_bigrams
from syvrio.infra.security.secrets import contains_secret

pytestmark = pytest.mark.memory


@pytest.mark.parametrize(
    "text",
    [
        "我的密码是 demo-123456",
        "API key: test-placeholder",
        "api_key demo-123456",
        "access_token demo-token-placeholder",
        "password=!Demo1234",
        "password=@@!!",
        "验证码 458712请勿转发",
        "验证码458712，请勿转发",
        "密码：合成测试口令",
        "密码是 合成测试口令",
        "私钥: -----BEGIN PRIVATE KEY-----",
        "我的 API 密钥是 synthetic-token-1234",
        "秘钥：DEMO1234",
        "访问令牌为 demo_token",
        "凭据=ABCD1234",
    ],
)
def test_detects_secret_material(text):
    assert contains_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "喜欢清淡风味",
        "密码忘记了怎么办",
        "验证码一直收不到",
        "这个软件的密码管理器好用",
        "我忘了密码",
        "验证码收不到时需要联系客服",
        "密码 忘记了怎么办",
        "验证码 一直收不到",
        "密钥管理可以通过控制台完成",
        "访问令牌是临时凭据",
    ],
)
def test_allows_statements_about_credentials(text):
    assert not contains_secret(text)


def test_literal_bigrams_support_retrieval_without_comparing_fact_truth():
    assert has_literal_bigrams("预算")
    assert not has_literal_bigrams("预 1 算")
    assert bigram_coverage("租房预算", "租房预算是 5000") == 1
    assert bigram_coverage("合成资料", "合成信息") == pytest.approx(1 / 3)
    assert bigram_coverage("english", "english") == 0
