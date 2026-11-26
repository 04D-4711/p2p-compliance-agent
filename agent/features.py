"""规则命中的特征提取：既给研判器做证据，也作为记忆检索的“签名”。

每条规则定义一组可比较的特征（SCHEMA）：
- num：数值特征，scale 是“差多少算明显不同”，距离按 |a-b|/scale 计；
- bool：布尔特征，不一致距离记 1。
相似度 = 1 / (1 + 距离)，完全相同为 1。
"""
from p2p import config, db

SCHEMA = {
    "R1_SOD": {"requester_is_approver": ("bool", None), "buyer_is_receiver": ("bool", None)},
    "R2_AUTH": {"over_ratio": ("num", 0.02)},
    "R3_SPLIT": {"min_gap_days": ("num", 2.0), "n_within_win": ("num", 1.0)},
    "R4_DUPINV": {"same_number": ("bool", None), "same_po": ("bool", None)},
    "R5_3WAY": {"deviation": ("num", 0.015)},
    "R6_PAY": {"has_receipt": ("bool", None), "paid_before_receipt": ("bool", None), "overpaid": ("bool", None)},
}


def _days(a, b):
    import datetime as dt
    return abs((dt.date.fromisoformat(a[:10]) - dt.date.fromisoformat(b[:10])).days)


def extract(conn, hit, chain):
    rule, f = hit["rule"], {}
    po = chain["po"] or {}
    if rule == "R1_SOD":
        f["requester_is_approver"] = po.get("requester_id") == po.get("approver_id")
        f["buyer_is_receiver"] = any(r["receiver_id"] == po.get("buyer_id") for r in chain["receipts"])
    elif rule == "R2_AUTH":
        lim = conn.execute("SELECT approval_limit FROM users WHERE id=?", (po["approver_id"],)).fetchone()[0]
        f.update(approval_limit=lim, over_ratio=round(po["amount"] / lim - 1, 4))
    elif rule == "R3_SPLIT":
        others = db.rows(conn, """SELECT created_at FROM purchase_orders
                                  WHERE requester_id=? AND vendor_id=? AND id<>?""",
                         (po["requester_id"], po["vendor_id"], po["id"]))
        gaps = [_days(o["created_at"], po["created_at"]) for o in others]
        f["min_gap_days"] = min(gaps) if gaps else 999
        f["n_within_win"] = sum(g <= config.SPLIT_WINDOW_DAYS for g in gaps)
    elif rule == "R4_DUPINV":
        me = next(i for i in chain["invoices"] if i["id"] == hit["entity_id"]) if chain["invoices"] else \
            db.rows(conn, "SELECT * FROM invoices WHERE id=?", (hit["entity_id"],))[0]
        others = db.rows(conn, "SELECT invoice_no, po_id, amount FROM invoices WHERE vendor_id=? AND id<>? AND status='valid'",
                         (me["vendor_id"], me["id"]))
        f["same_number"] = any(db.norm_inv(o["invoice_no"]) == db.norm_inv(me["invoice_no"]) for o in others)
        f["same_po"] = any(o["po_id"] == me["po_id"] and abs(o["amount"] - me["amount"]) < 0.01 for o in others)
    elif rule == "R5_3WAY":
        recv = sum(r["amount"] for r in chain["receipts"])
        inv = next(i for i in chain["invoices"] if i["id"] == hit["entity_id"])
        f.update(received=recv, invoiced=inv["amount"], deviation=round(inv["amount"] / recv - 1, 4) if recv else 1.0)
    elif rule == "R6_PAY":
        pay = next(p for p in chain["payments"] if p["id"] == hit["entity_id"])
        inv = next(i for i in chain["invoices"] if i["id"] == pay["invoice_id"])
        first = min((r["received_at"] for r in chain["receipts"]), default=None)
        paid = sum(p["amount"] for p in chain["payments"] if p["invoice_id"] == inv["id"] and p["status"] == "valid")
        f["has_receipt"] = first is not None
        f["paid_before_receipt"] = bool(first and pay["paid_at"] < first)
        f["overpaid"] = paid > inv["amount"] + 0.01
    return f


def signature(rule, features):
    """只保留参与相似度计算的特征。"""
    return {k: features.get(k) for k in SCHEMA[rule]}


def similarity(rule, a, b):
    d = 0.0
    for k, (kind, scale) in SCHEMA[rule].items():
        x, y = a.get(k), b.get(k)
        if x is None or y is None:
            d += 1
        elif kind == "bool":
            d += float(bool(x) != bool(y))
        else:
            d += abs(float(x) - float(y)) / scale
    return 1 / (1 + d)
