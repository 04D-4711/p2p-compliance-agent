"""长期记忆、上下文管理与 LangGraph 研判流程的测试。"""
import json

import pytest

import agent.memory as M
from agent import context as ctxmod
from agent.graph import route
from agent.memory import MemoryStore
from agent.pipeline import Agent
from evaluation import memory_eval
from p2p import cases, db, seed
from p2p.rules import run_rules

SIG_EQ = {"over_ratio": 0.0}       # R2：金额恰好等于权限（误报）
SIG_OVER = {"over_ratio": 0.3}     # R2：超权限 30%（异常）


LABELS = {}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    LABELS["x"] = seed.build(c)
    return c


@pytest.fixture
def store(conn):
    return MemoryStore(conn, version="test")


def test_only_human_reviews_are_written(store):
    with pytest.raises(ValueError):
        store.write_review("R2_AUTH", SIG_EQ, "不违规", source="llm")
    with pytest.raises(ValueError):
        store.write_review("R2_AUTH", SIG_EQ, "不确定")


def test_merge_and_consensus(store):
    first = store.write_review("R2_AUTH", SIG_EQ, "不违规", "等于权限上限", today="2026-03-01")
    assert first["action"] == "insert"
    assert store.recall("R2_AUTH", SIG_EQ, today="2026-03-01")["status"] == "weak"
    for _ in range(2):
        assert store.write_review("R2_AUTH", SIG_EQ, "不违规", today="2026-03-01")["action"] == "merge"
    r = store.recall("R2_AUTH", SIG_EQ, today="2026-03-01")
    assert r["status"] == "consensus" and r["vote"]["verdict"] == "不违规"
    assert len(store.list("precedent")) == 1  # 三次人审合并成一条，支持数 3
    # 差异明显的签名不受影响
    assert store.recall("R2_AUTH", SIG_OVER, today="2026-03-01")["status"] == "none"


def test_conflicting_reviews_force_human(store):
    for _ in range(3):
        store.write_review("R2_AUTH", SIG_EQ, "不违规", today="2026-03-01")
    res = store.write_review("R2_AUTH", SIG_EQ, "违规", "这一单其实越权", today="2026-03-02")
    assert res["conflicts"] == 1
    for _ in range(2):
        store.write_review("R2_AUTH", SIG_EQ, "违规", today="2026-03-02")
    assert store.recall("R2_AUTH", SIG_EQ, today="2026-03-02")["status"] == "conflict"
    assert any(e["action"] == "conflict" for e in store.events())


def test_revoke_and_policy_change(conn, store):
    for _ in range(3):
        mid = store.write_review("R2_AUTH", SIG_EQ, "不违规", today="2026-03-01")["id"]
    store.revoke(mid, "复核人撤销")
    assert store.recall("R2_AUTH", SIG_EQ)["status"] == "none"
    store.write_review("R2_AUTH", SIG_EQ, "不违规", today="2026-03-01")
    new = MemoryStore(conn, version="policy-v2")  # 制度修改
    assert new.recall("R2_AUTH", SIG_EQ)["status"] == "none"
    assert new.mark_stale() == 1


def test_decay_weakens_old_precedents(store):
    for _ in range(3):
        store.write_review("R2_AUTH", SIG_EQ, "不违规", today="2026-01-01")
    assert store.recall("R2_AUTH", SIG_EQ, today="2026-01-02")["status"] == "consensus"
    assert store.recall("R2_AUTH", SIG_EQ, today="2026-12-31")["status"] == "weak"  # 一年后支持数衰减到 3 以下


def test_capacity_eviction(store, monkeypatch):
    monkeypatch.setattr(M, "MAX_PER_RULE", 5)
    for i in range(8):
        store.write_review("R2_AUTH", {"over_ratio": i * 0.5}, "违规", today="2026-03-01")
    assert len(store.list("precedent")) == 5
    assert len(store.list("precedent", status="evicted")) == 3


def test_entity_facts_are_recalled(store):
    store.write_review("R4_DUPINV", {"same_number": False, "same_po": False}, "不违规", "月度固定服务费",
                       today="2026-02-10", fact=("vendor", "V14", "V14 为月度固定服务费合同"))
    r = store.recall("R4_DUPINV", {"same_number": False, "same_po": False}, {"vendor": "V14"})
    assert [f["note"] for f in r["facts"]] == ["V14 为月度固定服务费合同"]
    assert store.recall("R4_DUPINV", {}, {"vendor": "V01"})["facts"] == []


# ------------------------------------------------------------ 上下文管理
def _long_chain_hit():
    chain = {"po": {"id": "PO1", "requester_id": "U01", "buyer_id": "U04", "approver_id": "U07", "vendor_id": "V03",
                    "amount": 160000.0, "created_at": "2026-01-05"},
             "receipts": [{"id": f"GR{i}", "receiver_id": "U09", "amount": 4000.0, "received_at": f"2026-02-{i % 28 + 1:02d}"}
                          for i in range(40)],
             "invoices": [{"id": "IV1", "invoice_no": "INV-1", "amount": 160000.0, "invoice_date": "2026-01-15",
                           "status": "valid"}],
             "payments": [{"id": f"PY{i}", "invoice_id": "IV1", "amount": 4500.0, "paid_at": f"2026-03-{i % 28 + 1:02d}",
                           "status": "valid"} for i in range(36)]}
    hit = {"rule": "R6_PAY", "entity_type": "payment", "entity_id": "PY35", "amount": 4500.0, "detail": "累计付款超发票"}
    return hit, chain


def test_context_respects_budget_and_keeps_hit():
    hit, chain = _long_chain_hit()
    ev = [{"id": "P-06", "title": "付款前置条件", "text": "付款须在验收入库之后" * 5}]
    precs = [{"id": i, "similarity": 0.9, "support": 2, "verdict": "不违规", "note": "分期付款" * 10} for i in range(5)]
    c = ctxmod.build(hit, {"chain": chain, "features": {}}, ev, {"precedents": precs, "facts": []}, budget=400)
    assert c.tokens <= 400 < c.raw_tokens
    assert "PY35" in c.prompt and "IV1" in c.prompt and "[P-06]" in c.prompt
    assert c.steps[0] == "chain_summary"
    assert c.kept["precedents"] < 5  # 先例在条款之前被删减
    assert c.kept["clauses"] == 1


def test_context_unchanged_when_within_budget():
    hit, chain = _long_chain_hit()
    chain["receipts"], chain["payments"] = chain["receipts"][:2], chain["payments"][-1:]
    c = ctxmod.build(hit, {"chain": chain, "features": {}}, [], None, budget=5000)
    assert c.steps == [] and "GR0" in c.prompt and "GR1" in c.prompt


# ------------------------------------------------------------ LangGraph 流程与路由
class ExplodingJudge:
    name = "exploding"

    def judge(self, *a, **k):
        raise AssertionError("记忆已达成共识时不应调用研判器")


def _r2_hits(conn):
    return [h for h in run_rules(conn, "v1", ["R2_AUTH"]) if h["amount"] == 50000.0]


def test_consensus_skips_llm_and_auto_dismisses(conn, store):
    hits = _r2_hits(conn)
    assert len(hits) >= 4
    for _ in range(3):
        store.write_review("R2_AUTH", SIG_EQ, "不违规", "金额等于权限上限", today="2026-01-01")
    res = Agent(conn, ExplodingJudge(), store).analyse(hits[0], today="2026-01-02")
    assert res["judge"] == "memory" and res["route"] == "自动排除" and not res["llm_called"]


def test_high_amount_always_goes_to_human(conn, store):
    hit = next(h for h in run_rules(conn, "v1", ["R2_AUTH"]) if h["amount"] == 200000.0)
    for _ in range(3):
        store.write_review("R2_AUTH", SIG_EQ, "不违规", today="2026-01-01")
    res = Agent(conn, "heuristic", store).analyse(hit, today="2026-01-02")
    assert res["route"] == "人工复核" and "大额" in res["route_reasons"]


def test_route_reasons_for_memory_states():
    hit = {"amount": 1000}
    j = {"verdict": "违规", "confidence": 0.95, "cited": ["P-02"], "judge": "heuristic"}
    assert route(hit, j, ["P-02"], {"status": "conflict"})[1] == ["先例冲突"]
    assert route(hit, j, ["P-02"], {"status": "none"}, explore=True)[1] == ["无可靠先例"]
    assert route(hit, j, ["P-02"], {"status": "none"}, explore=False)[0] == "自动确认"


# ------------------------------------------------------------ 案件复核写入记忆
def test_case_review_writes_memory_and_correction(conn):
    agent = Agent(conn, "heuristic", True)
    res = [agent.analyse(h) for h in _r2_hits(conn)[:4]]
    for r in res:
        r["route"], r["route_reasons"] = "人工复核", ["测试"]
    cases.sync(conn, res)
    ids = [c["id"] for c in cases.list_cases(conn)]
    for cid in ids[:3]:
        cases.false_positive(conn, cid, "金额等于权限上限，权限为含")
    store = MemoryStore(conn)
    assert store.recall("R2_AUTH", SIG_EQ)["status"] == "consensus"
    # 第 4 单若被记忆自动排除，复核人仍可纠正，纠正结论同样写入记忆
    cases.confirm(conn, ids[3], "此单审批人权限已下调，属越权")
    c = cases.get(conn, ids[3])
    assert c["status"] == "整改中" and json.loads(c["memory_ids"])
    assert any("写入记忆" in e["action"] for e in cases.events(conn, ids[3]))


def test_memory_eval_smoke(conn):
    gold = {(l["rule"], l["entity_id"]) for l in LABELS["x"] if l["kind"] == "anomaly"}
    notes = {(l["rule"], l["entity_id"]): l["note"] for l in LABELS["x"]}
    hits = memory_eval.stream(conn, "v1")[:80]
    r = memory_eval.run_config(conn, hits, gold, notes, memory=True)
    assert r["n"] == 80 and r["human"] + r["auto"] + r["dismissed"] == 80
    assert r["dismissed_true"] == 0
    s = memory_eval.long_chain_stress()
    assert s["managed"] <= ctxmod.DEFAULT_BUDGET < s["raw"] and s["kept_hit"]


class CapturingJudge:
    name = "capture"

    def __init__(self):
        self.prompts = []

    def judge(self, hit, ctx, evidence, prompt=None):
        self.prompts.append(prompt)
        return {"verdict": "违规", "confidence": 0.9, "cited": [evidence[0]["id"]], "reasoning": "", "judge": self.name}


def test_llm_receives_precedents_in_managed_prompt(conn, store):
    store.write_review("R2_AUTH", SIG_EQ, "不违规", "权限为含，等于上限不算越权", today="2026-01-01")
    j = CapturingJudge()
    res = Agent(conn, j, store, explore=True).analyse(_r2_hits(conn)[0], today="2026-01-02")
    assert "历史人审先例" in j.prompts[0] and "权限为含" in j.prompts[0]
    assert res["route"] == "人工复核" and "无可靠先例" in res["route_reasons"]  # 只有 1 条先例，支持不足
