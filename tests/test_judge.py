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


# ---------------- ClaudeCodeJudge：用假的 claude 可执行文件走真实 subprocess 路径 ----------------
import json
import os
import stat

from agent.judge import ClaudeCodeJudge, get_judge


posix_only = pytest.mark.skipif(os.name == "nt", reason="假 CLI 用 shebang 脚本，仅在 macOS/Linux 上运行")


def _fake_cli(tmp_path, body):
    p = tmp_path / "claude"
    p.write_text("#!/usr/bin/env python3\nimport sys, json\nprompt = sys.stdin.read()\nargs = sys.argv[1:]\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


@posix_only
def test_claude_code_judge_parses_wrapper(tmp_path):
    cli = _fake_cli(tmp_path, '''
assert args[:3] == ["-p", "--output-format", "json"] and "--model" in args
verdict = "不违规" if "50000.0" in prompt else "违规"
inner = json.dumps({"verdict": verdict, "confidence": 0.88, "cited": ["P-02"], "reasoning": "依据权限"}, ensure_ascii=False)
print(json.dumps({"type": "result", "is_error": False, "result": "```json\\n" + inner + "\\n```"}))
''')
    j = ClaudeCodeJudge(model="sonnet", cli=cli)
    assert j.name == "claude-code:sonnet"
    out = j.judge(HIT, CTX, EV)
    assert out["verdict"] == "违规" and out["cited"] == ["P-02"] and out["confidence"] == 0.88


@posix_only
def test_claude_code_judge_error_goes_to_human(tmp_path):
    cli = _fake_cli(tmp_path, 'print(json.dumps({"type":"result","is_error":True,"result":"usage limit"}))')
    out = ClaudeCodeJudge(cli=cli).judge(HIT, CTX, EV)
    assert out["verdict"] == "不确定" and pipeline.route(HIT, out, ["P-02"])[0] == "人工复核"
    cli2 = _fake_cli(tmp_path, 'sys.exit(3)')
    assert ClaudeCodeJudge(cli=cli2).judge(HIT, CTX, EV)["confidence"] == 0


def test_missing_cli_raises():
    with pytest.raises(FileNotFoundError):
        ClaudeCodeJudge(cli="definitely-not-a-cli-xyz")


@posix_only
def test_get_judge_specs(tmp_path, monkeypatch):
    monkeypatch.setenv("P2P_CLAUDE_CLI", _fake_cli(tmp_path, "print('{}')"))
    assert get_judge("claude-code:opus").name == "claude-code:opus"
    assert get_judge("heuristic").name == "heuristic"


@posix_only
def test_judge_eval_end_to_end(tmp_path, monkeypatch):
    """研判器对比流程：假 CLI 对“金额等于权限上限”判不违规，应比启发式基线精确率更高。"""
    from evaluation import judge_eval
    from p2p import config
    monkeypatch.setattr(config, "REPORT_DIR", tmp_path)
    cli = _fake_cli(tmp_path, """
i = prompt.index("[P-")
cid = prompt[i + 1:i + 5]
ok = "R2_AUTH" in prompt and ("权限 50000，订单金额 50000.0" in prompt or "权限 200000，订单金额 200000.0" in prompt)
v = "不违规" if ok else "违规"
print(json.dumps({"type": "result", "is_error": False,
                  "result": json.dumps({"verdict": v, "confidence": 0.9, "cited": [cid], "reasoning": "t"})}))
""")
    monkeypatch.setenv("P2P_CLAUDE_CLI", cli)
    res = judge_eval.main(["--judges", "heuristic", "claude-code", "--limit", "0", "--candidates", "v1"])
    base, cc = res
    assert base["precision"] < cc["precision"] and cc["fail_rate"] == 0
    assert list(tmp_path.glob("judge_eval_*.md"))
