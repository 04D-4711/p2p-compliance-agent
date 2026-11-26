"""风险案件闭环：风险识别 → 异常确认 → 整改推进 → 复测验收 → 证据沉淀。"""
import datetime as dt
import io
import json
import zipfile

from . import config, db
from .rules import RULES, run_rules

OPEN = ("待确认", "整改中", "待复测")
OWNER = {"R1_SOD": "采购部", "R2_AUTH": "采购部", "R3_SPLIT": "采购部",
         "R4_DUPINV": "应付组", "R5_3WAY": "应付组", "R6_PAY": "资金组"}


def _now():
    return dt.datetime.now().isoformat(timespec="seconds")


def event(conn, cid, stage, action, note=""):
    conn.execute("INSERT INTO case_events(case_id, ts, stage, action, note) VALUES(?,?,?,?,?)",
                 (cid, _now(), stage, action, note))


def get(conn, cid):
    r = db.rows(conn, "SELECT * FROM cases WHERE id=?", (cid,))
    return r[0] if r else None


def events(conn, cid):
    return db.rows(conn, "SELECT ts, stage, action, note FROM case_events WHERE case_id=? ORDER BY id", (cid,))


def sync(conn, results, today=None):
    """把 Agent 结果写入案件库（同一 规则+单据 只建一次）。返回新建数量。"""
    today = today or dt.date.today()
    created = 0
    with db.tx(conn):
        for r in results:
            status = {"自动确认": "整改中", "自动排除": "自动排除"}.get(r["route"], "待确认")
            cur = conn.execute(
                """INSERT OR IGNORE INTO cases(rule, entity_type, entity_id, amount, detail, status, route,
                   route_reasons, verdict, confidence, cited, evidence, reasoning, judge, owner, created_at, due_date,
                   features, memory_refs)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["rule"], r["entity_type"], r["entity_id"], r["amount"], r["detail"], status, r["route"],
                 "、".join(r["route_reasons"]), r["verdict"], r["confidence"], json.dumps(r["cited"]),
                 json.dumps(r["evidence"], ensure_ascii=False), r["reasoning"], r["judge"], OWNER[r["rule"]],
                 today.isoformat(), (today + dt.timedelta(days=config.DUE_DAYS)).isoformat(),
                 json.dumps(r.get("features", {}), ensure_ascii=False),
                 json.dumps(r.get("memory_refs", []))))
            if cur.rowcount:
                cid = cur.lastrowid
                created += 1
                event(conn, cid, "风险识别", f"规则 {r['rule']} 命中", r["detail"])
                event(conn, cid, "异常确认", f"Agent 研判：{r['verdict']}（置信度 {r['confidence']:.2f}）",
                      f"路由：{r['route']} {'、'.join(r['route_reasons'])}")
                if status == "整改中":
                    event(conn, cid, "整改推进", f"自动派发至 {OWNER[r['rule']]}")
                elif status == "自动排除":
                    event(conn, cid, "异常确认", "依据人审先例自动排除，可人工纠正", r["reasoning"])
    return created


def _set(conn, cid, **kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    conn.execute(f"UPDATE cases SET {sets} WHERE id=?", [*kw.values(), cid])


def remember(conn, cid, verdict, note="", fact=None, today=None):
    """把人工复核结论写入长期记忆（只有人审结论会写入）。fact=(scope_type, scope_id, 文本)。"""
    from agent.memory import MemoryStore
    c = get(conn, cid)
    if not c or not c.get("features"):
        return None
    res = MemoryStore(conn).write_review(c["rule"], json.loads(c["features"]), verdict, note,
                                         ref=f"case#{cid}", today=today, fact=fact)
    ids = json.loads(c.get("memory_ids") or "[]") + [res["id"]]
    _set(conn, cid, memory_ids=json.dumps(ids))
    event(conn, cid, "证据沉淀", f"人审结论写入记忆 #{res['id']}（{'合并' if res['action'] == 'merge' else '新增'}）",
          f"与 {res['conflicts']} 条相反先例近似" if res["conflicts"] else "")
    return res


def confirm(conn, cid, note="人工复核确认", learn=True):
    """人工确认异常；也用于纠正被记忆自动排除的案件。"""
    with db.tx(conn):
        corrected = get(conn, cid)["status"] == "自动排除"
        _set(conn, cid, status="整改中", closed_at=None)
        event(conn, cid, "异常确认", "纠正自动排除：人工确认异常" if corrected else "人工确认异常", note)
        if learn:
            remember(conn, cid, "违规", note if note != "人工复核确认" else "")


def false_positive(conn, cid, note="人工复核判定误报", learn=True, fact=None):
    with db.tx(conn):
        _set(conn, cid, status="误报关闭", closed_at=_now())
        event(conn, cid, "异常确认", "判定误报并关闭", note)
        if learn:
            remember(conn, cid, "不违规", note if note != "人工复核判定误报" else "", fact=fact)


def assign(conn, cid, owner, due_date=None):
    with db.tx(conn):
        kw = {"owner": owner}
        if due_date:
            kw["due_date"] = str(due_date)
        _set(conn, cid, **kw)
        event(conn, cid, "整改推进", f"派发至 {owner}", f"截止 {due_date or get(conn, cid)['due_date']}")


def apply_fix(conn, cid):
    """演示用整改动作：按规则修正单据数据，之后进入待复测。"""
    c = get(conn, cid)
    rule, eid = c["rule"], c["entity_id"]
    with db.tx(conn):
        if rule in ("R1_SOD", "R2_AUTH"):
            po = db.rows(conn, "SELECT * FROM purchase_orders WHERE id=?", (eid,))[0]
            from .seed import Gen
            ap = Gen.approver_for(po["amount"], exclude=po["requester_id"])
            conn.execute("UPDATE purchase_orders SET approver_id=? WHERE id=?", (ap, eid))
            other = "U10" if po["buyer_id"] != "U10" else "U09"
            conn.execute("UPDATE receipts SET receiver_id=? WHERE po_id=? AND receiver_id=?", (other, eid, po["buyer_id"]))
            note = f"按权限改由 {ap} 重新审批，收货人与采购员分离"
        elif rule == "R3_SPLIT":
            po = db.rows(conn, "SELECT * FROM purchase_orders WHERE id=?", (eid,))[0]
            grp = db.rows(conn, """SELECT id, amount FROM purchase_orders WHERE requester_id=? AND vendor_id=?
                                   AND ABS(julianday(created_at)-julianday(?)) <= ?""",
                          (po["requester_id"], po["vendor_id"], po["created_at"], config.SPLIT_WINDOW_DAYS))
            from .seed import Gen
            ap = Gen.approver_for(sum(g["amount"] for g in grp), exclude=po["requester_id"])
            conn.executemany("UPDATE purchase_orders SET approver_id=? WHERE id=?", [(ap, g["id"]) for g in grp])
            note = f"{len(grp)} 笔订单合并按合计金额由 {ap} 重新审批"
        elif rule == "R4_DUPINV":
            conn.execute("UPDATE invoices SET status='void' WHERE id=?", (eid,))
            conn.execute("UPDATE payments SET status='reversed' WHERE invoice_id=?", (eid,))
            note = "重复发票作废，相关付款冲回"
        elif rule == "R5_3WAY":
            recv = conn.execute("""SELECT SUM(r.amount) FROM receipts r JOIN invoices i ON i.po_id=r.po_id
                                   WHERE i.id=?""", (eid,)).fetchone()[0]
            conn.execute("UPDATE invoices SET amount=? WHERE id=?", (round(recv, 2), eid))
            conn.execute("UPDATE payments SET amount=? WHERE invoice_id=? AND status='valid'", (round(recv, 2), eid))
            note = f"供应商开具红字发票，发票金额冲减至已收货金额 {recv:.2f}"
        elif rule == "R6_PAY":
            p = db.rows(conn, "SELECT * FROM payments WHERE id=?", (eid,))[0]
            conn.execute("UPDATE payments SET status='reversed' WHERE id=?", (eid,))
            note = "付款冲回，待收货入库后重新付款"
        _set(conn, cid, status="待复测")
        event(conn, cid, "整改推进", "整改完成，提交复测", note)
    return note


def retest(conn, cid):
    """复测：对该单据重跑同一规则；未命中则关闭。"""
    c = get(conn, cid)
    still = run_rules(conn, "v2", [c["rule"]], {c["entity_id"]})
    with db.tx(conn):
        if still:
            _set(conn, cid, status="整改中")
            event(conn, cid, "复测验收", "复测未通过，退回整改", still[0]["detail"])
        else:
            _set(conn, cid, status="已关闭", closed_at=_now())
            event(conn, cid, "复测验收", "复测通过，案件关闭", f"规则 {c['rule']} 重跑未命中")
            event(conn, cid, "证据沉淀", "证据包可导出")
    return not still


def overdue(conn, as_of=None):
    as_of = str(as_of or dt.date.today())
    return db.rows(conn, f"""SELECT * FROM cases WHERE status IN ({','.join('?' * len(OPEN))})
                             AND due_date < ? ORDER BY due_date""", (*OPEN, as_of))


def list_cases(conn):
    return db.rows(conn, "SELECT * FROM cases ORDER BY id")


def evidence_pack(conn, cid, write=True):
    """导出证据包 zip：case.json、documents.json、evidence.md。"""
    c = get(conn, cid)
    ev = events(conn, cid)
    chain = db.chain_for(conn, c["entity_type"], c["entity_id"])
    clauses = json.loads(c["evidence"] or "[]")
    md = [f"# 证据包：案件 #{cid}", "",
          f"- 规则：{c['rule']}（{RULES[c['rule']]['name']}）",
          f"- 单据：{c['entity_type']} {c['entity_id']}，金额 {c['amount']}",
          f"- 命中说明：{c['detail']}",
          f"- 研判：{c['verdict']}，置信度 {c['confidence']}，研判器 {c['judge']}",
          f"- 引用条款：{', '.join(json.loads(c['cited'] or '[]'))}",
          f"- 路由：{c['route']} {c['route_reasons'] or ''}",
          f"- 状态：{c['status']}，责任方 {c['owner']}，截止 {c['due_date']}，关闭 {c['closed_at'] or '-'}",
          "", "## 研判理由", c["reasoning"] or "", "", "## 检索到的制度条款"]
    md += [f"- **{e['id']} {e['title']}**（相关度 {e['score']}）：{e['text']}" for e in clauses]
    md += ["", "## 处理时间线"] + [f"- {e['ts']} · {e['stage']} · {e['action']} {e['note'] or ''}" for e in ev]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("case.json", json.dumps({"case": c, "events": ev}, ensure_ascii=False, indent=2))
        z.writestr("documents.json", json.dumps(chain, ensure_ascii=False, indent=2))
        z.writestr("evidence.md", "\n".join(md))
    data = buf.getvalue()
    if write:
        config.EXPORT_DIR.mkdir(exist_ok=True)
        (config.EXPORT_DIR / f"case_{cid}_evidence.zip").write_bytes(data)
        with db.tx(conn):
            event(conn, cid, "证据沉淀", "导出证据包", f"case_{cid}_evidence.zip")
    return data
