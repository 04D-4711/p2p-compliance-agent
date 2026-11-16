import zipfile
import io

import pytest

from agent import pipeline
from agent.judge import HeuristicJudge
from p2p import cases, db, precheck, seed
from p2p.rules import RULES, hit_keys, run_rules


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    labels = seed.build(c)
    yield c, labels
    c.close()


def test_norm_inv():
    assert db.norm_inv("INV-2026-00123") == db.norm_inv("inv 2026 0123") == db.norm_inv("INV2026123")
    assert db.norm_inv("INV-2026-00123") != db.norm_inv("INV-2026-00123R")


def test_v2_has_no_false_positive_on_labels(conn):
    c, labels = conn
    gold = {(l["rule"], l["entity_id"]) for l in labels if l["kind"] == "anomaly"}
    assert len(gold) == 90
    assert hit_keys(run_rules(c, "v2")) <= gold


def _po(**kw):
    base = dict(requester_id="U01", buyer_id="U04", approver_id="U06", vendor_id="V01",
                item="测试", qty=1, unit_price=10_000, created_at="2026-12-01")
    base.update(kw)
    return base


def test_precheck_po(conn):
    c, _ = conn
    assert precheck.submit(c, "po", _po())["saved"]
    r = precheck.submit(c, "po", _po(requester_id="U06"))
    assert r["blocked"] and r["violations"][0]["rule"] == "R1_SOD"
    r = precheck.submit(c, "po", _po(unit_price=60_000))
    assert r["blocked"] and r["violations"][0]["rule"] == "R2_AUTH"
    assert r["violations"][0]["clauses"][0]["id"] == "P-02"
    # 拆单：已有 1 万，再来两笔 3 万 → 合计 7 万 > 5 万
    assert precheck.submit(c, "po", _po(unit_price=30_000, created_at="2026-12-02"))["saved"]
    r = precheck.submit(c, "po", _po(unit_price=30_000, created_at="2026-12-03"))
    assert r["blocked"] and {v["rule"] for v in r["violations"]} == {"R3_SPLIT"}


def test_precheck_invoice_and_payment(conn):
    c, _ = conn
    po = precheck.submit(c, "po", _po(vendor_id="V02"))["id"]
    assert precheck.submit(c, "receipt", dict(po_id=po, receiver_id="U04", qty=1, amount=10_000,
                                              received_at="2026-12-05"))["blocked"]   # 采购员自收货
    assert precheck.submit(c, "receipt", dict(po_id=po, receiver_id="U09", qty=1, amount=10_000,
                                              received_at="2026-12-05"))["saved"]
    r = precheck.submit(c, "invoice", dict(po_id=po, vendor_id="V02", invoice_no="INV-2026-99001",
                                           amount=11_000, invoice_date="2026-12-06", entered_by="U11"))
    assert r["blocked"] and r["violations"][0]["rule"] == "R5_3WAY"
    iv = precheck.submit(c, "invoice", dict(po_id=po, vendor_id="V02", invoice_no="INV-2026-99001",
                                            amount=10_200, invoice_date="2026-12-06", entered_by="U11"))
    assert iv["saved"]   # 2% 容差内
    r = precheck.submit(c, "invoice", dict(po_id=po, vendor_id="V02", invoice_no="inv 2026 99001",
                                           amount=10_200, invoice_date="2026-12-08", entered_by="U11"))
    assert r["blocked"] and "R4_DUPINV" in {v["rule"] for v in r["violations"]}
    r = precheck.submit(c, "payment", dict(invoice_id=iv["id"], amount=10_200, paid_at="2026-12-01", paid_by="U12"))
    assert r["blocked"] and r["violations"][0]["rule"] == "R6_PAY"
    assert precheck.submit(c, "payment", dict(invoice_id=iv["id"], amount=4_000, paid_at="2026-12-10", paid_by="U12"))["saved"]
    assert precheck.submit(c, "payment", dict(invoice_id=iv["id"], amount=7_000, paid_at="2026-12-20", paid_by="U12"))["blocked"]


def test_dry_run_does_not_write(conn):
    c, _ = conn
    n = c.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]
    assert precheck.submit(c, "po", _po(), dry_run=True)["saved"] is False
    assert c.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == n


def test_retrieval_finds_mapped_clause(conn):
    c, _ = conn
    for r in pipeline.run(c, "v2", HeuristicJudge()):
        assert RULES[r["rule"]]["clauses"][0] in [e["id"] for e in r["evidence"]]


def test_routing():
    hit = {"amount": 120_000}
    ok = {"verdict": "违规", "confidence": 0.9, "cited": ["P-02"]}
    assert pipeline.route(hit, ok, ["P-02"])[1] == ["大额"]
    small = {"amount": 1_000}
    assert pipeline.route(small, ok, ["P-02"]) == ("自动确认", [])
    assert "低置信" in pipeline.route(small, {**ok, "confidence": 0.5}, ["P-02"])[1]
    assert "引用证据外条款" in pipeline.route(small, {**ok, "cited": ["P-99"]}, ["P-02"])[1]
    assert "研判为不确定" in pipeline.route(small, {**ok, "verdict": "不确定"}, ["P-02"])[1]


@pytest.mark.parametrize("rule", list(RULES))
def test_case_lifecycle(conn, rule):
    c, _ = conn
    res = [r for r in pipeline.run(c, "v2", HeuristicJudge(), [rule]) if r["route"] == "自动确认"][:1]
    assert res, rule
    cases.sync(c, res)
    cid = cases.list_cases(c)[0]["id"]
    assert cases.get(c, cid)["status"] == "整改中"
    cases.apply_fix(c, cid)
    assert cases.retest(c, cid), rule
    assert cases.get(c, cid)["status"] == "已关闭"
    z = zipfile.ZipFile(io.BytesIO(cases.evidence_pack(c, cid, write=False)))
    assert {"case.json", "documents.json", "evidence.md"} <= set(z.namelist())


def test_overdue(conn):
    c, _ = conn
    res = pipeline.run(c, "v2", HeuristicJudge(), ["R2_AUTH"])[:2]
    cases.sync(c, res)
    assert cases.overdue(c, "2099-01-01") and not cases.overdue(c, "2000-01-01")
