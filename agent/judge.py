"""研判器：给定规则命中、单据链特征与检索到的条款，输出 {verdict, confidence, cited, reasoning}。

- ClaudeJudge：调用 Anthropic API（需要 ANTHROPIC_API_KEY）。
- HeuristicJudge：离线启发式研判，无 API Key 时的兜底，便于本地演示和测试。
"""
import json
import os
import re

from p2p.rules import RULES

SYSTEM = """你是企业财务内控审计助手，负责研判采购到付款（P2P）流程中规则命中的疑似异常。
要求：
1. 只能依据给定的单据信息与“检索到的制度条款”作出判断，只能引用其中出现的条款编号；
2. 证据不足或存在合理例外（如合同预付款、分期付款、容差内差异）时，降低置信度或判为“不确定”；
3. 只输出一个 JSON 对象，不要输出其他内容：
{"verdict": "违规" | "不违规" | "不确定", "confidence": 0~1 的小数, "cited": ["P-xx"], "reasoning": "不超过 80 字的中文理由"}"""


class HeuristicJudge:
    name = "heuristic"

    def judge(self, hit, ctx, evidence):
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


class ClaudeJudge:
    name = "claude"

    def __init__(self, model=None):
        import anthropic
        self.client = anthropic.Anthropic()
        self.model = model or os.getenv("P2P_LLM_MODEL", "claude-sonnet-5-5")

    def judge(self, hit, ctx, evidence):
        ev = "\n".join(f"[{e['id']}] {e['title']}：{e['text']}" for e in evidence)
        user = (f"规则：{hit['rule']}（{RULES[hit['rule']]['name']}）\n命中说明：{hit['detail']}\n"
                f"涉及金额：{hit['amount']}\n单据链：{json.dumps(ctx['chain'], ensure_ascii=False)}\n"
                f"特征：{json.dumps(ctx.get('features', {}), ensure_ascii=False)}\n\n检索到的制度条款：\n{ev}")
        try:
            msg = self.client.messages.create(model=self.model, max_tokens=400, system=SYSTEM,
                                              messages=[{"role": "user", "content": user}])
            text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
            data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            return {"verdict": data.get("verdict", "不确定"),
                    "confidence": float(data.get("confidence", 0)),
                    "cited": list(data.get("cited", [])),
                    "reasoning": data.get("reasoning", ""), "judge": f"claude:{self.model}"}
        except Exception as e:  # 调用或解析失败一律转人工
            return {"verdict": "不确定", "confidence": 0.0, "cited": [],
                    "reasoning": f"LLM 研判失败：{type(e).__name__}", "judge": f"claude:{self.model}"}


def get_judge(kind="auto"):
    if kind == "heuristic":
        return HeuristicJudge()
    if kind == "claude" or (kind == "auto" and os.getenv("ANTHROPIC_API_KEY")):
        try:
            return ClaudeJudge()
        except ImportError:
            if kind == "claude":
                raise
    return HeuristicJudge()
