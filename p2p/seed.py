"""生成合成 P2P 单据数据与评估标注。

- 正常单据链 300 条；
- 6 类注入异常共 90 条（anomaly），其中少量“难例”是 v2 规则覆盖不到的；
- “近似合规”干扰样本（distractor）：边界金额、容差内差异、月度固定服务费、分期付款等。

注意：数据和规则出自同一作者，评估结果只能说明规则在这些构造场景下的行为，
不能代表真实业务数据上的效果（见 README“评估说明”）。
"""
import csv
import datetime as dt
import random
from collections import defaultdict

from . import config, db

USERS = [
    ("U01", "张敏", "requester", 0), ("U02", "李强", "requester", 0), ("U03", "王芳", "requester", 0),
    ("U04", "赵磊", "buyer", 0), ("U05", "陈静", "buyer", 0),
    ("U06", "刘洋", "approver_l1", 50_000), ("U07", "周婷", "approver_l2", 200_000),
    ("U08", "吴刚", "cfo", 1e12),
    ("U09", "孙丽", "receiver", 0), ("U10", "郑浩", "receiver", 0),
    ("U11", "冯雪", "ap_clerk", 0), ("U12", "何军", "treasurer", 0),
    ("U13", "黄蕾", "requester", 0), ("U14", "林涛", "requester", 0), ("U15", "高宇", "requester", 0),
]
VENDORS = [
    ("V01", "华东办公用品有限公司", "办公用品"), ("V02", "中科信息技术有限公司", "IT 设备"),
    ("V03", "申达物流服务有限公司", "物流服务"), ("V04", "恒信包装材料有限公司", "包装材料"),
    ("V05", "金桥机电设备有限公司", "机电设备"), ("V06", "明德印务有限公司", "印刷"),
    ("V07", "宏远劳保用品有限公司", "劳保用品"), ("V08", "博瑞软件有限公司", "软件"),
    ("V09", "新航线差旅服务有限公司", "差旅"), ("V10", "泰和家具有限公司", "家具"),
    ("V11", "清源环保科技有限公司", "环保服务"), ("V12", "锦程广告传媒有限公司", "市场推广"),
    ("V13", "安盾安防设备有限公司", "安防设备"),
    ("V14", "恒洁物业服务有限公司", "物业服务"), ("V15", "云帆网络科技有限公司", "IT 服务"),
]
ITEMS = ["办公耗材", "笔记本电脑", "仓储配送", "纸箱", "空压机配件", "宣传册印刷", "劳保手套",
         "软件许可", "差旅服务", "办公桌椅", "废弃物处理", "展会物料", "监控摄像头"]
GENERAL_VENDORS = [v[0] for v in VENDORS[:13]]
REQUESTERS = ["U01", "U02", "U03", "U13", "U14", "U15"]
START, END = dt.date(2026, 1, 5), dt.date(2026, 9, 30)


def _d(x):
    return x.isoformat()


class Gen:
    def __init__(self, conn, rnd):
        self.c, self.r = conn, rnd
        self.labels = []
        self.idx = defaultdict(list)  # (requester, vendor) -> [date]
        self.n = defaultdict(int)
        self.inv_seq = 0

    # ---------- 基础写入 ----------
    def _id(self, table):
        self.n[table] += 1
        return f"{db.PREFIX[table]}{self.n[table]:05d}"

    def free(self, req, vendor, date, window=10):
        return all(abs((date - d0).days) > window for d0 in self.idx[(req, vendor)])

    def rand_date(self, req, vendor, lo=START, hi=END, window=10):
        span = (hi - lo).days
        for _ in range(500):
            d = lo + dt.timedelta(days=self.r.randint(0, span))
            if self.free(req, vendor, d, window):
                return d
        raise RuntimeError("no free date")

    @staticmethod
    def approver_for(amount, exclude=None):
        for uid, lim in (("U06", 50_000), ("U07", 200_000), ("U08", 1e12)):
            if amount <= lim and uid != exclude:
                return uid
        return "U08"

    def amount(self, lo, hi):
        return round(self.r.uniform(lo, hi), 2)

    def po(self, req, vendor, amount, date, approver=None, buyer=None, exact=False):
        pid = self._id("purchase_orders")
        qty = 1 if exact else self.r.randint(1, 20)
        unit = round(amount / qty, 2)
        amt = round(qty * unit, 2)
        db.insert(self.c, "purchase_orders", dict(
            id=pid, requester_id=req, buyer_id=buyer or self.r.choice(["U04", "U05"]),
            approver_id=approver or self.approver_for(amt), vendor_id=vendor,
            item=self.r.choice(ITEMS), qty=qty, unit_price=unit, amount=amt, created_at=_d(date)))
        self.idx[(req, vendor)].append(date)
        return pid, amt, qty

    def receipt(self, po_id, qty, amount, date, receiver=None):
        rid = self._id("receipts")
        db.insert(self.c, "receipts", dict(id=rid, po_id=po_id, receiver_id=receiver or self.r.choice(["U09", "U10"]),
                                           qty=qty, amount=amount, received_at=_d(date)))
        return rid

    def new_inv_no(self):
        self.inv_seq += 1
        return f"INV-2026-{self.inv_seq:05d}"

    def invoice(self, po_id, vendor, amount, date, no=None):
        iid = self._id("invoices")
        db.insert(self.c, "invoices", dict(id=iid, po_id=po_id, vendor_id=vendor, invoice_no=no or self.new_inv_no(),
                                           amount=amount, invoice_date=_d(date), entered_by="U11"))
        return iid

    def payment(self, inv_id, amount, date):
        yid = self._id("payments")
        db.insert(self.c, "payments", dict(id=yid, invoice_id=inv_id, amount=amount, paid_at=_d(date), paid_by="U12"))
        return yid

    def label(self, rule, etype, eid, kind, note=""):
        self.labels.append(dict(rule=rule, entity_type=etype, entity_id=eid, kind=kind, note=note))

    # ---------- 完整单据链 ----------
    def chain(self, req, vendor, amount, date, *, approver=None, buyer=None, receiver=None,
              inv_factor=1.0, pay_before_receipt=False, no_receipt=False, partial=None, exact=False):
        r = self.r
        pid, amt, qty = self.po(req, vendor, amount, date, approver, buyer, exact)
        out = {"po": pid, "amount": amt}
        if pay_before_receipt or no_receipt:
            inv_date = date + dt.timedelta(days=r.randint(1, 3))
            pay_date = inv_date + dt.timedelta(days=r.randint(1, 3))
            recv_date = pay_date + dt.timedelta(days=r.randint(2, 8))
        else:
            recv_date = date + dt.timedelta(days=r.randint(3, 10))
            inv_date = recv_date + dt.timedelta(days=r.randint(1, 7))
            pay_date = inv_date + dt.timedelta(days=r.randint(5, 20))
        if not no_receipt:
            out["receipt"] = self.receipt(pid, qty, amt, recv_date, receiver)
            out["recv_date"] = recv_date
        inv_amt = round(amt * inv_factor, 2)
        out["invoice"] = self.invoice(pid, vendor, inv_amt, inv_date)
        out["inv_amount"], out["inv_date"] = inv_amt, inv_date
        if partial:
            pays, d = [], pay_date
            for i, f in enumerate(partial):
                a = round(inv_amt * f, 2) if i < len(partial) - 1 else round(inv_amt - sum(p[1] for p in pays), 2)
                pays.append((self.payment(out["invoice"], a, d), a))
                d += dt.timedelta(days=30)
            out["payments"] = [p[0] for p in pays]
        else:
            out["payments"] = [self.payment(out["invoice"], inv_amt, pay_date)]
        return out


def _variant(no, r):
    """同一张发票号码的不同写法（分隔符、大小写、前导零）。"""
    _, year, seq = no.split("-")
    return r.choice([f"INV{year}{int(seq)}", f"inv-{year}-{seq}", f"INV {year} {int(seq):04d}", f"INV/{year}/{seq}"])


def build(conn, labels_path=None, seed=42, n_normal=300):
    r = random.Random(seed)
    g = Gen(conn, r)
    with db.tx(conn):
        for row in USERS:
            db.insert(conn, "users", dict(zip(["id", "name", "role", "approval_limit"], row)))
        for row in VENDORS:
            db.insert(conn, "vendors", dict(zip(["id", "name", "category"], row)))

        def gv(req):
            return r.choice(GENERAL_VENDORS)

        # ---- R1 职责分离：15 异常 + 10 干扰 ----
        for _ in range(8):  # 申请人自批
            v = gv("U06"); d = g.rand_date("U06", v)
            c = g.chain("U06", v, g.amount(5_000, 45_000), d, approver="U06")
            g.label("R1_SOD", "po", c["po"], "anomaly", "申请人自批")
        for _ in range(7):  # 采购员自收货
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v)
            c = g.chain(q, v, g.amount(5_000, 45_000), d, buyer="U04", receiver="U04")
            g.label("R1_SOD", "po", c["po"], "anomaly", "采购员自收货")
        for _ in range(10):  # 干扰：审批人以申请人身份提单，但由他人审批（合规）
            v = gv("U06"); d = g.rand_date("U06", v)
            c = g.chain("U06", v, g.amount(5_000, 45_000), d, approver="U07")
            g.label("R1_SOD", "po", c["po"], "distractor", "审批人提单、他人审批")

        # ---- R2 越权审批：15 异常 + 15 干扰 ----
        for i in range(15):
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v)
            if i < 12:
                c = g.chain(q, v, g.amount(51_000, 95_000), d, approver="U06")
            else:
                c = g.chain(q, v, g.amount(205_000, 260_000), d, approver="U07")
            g.label("R2_AUTH", "po", c["po"], "anomaly", "超权限审批")
        for i in range(15):  # 干扰：恰好等于权限上限（含）
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v)
            amt, ap = (50_000.0, "U06") if i < 10 else (200_000.0, "U07")
            c = g.chain(q, v, amt, d, approver=ap, exact=True)
            g.label("R2_AUTH", "po", c["po"], "distractor", "金额等于权限上限")

        # ---- R3 拆单：4 组×3 笔 + 1 组难例（间隔 9 天）+ 5 组干扰 ----
        for _ in range(4):
            q = r.choice(REQUESTERS); v = gv(q)
            d0 = g.rand_date(q, v, hi=END - dt.timedelta(days=30), window=20)
            for off in (0, 2, 5):
                c = g.chain(q, v, g.amount(18_000, 45_000), d0 + dt.timedelta(days=off), approver="U06")
                g.label("R3_SPLIT", "po", c["po"], "anomaly", "7 日内拆单")
        q = r.choice(REQUESTERS); v = gv(q)
        d0 = g.rand_date(q, v, hi=END - dt.timedelta(days=30), window=20)
        for off in (0, 9):
            c = g.chain(q, v, g.amount(28_000, 45_000), d0 + dt.timedelta(days=off), approver="U06")
            g.label("R3_SPLIT", "po", c["po"], "anomaly", "难例：间隔 9 天的拆单，超出 7 日窗口")
        for _ in range(5):  # 干扰：同一供应商正常补货，间隔 15–25 天
            q = r.choice(REQUESTERS); v = gv(q)
            d0 = g.rand_date(q, v, hi=END - dt.timedelta(days=40), window=30)
            for off in (0, r.randint(15, 25)):
                c = g.chain(q, v, g.amount(20_000, 45_000), d0 + dt.timedelta(days=off), approver="U06")
                g.label("R3_SPLIT", "po", c["po"], "distractor", "正常补货，间隔 >7 天")

        # ---- R4 重复发票：13 异常 + 2 难例 + 15 干扰 ----
        for i in range(15):
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v, hi=END - dt.timedelta(days=60))
            c = g.chain(q, v, g.amount(3_000, 45_000), d)
            orig_no = conn.execute("SELECT invoice_no FROM invoices WHERE id=?", (c["invoice"],)).fetchone()[0]
            dd = c["inv_date"] + dt.timedelta(days=r.randint(5, 20))
            if i < 13:
                dup = g.invoice(c["po"], v, c["inv_amount"], dd, no=_variant(orig_no, r))
                g.label("R4_DUPINV", "invoice", dup, "anomaly", "同号不同写法重复入账")
            else:
                dup = g.invoice(c["po"], v, round(c["inv_amount"] + 0.5, 2), dd, no=orig_no + "R")
                g.label("R4_DUPINV", "invoice", dup, "anomaly", "难例：号码加后缀且金额有尾差")
            g.payment(dup, round(c["inv_amount"], 2) if i < 13 else round(c["inv_amount"] + 0.5, 2),
                      dd + dt.timedelta(days=r.randint(5, 15)))
        for vendor, req, amt, months in (("V14", "U03", 12_800.0, range(1, 9)), ("V15", "U02", 9_600.0, range(1, 8))):
            for m in months:  # 干扰：月度固定服务费，同金额不同发票号
                c = g.chain(req, vendor, amt, dt.date(2026, m, 10), exact=True)
                g.label("R4_DUPINV", "invoice", c["invoice"], "distractor", "月度固定服务费")

        # ---- R5 三单匹配：14 异常 + 1 难例 + 15 干扰 ----
        for _ in range(14):
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v)
            c = g.chain(q, v, g.amount(5_000, 90_000), d, inv_factor=1 + r.uniform(0.06, 0.30))
            g.label("R5_3WAY", "invoice", c["invoice"], "anomaly", "发票超收货 >5%")
        q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v, hi=END - dt.timedelta(days=60))
        c = g.chain(q, v, g.amount(20_000, 45_000), d)
        extra = g.invoice(c["po"], v, round(c["amount"] * r.uniform(0.06, 0.12), 2),
                          c["inv_date"] + dt.timedelta(days=10))
        g.payment(extra, conn.execute("SELECT amount FROM invoices WHERE id=?", (extra,)).fetchone()[0],
                  c["inv_date"] + dt.timedelta(days=25))
        g.label("R5_3WAY", "invoice", extra, "anomaly", "难例：补开第二张发票，累计超收货但单张不超")
        for _ in range(15):
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v)
            c = g.chain(q, v, g.amount(5_000, 90_000), d, inv_factor=1 + r.uniform(0.005, 0.04))
            g.label("R5_3WAY", "invoice", c["invoice"], "distractor", "容差内差异（运费/尾差）")

        # ---- R6 付款前置：16 异常 + 15 组分期付款干扰 ----
        for i in range(16):
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v)
            c = g.chain(q, v, g.amount(5_000, 90_000), d, pay_before_receipt=i < 10, no_receipt=i >= 10)
            g.label("R6_PAY", "payment", c["payments"][0], "anomaly",
                    "付款早于收货" if i < 10 else "无收货记录即付款")
        for _ in range(15):
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v, hi=END - dt.timedelta(days=60))
            c = g.chain(q, v, g.amount(10_000, 90_000), d, partial=[0.4, 0.6])
            for p in c["payments"]:
                g.label("R6_PAY", "payment", p, "distractor", "合同分期付款")

        # ---- 正常单据链 ----
        for _ in range(n_normal):
            q = r.choice(REQUESTERS); v = gv(q); d = g.rand_date(q, v)
            x = r.random()
            amt = g.amount(2_000, 45_000) if x < 0.70 else g.amount(55_000, 180_000) if x < 0.92 else g.amount(210_000, 480_000)
            g.chain(q, v, amt, d)

    if labels_path:
        with open(labels_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["rule", "entity_type", "entity_id", "kind", "note"])
            w.writeheader()
            w.writerows(g.labels)
    return g.labels


def reset_and_build(path=None):
    path = path or config.DB_PATH
    import os
    if os.path.exists(path):
        os.remove(path)
    conn = db.connect(path)
    labels = build(conn, config.LABELS_PATH)
    return conn, labels
