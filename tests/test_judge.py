"""用假客户端测试 ClaudeJudge 的解析与失败兜底（不发网络请求）。"""
from types import SimpleNamespace

import pytest

pytest.importorskip("anthropic")
from agent import pipeline
from agent.judge import ClaudeJudge


def _judge(reply=None, exc=None):
    j = ClaudeJudge.__new__(ClaudeJudge)
    j.model = "fake"

    def create(**_):
        if exc:
            raise exc
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])
    j.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    return j


HIT = {"rule": "R2_AUTH", "detail": "超权限", "amount": 60_000, "entity_id": "PO1"}
CTX = {"chain": {}, "features": {}}
EV = [{"id": "P-02", "title": "审批权限", "text": "..."}]


def test_parse_ok():
    out = _judge('好的：{"verdict":"违规","confidence":0.9,"cited":["P-02"],"reasoning":"超权限"}').judge(HIT, CTX, EV)
    assert out["verdict"] == "违规" and out["cited"] == ["P-02"]
    assert pipeline.route(HIT, out, ["P-02"]) == ("自动确认", [])


def test_hallucinated_clause_goes_to_human():
    out = _judge('{"verdict":"违规","confidence":0.95,"cited":["P-07"],"reasoning":"x"}').judge(HIT, CTX, EV)
    assert "引用证据外条款" in pipeline.route(HIT, out, ["P-02"])[1]


def test_failure_falls_back_to_human():
    out = _judge(exc=RuntimeError("timeout")).judge(HIT, CTX, EV)
    assert out["verdict"] == "不确定" and out["confidence"] == 0
    assert pipeline.route(HIT, out, ["P-02"])[0] == "人工复核"
