"""研判器：给定规则命中、单据链特征与检索到的条款，输出 {verdict, confidence, cited, reasoning}。

- ClaudeJudge：调用 Anthropic API（需要 ANTHROPIC_API_KEY）。
- ClaudeCodeJudge：调用本机 Claude Code CLI（claude -p），使用已登录的订阅账户。
- HeuristicJudge：离线启发式研判，无 API Key 时的兜底，便于本地演示和测试。
"""
import json
import os
import re
import shutil
import subprocess

from p2p.rules import RULES

SYSTEM = """你是企业财务内控审计助手，负责研判采购到付款（P2P）流程中规则命中的疑似异常。
要求：
1. 只能依据给定的单据信息与“检索到的制度条款”作出判断，只能引用其中出现的条款编号；
2. 证据不足或存在合理例外（如合同预付款、分期付款、容差内差异）时，降低置信度或判为“不确定”；
3. 如提供了“历史人审先例”和“实体事实”，可参考相似先例的结论，但本单据事实与先例不一致时以单据和条款为准；
4. 只输出一个 JSON 对象，不要输出其他内容：
{"verdict": "违规" | "不违规" | "不确定", "confidence": 0~1 的小数, "cited": ["P-xx"], "reasoning": "不超过 80 字的中文理由"}"""


class HeuristicJudge:
    name = "heuristic"

    def judge(self, hit, ctx, evidence, prompt=None):
        rule, f = hit["rule"], ctx.get("features", {})
        conf = {"R1_SOD": 0.95, "R2_AUTH": 0.92, "R3_SPLIT": 0.85,
                "R4_DUPINV": 0.93, "R5_3WAY": 0.9, "R6_PAY": 0.9}[rule]
        notes = []
        if rule == "R2_AUTH" and f.get("over_ratio", 1) < 0.05:
            conf = 0.7; notes.append("超权限幅度不足 5%")
        if rule == "R5_3WAY" and f.get("deviation", 1) < 0.08:
            conf = 0.7; notes.append("偏差接近 5% 容差")
        if rule == "R4_DUPINV" and not f.get("same_number"):
            conf = 0.72; notes.append("号码不同，仅同订单同金额")
        if rule == "R6_PAY" and any(e["id"] == "P-12" for e in evidence):
            conf = min(conf, 0.8); notes.append("检索到预付款条款，需核对合同")
        ev_ids = [e["id"] for e in evidence]
        mapped = [c for c in RULES[rule]["clauses"] if c in ev_ids]
        cited = mapped or ev_ids[:1]
        reasoning = f"{RULES[rule]['name']}：{hit['detail']}" + (f"（{'；'.join(notes)}）" if notes else "")
        return {"verdict": "违规", "confidence": conf, "cited": cited, "reasoning": reasoning, "judge": self.name}


def build_prompt(hit, ctx, evidence):
    """未经上下文管理的提示词（完整单据链 JSON），仅在调用方没有传入 prompt 时使用。"""
    from .context import raw_prompt
    return raw_prompt(hit, ctx, evidence)


def parse_reply(text, judge_name):
    """从模型回复中取出 JSON；任何解析问题都抛异常，由调用方兜底。"""
    data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
    verdict = data.get("verdict", "不确定")
    if verdict not in ("违规", "不违规", "不确定"):
        verdict = "不确定"
    return {"verdict": verdict, "confidence": max(0.0, min(1.0, float(data.get("confidence", 0)))),
            "cited": [str(c) for c in data.get("cited", [])],
            "reasoning": str(data.get("reasoning", "")), "judge": judge_name}


def failed(judge_name, e):
    return {"verdict": "不确定", "confidence": 0.0, "cited": [],
            "reasoning": f"LLM 研判失败：{type(e).__name__} {str(e)[:80]}", "judge": judge_name}


class ClaudeJudge:
    """通过 Anthropic API 研判（需要 ANTHROPIC_API_KEY，按量计费）。"""

    def __init__(self, model=None):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("P2P_LLM_MODEL", "claude-sonnet-5-5")

    @property
    def name(self):
        return f"api:{self.model}"

    def judge(self, hit, ctx, evidence, prompt=None):
        try:
            msg = self.client.messages.create(model=self.model, max_tokens=400, system=SYSTEM,
                                              messages=[{"role": "user", "content": prompt or build_prompt(hit, ctx, evidence)}])
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            return parse_reply(text, self.name)
        except Exception as e:  # 调用或解析失败一律转人工
            return failed(self.name, e)


class ClaudeCodeJudge:
    """通过本机 Claude Code CLI（claude -p）研判，使用已登录的订阅账户，无需 API Key。

    model 可用别名（sonnet / opus / haiku）或完整模型名；为空则用 CLI 默认模型。
    """

    def __init__(self, model=None, cli=None, timeout=180):
        self.model = model or os.getenv("P2P_CC_MODEL") or None
        self.cli = cli or os.getenv("P2P_CLAUDE_CLI", "claude")
        self.timeout = timeout
        # 用完整路径：Windows 上 npm 安装的是 claude.cmd，只写 "claude" 时 subprocess 找不到
        self.path = shutil.which(self.cli)
        if not self.path:
            raise FileNotFoundError(f"找不到 Claude Code 命令行：{self.cli}")

    @property
    def name(self):
        return f"claude-code:{self.model or 'default'}"

    def judge(self, hit, ctx, evidence, prompt=None):
        prompt = SYSTEM + "\n\n" + (prompt or build_prompt(hit, ctx, evidence))
        cmd = [self.path, "-p", "--output-format", "json"]
        if self.model:
            cmd += ["--model", self.model]
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=self.timeout)
            if proc.returncode != 0:
                raise RuntimeError(proc.stderr.strip() or f"exit {proc.returncode}")
            out = proc.stdout.strip()
            try:  # --output-format json 外层包装：{"type":"result","result":"<模型回复>",...}
                wrapper = json.loads(out)
                if isinstance(wrapper, dict) and "result" in wrapper:
                    if wrapper.get("is_error"):
                        raise RuntimeError(str(wrapper["result"])[:120])
                    out = wrapper["result"]
            except json.JSONDecodeError:
                pass
            return parse_reply(out, self.name)
        except Exception as e:
            return failed(self.name, e)


def get_judge(kind="auto"):
    """kind：auto | heuristic | api[:模型] | claude（同 api）| claude-code[:模型]"""
    base, _, model = kind.partition(":")
    if base == "heuristic":
        return HeuristicJudge()
    if base in ("api", "claude"):
        return ClaudeJudge(model or None)
    if base == "claude-code":
        return ClaudeCodeJudge(model or None)
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            return ClaudeJudge()
        except ImportError:
            pass
    return HeuristicJudge()
