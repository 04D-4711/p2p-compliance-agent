"""用 LangGraph 编排单条命中的研判流程。

    gather ─→ recall ─┬─(先例达成共识且非大额)─→ memory_judge ─┐
                      └────────────→ build_context ─→ llm_judge ─┴─→ route ─→ END

- gather：追溯单据链、提取特征、检索制度条款
- recall：从长期记忆检索相似人审先例和实体事实（未启用记忆时跳过）
- memory_judge：相似先例足够多且结论一致时，直接按记忆研判，不调用 LLM（省成本）
- build_context：在 token 预算内组装提示词（先例、事实、压缩后的单据链）
- llm_judge：调用研判器（Claude / Claude Code / 启发式）
- route：人审路由

LangGraph 不可用时（未安装，或 Windows 智能应用控制拦截了 xxhash 等依赖的 DLL），
自动改用 agent/minigraph.py 里同样接口的顺序执行器，节点与边的定义不变。
"""
from typing import Any, Optional, TypedDict

try:
    from langgraph.graph import END, StateGraph
    BACKEND = "langgraph"
except Exception:  # 未安装，或 Windows 智能应用控制拦截了依赖的 DLL（如 xxhash）
    from .minigraph import END, StateGraph
    BACKEND = "minigraph"

from p2p import config, db
from p2p.rules import RULES

from . import context as ctxmod
from .features import extract, signature
from .retriever import retrieve


class State(TypedDict, total=False):
    hit: dict
    today: Optional[str]
    chain: dict
    features: dict
    evidence: list
    memory: dict
    context: Any
    judgment: dict
    route: str
    route_reasons: list
    llm_called: bool


def entity_date(hit, chain):
    key = {"po": ("po", "created_at"), "invoice": ("invoices", "invoice_date"), "payment": ("payments", "paid_at")}
    k, f = key.get(hit["entity_type"], ("po", "created_at"))
    if k == "po":
        return chain["po"][f]
    return next((x[f] for x in chain[k] if x["id"] == hit["entity_id"]), chain["po"]["created_at"])


def route(hit, judgment, evidence_ids, memory=None, explore=False):
    """返回 (路由, 原因列表)。路由：自动确认 / 自动排除 / 人工复核。"""
    reasons = []
    if hit["amount"] >= config.HIGH_AMOUNT:
        reasons.append("大额")
    if judgment["confidence"] < config.CONF_THRESHOLD:
        reasons.append("低置信")
    if not judgment["cited"]:
        reasons.append("未引用条款")
    elif not set(judgment["cited"]) <= set(evidence_ids):
        reasons.append("引用证据外条款")
    from_memory = judgment.get("judge") == "memory"
    if memory is not None and not from_memory:
        if memory["status"] == "conflict":
            reasons.append("先例冲突")
        elif explore and memory["status"] in ("none", "weak"):
            reasons.append("无可靠先例")
    if judgment["verdict"] == "不违规" and from_memory:
        return ("人工复核", reasons) if reasons else ("自动排除", [])
    if judgment["verdict"] != "违规":
        reasons.append(f"研判为{judgment['verdict']}")
    return ("人工复核" if reasons else "自动确认"), reasons


def build_graph(conn, judge, store=None, budget=ctxmod.DEFAULT_BUDGET, explore=False):
    def gather(s):
        hit = s["hit"]
        chain = db.chain_for(conn, hit["entity_type"], hit["entity_id"])
        meta = RULES[hit["rule"]]
        evidence = retrieve(f"{meta['name']} {meta['desc']} {hit['detail']}", k=3)
        return {"chain": chain, "features": extract(conn, hit, chain), "evidence": evidence,
                "today": s.get("today") or entity_date(hit, chain)}

    def recall(s):
        if store is None:
            return {"memory": None}
        po = s["chain"]["po"] or {}
        ents = {"vendor": po.get("vendor_id"), "user": po.get("requester_id")}
        return {"memory": store.recall(s["hit"]["rule"], signature(s["hit"]["rule"], s["features"]),
                                       ents, today=s["today"])}

    def after_recall(s):
        m = s.get("memory")
        if m and m["status"] == "consensus" and s["hit"]["amount"] < config.HIGH_AMOUNT:
            return "memory_judge"
        return "build_context"

    def memory_judge(s):
        m, hit = s["memory"], s["hit"]
        ev_ids = [e["id"] for e in s["evidence"]]
        cited = [c for c in RULES[hit["rule"]]["clauses"] if c in ev_ids] or ev_ids[:1]
        ids = "、".join(f"#{p['id']}" for p in m["precedents"][:3])
        return {"judgment": {"verdict": m["vote"]["verdict"], "confidence": m["vote"]["agreement"],
                             "cited": cited, "judge": "memory",
                             "reasoning": f"依据相似人审先例 {ids}（有效支持 {m['vote']['support']}，"
                                          f"一致度 {m['vote']['agreement']:.2f}）"},
                "llm_called": False}

    def build_context(s):
        c = ctxmod.build(s["hit"], {"chain": s["chain"], "features": s["features"]}, s["evidence"],
                         s.get("memory"), budget)
        return {"context": c}

    def llm_judge(s):
        j = judge.judge(s["hit"], {"chain": s["chain"], "features": s["features"]}, s["evidence"],
                        prompt=s["context"].prompt)
        return {"judgment": j, "llm_called": judge.name != "heuristic"}

    def do_route(s):
        r, reasons = route(s["hit"], s["judgment"], [e["id"] for e in s["evidence"]], s.get("memory"), explore)
        return {"route": r, "route_reasons": reasons}

    g = StateGraph(State)
    for name, fn in (("gather", gather), ("recall", recall), ("memory_judge", memory_judge),
                     ("build_context", build_context), ("llm_judge", llm_judge), ("route", do_route)):
        g.add_node(name, fn)
    g.set_entry_point("gather")
    g.add_edge("gather", "recall")
    g.add_conditional_edges("recall", after_recall, {"memory_judge": "memory_judge", "build_context": "build_context"})
    g.add_edge("build_context", "llm_judge")
    g.add_edge("memory_judge", "route")
    g.add_edge("llm_judge", "route")
    g.add_edge("route", END)
    return g.compile()


def to_result(s):
    hit, j, c = s["hit"], s["judgment"], s.get("context")
    m = s.get("memory")
    return {**hit, "rule_name": RULES[hit["rule"]]["name"], "evidence": s["evidence"], "features": s["features"],
            **j, "route": s["route"], "route_reasons": s["route_reasons"], "today": s["today"],
            "llm_called": s.get("llm_called", False),
            "memory_status": m["status"] if m else None,
            "memory_refs": [p["id"] for p in m["precedents"]] if m else [],
            "memory_facts": [f["note"] for f in m["facts"]] if m else [],
            "prompt_tokens": c.tokens if c else 0, "raw_prompt_tokens": c.raw_tokens if c else 0,
            "context_steps": c.steps if c else []}
