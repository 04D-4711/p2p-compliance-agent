"""Agent 工作流：SQL 规则初筛 → 条款检索取证 → 研判 → 人审路由。"""
from p2p import config, db
from p2p.rules import RULES, run_rules

from .judge import get_judge
from .retriever import retrieve


def features(conn, hit, chain):
    rule, f = hit["rule"], {}
    po = chain["po"] or {}
    if rule == "R2_AUTH":
        lim = conn.execute("SELECT approval_limit FROM users WHERE id=?", (po["approver_id"],)).fetchone()[0]
        f.update(approval_limit=lim, over_ratio=round(po["amount"] / lim - 1, 4))
    elif rule == "R5_3WAY":
        recv = sum(r["amount"] for r in chain["receipts"])
        inv = next(i for i in chain["invoices"] if i["id"] == hit["entity_id"])
        f.update(received=recv, invoiced=inv["amount"], deviation=round(inv["amount"] / recv - 1, 4) if recv else None)
    elif rule == "R4_DUPINV":
        me = next(i for i in chain["invoices"] if i["id"] == hit["entity_id"])
        others = db.rows(conn, "SELECT invoice_no FROM invoices WHERE vendor_id=? AND id<>? AND status='valid'",
                         (me["vendor_id"], me["id"]))
        f["same_number"] = any(db.norm_inv(o["invoice_no"]) == db.norm_inv(me["invoice_no"]) for o in others)
    elif rule == "R6_PAY":
        f["has_receipt"] = bool(chain["receipts"])
    return f


def route(hit, judgment, evidence_ids):
    reasons = []
    if hit["amount"] >= config.HIGH_AMOUNT:
        reasons.append("大额")
    if judgment["confidence"] < config.CONF_THRESHOLD:
        reasons.append("低置信")
    if not judgment["cited"]:
        reasons.append("未引用条款")
    elif not set(judgment["cited"]) <= set(evidence_ids):
        reasons.append("引用证据外条款")
    if judgment["verdict"] != "违规":
        reasons.append(f"研判为{judgment['verdict']}")
    return ("人工复核" if reasons else "自动确认"), reasons


def analyse(conn, hit, judge):
    chain = db.chain_for(conn, hit["entity_type"], hit["entity_id"])
    ctx = {"chain": chain, "features": features(conn, hit, chain)}
    meta = RULES[hit["rule"]]
    evidence = retrieve(f"{meta['name']} {meta['desc']} {hit['detail']}", k=3)
    j = judge.judge(hit, ctx, evidence)
    r, reasons = route(hit, j, [e["id"] for e in evidence])
    return {**hit, "rule_name": meta["name"], "evidence": evidence, "features": ctx["features"],
            **j, "route": r, "route_reasons": reasons}


def run(conn, version="v2", judge="auto", rules=None):
    judge = get_judge(judge) if isinstance(judge, str) else judge
    return [analyse(conn, h, judge) for h in run_rules(conn, version, rules)]
