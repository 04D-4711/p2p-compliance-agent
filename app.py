"""Streamlit 界面：录单拦截 / 单据追溯 / 巡检 Agent / 风险看板 / 效果评估。

运行：streamlit run app.py
"""
import datetime as dt
import json

import pandas as pd
import streamlit as st

from agent import pipeline
from agent.judge import get_judge
from evaluation.evaluate import evaluate, to_markdown
from p2p import cases, config, db, precheck, seed
from p2p.rules import RULES

st.set_page_config(page_title="P2P 合规审查 Agent", layout="wide")


@st.cache_resource
def get_conn():
    conn = db.connect()
    if db.is_empty(conn):
        seed.build(conn, config.LABELS_PATH)
    return conn


conn = get_conn()
users = db.rows(conn, "SELECT * FROM users ORDER BY id")
uname = {u["id"]: f"{u['id']} {u['name']}（{u['role']}）" for u in users}
vendors = {v["id"]: f"{v['id']} {v['name']}" for v in db.rows(conn, "SELECT * FROM vendors ORDER BY id")}

page = st.sidebar.radio("功能", ["录单（事前拦截）", "单据追溯", "巡检 Agent", "风险看板", "效果评估"])
as_of = st.sidebar.date_input("看板日期（模拟逾期）", dt.date.today())
judge_kind = st.sidebar.selectbox("研判器", ["auto", "heuristic", "claude"],
                                  help="auto：有 ANTHROPIC_API_KEY 时用 Claude，否则用离线启发式研判")
if st.sidebar.button("重置演示数据"):
    conn.close()
    get_conn.clear()
    seed.reset_and_build()
    st.rerun()


def show_result(res):
    if res["blocked"]:
        st.error(f"已拦截：{res['id']} 未入库")
        for v in res["violations"]:
            st.markdown(f"**{v['rule']} {v['name']}** · {v['detail']}")
            for c in v["clauses"]:
                st.caption(f"{c['id']} {c['title']}：{c['text']}")
    elif res["saved"]:
        st.success(f"校验通过，已入库：{res['id']}")
    else:
        st.info("校验通过（仅校验，未入库）")


# ------------------------------------------------------------------ 录单
if page == "录单（事前拦截）":
    st.header("录单：事前校验与拦截")
    st.caption("提交时在事务保存点内试写入并重跑 v2 规则，产生新命中即回滚，并引用制度条款说明原因。")
    dry = st.checkbox("仅校验不入库", value=False)
    t1, t2, t3, t4 = st.tabs(["采购订单 PO", "收货", "发票", "付款"])
    with t1, st.form("po"):
        c1, c2, c3 = st.columns(3)
        req = c1.selectbox("申请人", list(uname), format_func=uname.get, index=0)
        buyer = c2.selectbox("采购员", list(uname), format_func=uname.get, index=3)
        appr = c3.selectbox("审批人", list(uname), format_func=uname.get, index=5)
        vend = c1.selectbox("供应商", list(vendors), format_func=vendors.get)
        item = c2.text_input("物料", "办公耗材")
        date = c3.date_input("日期", dt.date(2026, 10, 1))
        qty = c1.number_input("数量", 1.0, value=10.0)
        price = c2.number_input("单价", 0.01, value=1000.0)
        if st.form_submit_button("提交 PO"):
            show_result(precheck.submit(conn, "po", dict(requester_id=req, buyer_id=buyer, approver_id=appr,
                                        vendor_id=vend, item=item, qty=qty, unit_price=price,
                                        created_at=date.isoformat()), dry))
    pos = [r["id"] for r in db.rows(conn, "SELECT id FROM purchase_orders ORDER BY id DESC LIMIT 200")]
    with t2, st.form("gr"):
        po = st.selectbox("订单", pos)
        p = db.rows(conn, "SELECT * FROM purchase_orders WHERE id=?", (po,))[0]
        st.caption(f"采购员 {p['buyer_id']} · 金额 {p['amount']}")
        rcv = st.selectbox("收货人", list(uname), format_func=uname.get, index=8)
        q = st.number_input("收货数量", 0.0, value=float(p["qty"]))
        d = st.date_input("收货日期", dt.date(2026, 10, 8))
        if st.form_submit_button("提交收货"):
            show_result(precheck.submit(conn, "receipt", dict(po_id=po, receiver_id=rcv, qty=q,
                                        amount=round(q * p["unit_price"], 2), received_at=d.isoformat()), dry))
    with t3, st.form("iv"):
        po = st.selectbox("订单 ", pos)
        p = db.rows(conn, "SELECT * FROM purchase_orders WHERE id=?", (po,))[0]
        recv = conn.execute("SELECT COALESCE(SUM(amount),0) FROM receipts WHERE po_id=?", (po,)).fetchone()[0]
        st.caption(f"供应商 {p['vendor_id']} · 订单金额 {p['amount']} · 已收货 {recv}")
        no = st.text_input("发票号", "INV-2026-90001")
        amt = st.number_input("发票金额", 0.0, value=float(recv or p["amount"]))
        d = st.date_input("开票日期", dt.date(2026, 10, 10))
        if st.form_submit_button("提交发票"):
            show_result(precheck.submit(conn, "invoice", dict(po_id=po, vendor_id=p["vendor_id"], invoice_no=no,
                                        amount=amt, invoice_date=d.isoformat(), entered_by="U11"), dry))
    with t4, st.form("py"):
        ivs = [r["id"] for r in db.rows(conn, "SELECT id FROM invoices WHERE status='valid' ORDER BY id DESC LIMIT 200")]
        iv = st.selectbox("发票", ivs)
        i = db.rows(conn, "SELECT * FROM invoices WHERE id=?", (iv,))[0]
        paid = conn.execute("SELECT COALESCE(SUM(amount),0) FROM payments WHERE invoice_id=? AND status='valid'",
                            (iv,)).fetchone()[0]
        st.caption(f"发票金额 {i['amount']} · 已付 {paid}")
        amt = st.number_input("付款金额", 0.0, value=float(max(i["amount"] - paid, 0)))
        d = st.date_input("付款日期", dt.date(2026, 10, 20))
        if st.form_submit_button("提交付款"):
            show_result(precheck.submit(conn, "payment", dict(invoice_id=iv, amount=amt, paid_at=d.isoformat(),
                                        paid_by="U12"), dry))

# ------------------------------------------------------------------ 追溯
elif page == "单据追溯":
    st.header("单据追溯：PO → 收货 → 发票 → 付款")
    q = st.text_input("输入任意单据号（PO/GR/IV/PY）", "PO00001").strip().upper()
    etype = {"PO": "po", "GR": "receipt", "IV": "invoice", "PY": "payment"}.get(q[:2])
    try:
        ch = db.chain_for(conn, etype, q)
    except Exception:
        ch = None
    if not ch or not ch["po"]:
        st.warning("未找到该单据")
    else:
        st.subheader("采购订单"); st.dataframe(pd.DataFrame([ch["po"]]), hide_index=True)
        for k, t in (("receipts", "收货"), ("invoices", "发票"), ("payments", "付款")):
            st.subheader(t); st.dataframe(pd.DataFrame(ch[k]), hide_index=True)
        ids = [ch["po"]["id"]] + [x["id"] for k in ("receipts", "invoices", "payments") for x in ch[k]]
        rel = db.rows(conn, f"SELECT id, rule, entity_id, status FROM cases WHERE entity_id IN ({','.join('?' * len(ids))})", ids)
        st.subheader("关联风险案件"); st.dataframe(pd.DataFrame(rel), hide_index=True)

# ------------------------------------------------------------------ 巡检
elif page == "巡检 Agent":
    st.header("事后巡检：SQL 初筛 → 条款检索取证 → 研判 → 人审路由")
    c1, c2 = st.columns(2)
    ver = c1.selectbox("规则版本", ["v2", "v1"])
    sel = c2.multiselect("规则", list(RULES), default=list(RULES), format_func=lambda r: f"{r} {RULES[r]['name']}")
    if st.button("运行巡检"):
        with st.spinner("巡检中…"):
            st.session_state["scan"] = pipeline.run(conn, ver, get_judge(judge_kind), sel)
    res = st.session_state.get("scan")
    if res:
        df = pd.DataFrame([{"规则": r["rule"], "名称": r["rule_name"], "单据": r["entity_id"], "金额": r["amount"],
                            "命中说明": r["detail"], "研判": r["verdict"], "置信度": r["confidence"],
                            "引用条款": ",".join(r["cited"]), "路由": r["route"],
                            "转人工原因": "、".join(r["route_reasons"]), "研判器": r["judge"]} for r in res])
        a, b, c = st.columns(3)
        a.metric("命中", len(df)); b.metric("自动确认", int((df["路由"] == "自动确认").sum()))
        c.metric("转人工", int((df["路由"] == "人工复核").sum()))
        st.dataframe(df, hide_index=True, use_container_width=True)
        if st.button("写入风险看板"):
            n = cases.sync(conn, res)
            st.success(f"新建案件 {n} 个（已存在的同一 规则+单据 不重复建）")

# ------------------------------------------------------------------ 看板
elif page == "风险看板":
    st.header("风险看板：识别 → 确认 → 整改 → 复测 → 证据沉淀")
    allc = cases.list_cases(conn)
    if not allc:
        st.info("暂无案件。先到“巡检 Agent”运行巡检并写入看板。")
    else:
        df = pd.DataFrame(allc)
        od = {c["id"] for c in cases.overdue(conn, as_of)}
        cols = st.columns(6)
        for col, s in zip(cols, ["待确认", "整改中", "待复测", "已关闭", "误报关闭"]):
            col.metric(s, int((df["status"] == s).sum()))
        cols[5].metric("逾期", len(od))
        if od:
            st.warning(f"逾期提醒：{len(od)} 个案件超过整改截止日（看板日期 {as_of}）")
        st.bar_chart(df.groupby(["rule", "status"]).size().unstack(fill_value=0))
        f = st.multiselect("状态筛选", sorted(df["status"].unique()), default=[s for s in cases.OPEN if s in set(df["status"])])
        view = df[df["status"].isin(f)] if f else df
        view = view.assign(逾期=view["id"].isin(od))
        st.dataframe(view[["id", "rule", "entity_id", "amount", "status", "route", "route_reasons", "confidence",
                           "owner", "due_date", "逾期"]], hide_index=True, use_container_width=True)
        cid = st.selectbox("选择案件", view["id"].tolist() or df["id"].tolist())
        c = cases.get(conn, cid)
        st.subheader(f"案件 #{cid} · {c['rule']} {RULES[c['rule']]['name']} · {c['status']}")
        st.write(c["detail"]); st.caption(f"研判：{c['verdict']}（{c['confidence']}）{c['reasoning']}")
        for e in json.loads(c["evidence"]):
            st.caption(f"{e['id']} {e['title']}：{e['text']}")
        b1, b2, b3, b4 = st.columns(4)
        if c["status"] == "待确认":
            if b1.button("确认异常"):
                cases.confirm(conn, cid); st.rerun()
            if b2.button("判定误报"):
                cases.false_positive(conn, cid); st.rerun()
        if c["status"] == "整改中":
            owner = b1.text_input("责任方", c["owner"])
            due = b2.date_input("截止日", dt.date.fromisoformat(c["due_date"]))
            if b3.button("派发整改"):
                cases.assign(conn, cid, owner, due); st.rerun()
            if b4.button("执行整改（演示）"):
                st.toast(cases.apply_fix(conn, cid)); st.rerun()
        if c["status"] == "待复测" and b1.button("复测"):
            ok = cases.retest(conn, cid)
            st.toast("复测通过，已关闭" if ok else "复测未通过，退回整改"); st.rerun()
        st.download_button("导出证据包", cases.evidence_pack(conn, cid, write=False),
                           file_name=f"case_{cid}_evidence.zip")
        st.subheader("处理时间线")
        st.dataframe(pd.DataFrame(cases.events(conn, cid)), hide_index=True, use_container_width=True)

# ------------------------------------------------------------------ 评估
else:
    st.header("效果评估（合成标注集）")
    st.caption("在全新的内存数据库上生成评估集，比较 v1 / v2 规则，不影响当前演示数据。")
    if st.button("运行评估"):
        st.session_state["eval"] = to_markdown(evaluate(judge="heuristic"))
    if "eval" in st.session_state:
        st.markdown(st.session_state["eval"])
