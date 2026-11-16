"""事前拦截：录单时在 SAVEPOINT 中试写入，复用 v2 规则比对新增命中，命中即回滚并引用制度条款。"""
import datetime as dt

from . import db
from .rules import RULES, hit_keys, run_rules

TABLE = {"po": "purchase_orders", "receipt": "receipts", "invoice": "invoices", "payment": "payments"}


def _clauses(rule):
    from agent.retriever import policy_by_id
    pol = policy_by_id()
    return [pol[c] for c in RULES[rule]["clauses"]]


def submit(conn, doc_type, row, dry_run=False):
    """提交单据。返回 {blocked, id, violations:[{rule, name, detail, clauses}]}。"""
    table = TABLE[doc_type]
    row = dict(row)
    row.setdefault("id", db.next_id(conn, table))
    if doc_type == "po":
        row["amount"] = round(float(row["qty"]) * float(row["unit_price"]), 2)
        row.setdefault("created_at", dt.date.today().isoformat())
    before = hit_keys(run_rules(conn, "v2"))
    conn.execute("SAVEPOINT precheck")
    try:
        db.insert(conn, table, row)
        new = [h for h in run_rules(conn, "v2") if (h["rule"], h["entity_id"]) not in before]
    except Exception:
        conn.execute("ROLLBACK TO precheck"); conn.execute("RELEASE precheck")
        raise
    if new or dry_run:
        conn.execute("ROLLBACK TO precheck")
    conn.execute("RELEASE precheck")
    violations = [{"rule": h["rule"], "name": RULES[h["rule"]]["name"], "detail": h["detail"],
                   "entity_id": h["entity_id"], "clauses": _clauses(h["rule"])} for h in new]
    return {"blocked": bool(new), "id": row["id"], "saved": not new and not dry_run, "violations": violations}
