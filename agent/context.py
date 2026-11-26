"""上下文管理：在 token 预算内为研判器组装提示词。

内容按优先级分段：
  必选  命中说明、特征、首条制度条款、命中单据本身
  可压  单据链（字段裁剪 → 长列表摘要 → 只留命中单据与汇总）
  可删  其余条款、历史先例、实体事实（按相关度从低到高删）
超预算时按 DEGRADE 顺序逐级降级，并记录用了哪些步骤，便于排查“模型为什么没看到某条信息”。

token 数用字符估算（中文约 1 字 1 token，其他字符约 3.5 字符 1 token），
用于预算控制足够；需要精确值时可用 Anthropic 的 count_tokens 接口校准。
"""
import json
import re
from dataclasses import dataclass, field

from p2p.rules import RULES

DEFAULT_BUDGET = 1200
MAX_LIST_ITEMS = 5

FIELDS = {
    "po": [("id", ""), ("requester_id", "申请人"), ("buyer_id", "采购员"), ("approver_id", "审批人"),
           ("vendor_id", "供应商"), ("amount", "金额"), ("created_at", "日期")],
    "receipts": [("id", ""), ("receiver_id", "收货人"), ("amount", "金额"), ("received_at", "日期")],
    "invoices": [("id", ""), ("invoice_no", "号码"), ("amount", "金额"), ("invoice_date", "日期"), ("status", "状态")],
    "payments": [("id", ""), ("invoice_id", "发票"), ("amount", "金额"), ("paid_at", "日期"), ("status", "状态")],
}
LABEL = {"po": "订单", "receipts": "收货", "invoices": "发票", "payments": "付款"}
DATE = {"receipts": "received_at", "invoices": "invoice_date", "payments": "paid_at"}
_CJK = re.compile(r"[　-〿一-鿿＀-￯]")


def estimate_tokens(text):
    cjk = len(_CJK.findall(text))
    return int(cjk + (len(text) - cjk) / 3.5) + 1


def _line(kind, row):
    parts = []
    for k, lab in FIELDS[kind]:
        v = row.get(k)
        if v is None or (k == "status" and v == "valid"):
            continue
        if isinstance(v, float):
            v = f"{v:.2f}"
        parts.append(f"{lab} {v}" if lab else str(v))
    return f"{LABEL[kind]} " + " ".join(parts)


def _pinned(hit, chain):
    """必须完整保留的单据：命中单据本身，以及付款命中对应的发票。"""
    ids = {hit["entity_id"]}
    for p in chain.get("payments", []):
        if p["id"] == hit["entity_id"]:
            ids.add(p["invoice_id"])
    return ids


def render_chain(hit, chain, level):
    """level 1：字段裁剪；2：长列表摘要；3：只留命中单据 + 各类汇总。"""
    pins = _pinned(hit, chain)
    out = [_line("po", chain["po"])] if chain.get("po") else []
    for kind in ("receipts", "invoices", "payments"):
        items = chain.get(kind, [])
        if not items:
            out.append(f"{LABEL[kind]}：无")
            continue
        keep = items
        if level >= 3:
            keep = [x for x in items if x["id"] in pins]
        elif level >= 2 and len(items) > MAX_LIST_ITEMS:
            keep = [x for i, x in enumerate(items) if x["id"] in pins or i in (0, len(items) - 1)]
        out += [_line(kind, x) for x in keep]
        rest = [x for x in items if x not in keep]
        if rest:
            ds = sorted(x[DATE[kind]] for x in rest)
            out.append(f"……另有{LABEL[kind]} {len(rest)} 条，合计 {sum(x['amount'] for x in rest):.2f}，"
                       f"日期 {ds[0]} ~ {ds[-1]}")
        if level >= 3 or len(items) > 1:
            out.append(f"（{LABEL[kind]}共 {len(items)} 条，合计 {sum(x['amount'] for x in items):.2f}）")
    return "\n".join(out)


def _precedent(m):
    src = f"{int(m['support'])} 次人审" if m["support"] > 1 else "1 次人审"
    note = f"；复核意见：{m['note']}" if m.get("note") else ""
    return f"- 先例#{m['id']}（相似度 {m['similarity']:.2f}，{src}）→ {m['verdict']}{note}"


@dataclass
class Context:
    prompt: str
    tokens: int
    raw_tokens: int
    steps: list = field(default_factory=list)
    kept: dict = field(default_factory=dict)


def raw_prompt(hit, ctx, evidence):
    """旧版提示词：完整 JSON 单据链（作为对比基线）。"""
    ev = "\n".join(f"[{e['id']}] {e['title']}：{e['text']}" for e in evidence)
    return (f"规则：{hit['rule']}（{RULES[hit['rule']]['name']}）\n命中说明：{hit['detail']}\n"
            f"涉及金额：{hit['amount']}\n单据链：{json.dumps(ctx['chain'], ensure_ascii=False)}\n"
            f"特征：{json.dumps(ctx.get('features', {}), ensure_ascii=False)}\n\n检索到的制度条款：\n{ev}")


DEGRADE = ["chain_summary", "precedents_3", "facts_1", "clause_trim", "precedents_1",
           "clauses_1", "facts_0", "precedents_0", "chain_minimal"]


def build(hit, ctx, evidence, memory=None, budget=DEFAULT_BUDGET):
    memory = memory or {}
    st = {"chain_level": 1, "n_prec": 5, "n_facts": 3, "clause_len": None, "n_clauses": len(evidence)}

    def render():
        precs = (memory.get("precedents") or [])[:st["n_prec"]]
        facts = (memory.get("facts") or [])[:st["n_facts"]]
        clauses = evidence[:st["n_clauses"]]
        cl = [f"[{e['id']}] {e['title']}：" + (e["text"][:st["clause_len"]] + "…"
              if st["clause_len"] and len(e["text"]) > st["clause_len"] else e["text"]) for e in clauses]
        parts = [f"规则：{hit['rule']}（{RULES[hit['rule']]['name']}）",
                 f"命中说明：{hit['detail']}", f"涉及金额：{hit['amount']}",
                 "特征：" + json.dumps(ctx.get("features", {}), ensure_ascii=False),
                 "", "单据链：", render_chain(hit, ctx["chain"], st["chain_level"]),
                 "", "检索到的制度条款：", *cl]
        if precs:
            parts += ["", "历史人审先例（仅供参考，与本单据事实不一致时以单据和条款为准）：", *map(_precedent, precs)]
        if facts:
            parts += ["", "已登记的实体事实：", *[f"- {f['scope_type']} {f['scope_id']}：{f['note']}" for f in facts]]
        text = "\n".join(parts)
        return text, {"precedents": len(precs), "facts": len(facts), "clauses": len(clauses),
                      "chain_level": st["chain_level"]}

    steps = []
    text, kept = render()
    for step in DEGRADE:
        if estimate_tokens(text) <= budget:
            break
        if step == "chain_summary":
            st["chain_level"] = 2
        elif step.startswith("precedents_"):
            st["n_prec"] = min(st["n_prec"], int(step[-1]))
        elif step.startswith("facts_"):
            st["n_facts"] = min(st["n_facts"], int(step[-1]))
        elif step == "clause_trim":
            st["clause_len"] = 60
        elif step == "clauses_1":
            st["n_clauses"] = min(st["n_clauses"], 1)
        elif step == "chain_minimal":
            st["chain_level"] = 3
        steps.append(step)
        text, kept = render()
    return Context(text, estimate_tokens(text), estimate_tokens(raw_prompt(hit, ctx, evidence)), steps, kept)
