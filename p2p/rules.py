"""6 条 SQL 合规规则。

v1：初版规则，口径宽、追求召回；
v2：基于误报分析迭代后的规则（容差、窗口、合计口径、发票号归一化、分期付款）。
事前拦截（precheck）与事后巡检复用同一套 v2 SQL，保证口径一致。
"""
from . import config

RULES = {
    "R1_SOD": {
        "name": "职责分离",
        "desc": "申请人审批本人采购申请，或采购员同时办理验收入库，不相容岗位未分离",
        "clauses": ["P-01"],
        "v1": """
SELECT 'R1_SOD' AS rule, 'po' AS entity_type, po.id AS entity_id, po.amount AS amount,
  '申请人 '||po.requester_id||' 具备审批权限，或采购员参与收货' AS detail
FROM purchase_orders po
WHERE po.requester_id IN (SELECT id FROM users WHERE approval_limit > 0)
   OR EXISTS (SELECT 1 FROM receipts r WHERE r.po_id = po.id AND r.receiver_id = po.buyer_id)
""",
        "v2": """
SELECT 'R1_SOD' AS rule, 'po' AS entity_type, po.id AS entity_id, po.amount AS amount,
  CASE WHEN po.requester_id = po.approver_id
       THEN '申请人 '||po.requester_id||' 审批了本人提交的采购申请'
       ELSE '采购员 '||po.buyer_id||' 同时办理了验收入库' END AS detail
FROM purchase_orders po
WHERE po.requester_id = po.approver_id
   OR EXISTS (SELECT 1 FROM receipts r WHERE r.po_id = po.id AND r.receiver_id = po.buyer_id)
""",
    },
    "R2_AUTH": {
        "name": "越权审批",
        "desc": "订单金额超过审批人的审批权限，超出审批权限的审批无效",
        "clauses": ["P-02"],
        "v1": """
SELECT 'R2_AUTH' AS rule, 'po' AS entity_type, po.id AS entity_id, po.amount AS amount,
  '审批人 '||a.id||' 权限 '||CAST(a.approval_limit AS INTEGER)||'，订单金额 '||po.amount AS detail
FROM purchase_orders po JOIN users a ON a.id = po.approver_id
WHERE po.amount >= a.approval_limit
""",
        "v2": """
SELECT 'R2_AUTH' AS rule, 'po' AS entity_type, po.id AS entity_id, po.amount AS amount,
  '审批人 '||a.id||' 权限 '||CAST(a.approval_limit AS INTEGER)||'，订单金额 '||po.amount AS detail
FROM purchase_orders po JOIN users a ON a.id = po.approver_id
WHERE po.amount > a.approval_limit
""",
    },
    "R3_SPLIT": {
        "name": "拆单规避审批",
        "desc": "同一申请人对同一供应商 7 日内多笔采购申请，单笔未超权限但合计超过审批人权限，拆分采购",
        "clauses": ["P-03"],
        "v1": """
SELECT 'R3_SPLIT' AS rule, 'po' AS entity_type, p1.id AS entity_id, p1.amount AS amount,
  '同一申请人/供应商 30 日内 '||COUNT(p2.id)||' 笔采购' AS detail
FROM purchase_orders p1
JOIN purchase_orders p2 ON p2.requester_id = p1.requester_id AND p2.vendor_id = p1.vendor_id
  AND ABS(julianday(p2.created_at) - julianday(p1.created_at)) <= 30
GROUP BY p1.id
HAVING COUNT(p2.id) >= 2
""",
        "v2": """
SELECT 'R3_SPLIT' AS rule, 'po' AS entity_type, p1.id AS entity_id, p1.amount AS amount,
  '同一申请人/供应商 '||:win||' 日内 '||COUNT(p2.id)||' 笔，合计 '||ROUND(SUM(p2.amount),2)
  ||'，审批人权限 '||CAST(MAX(a.approval_limit) AS INTEGER) AS detail
FROM purchase_orders p1
JOIN users a ON a.id = p1.approver_id
JOIN purchase_orders p2 ON p2.requester_id = p1.requester_id AND p2.vendor_id = p1.vendor_id
  AND ABS(julianday(p2.created_at) - julianday(p1.created_at)) <= :win
  AND p2.amount <= :l1
WHERE p1.amount <= :l1
GROUP BY p1.id
HAVING COUNT(p2.id) >= 2 AND SUM(p2.amount) > :l1 AND SUM(p2.amount) > MAX(a.approval_limit)
""",
    },
    "R4_DUPINV": {
        "name": "重复发票",
        "desc": "同一供应商发票号码重复入账，或同一订单下同金额发票重复提交",
        "clauses": ["P-04"],
        "v1": """
SELECT 'R4_DUPINV' AS rule, 'invoice' AS entity_type, i2.id AS entity_id, i2.amount AS amount,
  '与发票 '||MIN(i1.id)||' 同供应商同金额' AS detail
FROM invoices i1 JOIN invoices i2 ON i1.vendor_id = i2.vendor_id AND i1.id < i2.id
WHERE i1.status = 'valid' AND i2.status = 'valid'
  AND ABS(i1.amount - i2.amount) < 0.01
  AND ABS(julianday(i2.invoice_date) - julianday(i1.invoice_date)) <= 90
GROUP BY i2.id
""",
        "v2": """
SELECT 'R4_DUPINV' AS rule, 'invoice' AS entity_type, i2.id AS entity_id, i2.amount AS amount,
  '与发票 '||MIN(i1.id)||'（'||MIN(i1.invoice_no)||'）重复，本张号码 '||i2.invoice_no AS detail
FROM invoices i1 JOIN invoices i2 ON i1.vendor_id = i2.vendor_id AND i1.id < i2.id
WHERE i1.status = 'valid' AND i2.status = 'valid'
  AND ( norm_inv(i1.invoice_no) = norm_inv(i2.invoice_no)
        OR (i1.po_id = i2.po_id AND ABS(i1.amount - i2.amount) < 0.01) )
GROUP BY i2.id
""",
    },
    "R5_3WAY": {
        "name": "三单匹配不符",
        "desc": "发票金额超过订单已收货金额，三单匹配差异超出 5% 容差，不得付款",
        "clauses": ["P-05"],
        "v1": """
SELECT 'R5_3WAY' AS rule, 'invoice' AS entity_type, i.id AS entity_id, i.amount AS amount,
  '发票 '||i.amount||' 与已收货 '||ROUND(rc.recv,2)||' 不一致' AS detail
FROM invoices i
JOIN (SELECT po_id, SUM(amount) AS recv FROM receipts GROUP BY po_id) rc ON rc.po_id = i.po_id
WHERE i.status = 'valid' AND ABS(i.amount - rc.recv) > 0.01
""",
        "v2": """
SELECT 'R5_3WAY' AS rule, 'invoice' AS entity_type, i.id AS entity_id, i.amount AS amount,
  '发票 '||i.amount||' vs 已收货 '||ROUND(rc.recv,2)||'，超出 '
  ||printf('%.1f%%', (i.amount - rc.recv) * 100.0 / rc.recv) AS detail
FROM invoices i
JOIN (SELECT po_id, SUM(amount) AS recv FROM receipts GROUP BY po_id) rc ON rc.po_id = i.po_id
WHERE i.status = 'valid' AND rc.recv > 0 AND (i.amount - rc.recv) > :tol * rc.recv
""",
    },
    "R6_PAY": {
        "name": "付款前置条件",
        "desc": "付款早于验收入库或无收货记录，或累计付款超过发票金额",
        "clauses": ["P-06"],
        "v1": """
SELECT 'R6_PAY' AS rule, 'payment' AS entity_type, p.id AS entity_id, p.amount AS amount,
  '付款金额与发票不一致或早于收货' AS detail
FROM payments p JOIN invoices i ON i.id = p.invoice_id
LEFT JOIN (SELECT po_id, MIN(received_at) AS first_recv FROM receipts GROUP BY po_id) fr
  ON fr.po_id = i.po_id
WHERE p.status = 'valid'
  AND (fr.first_recv IS NULL OR p.paid_at < fr.first_recv OR ABS(p.amount - i.amount) > 0.01)
""",
        "v2": """
SELECT 'R6_PAY' AS rule, 'payment' AS entity_type, p.id AS entity_id, p.amount AS amount,
  CASE WHEN fr.first_recv IS NULL THEN '付款时订单无收货记录'
       WHEN p.paid_at < fr.first_recv THEN '付款日 '||p.paid_at||' 早于首次收货日 '||fr.first_recv
       ELSE '累计付款 '||ROUND(tp.total,2)||' 超过发票金额 '||i.amount END AS detail
FROM payments p JOIN invoices i ON i.id = p.invoice_id
LEFT JOIN (SELECT po_id, MIN(received_at) AS first_recv FROM receipts GROUP BY po_id) fr
  ON fr.po_id = i.po_id
JOIN (SELECT invoice_id, SUM(amount) AS total FROM payments WHERE status = 'valid'
      GROUP BY invoice_id) tp ON tp.invoice_id = p.invoice_id
WHERE p.status = 'valid'
  AND (fr.first_recv IS NULL OR p.paid_at < fr.first_recv OR tp.total > i.amount + 0.01)
""",
    },
}


def run_rules(conn, version="v2", rules=None, entity_ids=None):
    """执行规则，返回命中列表 [{rule, entity_type, entity_id, amount, detail}]。"""
    hits = []
    for rid in rules or RULES:
        sql = RULES[rid][version]
        params = {k: v for k, v in config.RULE_PARAMS.items() if f":{k}" in sql}
        for r in conn.execute(sql, params).fetchall():
            h = dict(r)
            if entity_ids is None or h["entity_id"] in entity_ids:
                hits.append(h)
    return hits


def hit_keys(hits):
    return {(h["rule"], h["entity_id"]) for h in hits}
