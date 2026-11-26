"""SQLite 连接、表结构与通用写入工具。"""
import re
import sqlite3
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id TEXT PRIMARY KEY, name TEXT, role TEXT, approval_limit REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS vendors(
  id TEXT PRIMARY KEY, name TEXT, category TEXT);
CREATE TABLE IF NOT EXISTS purchase_orders(
  id TEXT PRIMARY KEY,
  requester_id TEXT REFERENCES users(id),
  buyer_id TEXT REFERENCES users(id),
  approver_id TEXT REFERENCES users(id),
  vendor_id TEXT REFERENCES vendors(id),
  item TEXT, qty REAL, unit_price REAL, amount REAL, created_at TEXT);
CREATE TABLE IF NOT EXISTS receipts(
  id TEXT PRIMARY KEY,
  po_id TEXT REFERENCES purchase_orders(id),
  receiver_id TEXT REFERENCES users(id),
  qty REAL, amount REAL, received_at TEXT);
CREATE TABLE IF NOT EXISTS invoices(
  id TEXT PRIMARY KEY,
  po_id TEXT REFERENCES purchase_orders(id),
  vendor_id TEXT REFERENCES vendors(id),
  invoice_no TEXT, amount REAL, invoice_date TEXT,
  entered_by TEXT REFERENCES users(id),
  status TEXT DEFAULT 'valid');
CREATE TABLE IF NOT EXISTS payments(
  id TEXT PRIMARY KEY,
  invoice_id TEXT REFERENCES invoices(id),
  amount REAL, paid_at TEXT,
  paid_by TEXT REFERENCES users(id),
  status TEXT DEFAULT 'valid');
CREATE TABLE IF NOT EXISTS cases(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule TEXT, entity_type TEXT, entity_id TEXT, amount REAL, detail TEXT,
  status TEXT, route TEXT, route_reasons TEXT,
  verdict TEXT, confidence REAL, cited TEXT, evidence TEXT, reasoning TEXT, judge TEXT,
  owner TEXT, created_at TEXT, due_date TEXT, closed_at TEXT,
  UNIQUE(rule, entity_id));
CREATE TABLE IF NOT EXISTS case_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id INTEGER REFERENCES cases(id), ts TEXT, stage TEXT, action TEXT, note TEXT);
"""

PREFIX = {"purchase_orders": "PO", "receipts": "GR", "invoices": "IV", "payments": "PY"}


def norm_inv(s):
    """发票号归一化：忽略大小写、分隔符与数字段前导零。INV-2026-00123 == inv 2026 123。"""
    if s is None:
        return None
    parts = re.findall(r"[A-Z]+|\d+", str(s).upper())
    return "".join((p.lstrip("0") or "0") if p.isdigit() else p for p in parts)


def connect(path=None):
    conn = sqlite3.connect(path or config.DB_PATH, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.create_function("norm_inv", 1, norm_inv, deterministic=True)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """给旧数据库补上新增列（记忆相关）。"""
    have = {r[1] for r in conn.execute("PRAGMA table_info(cases)").fetchall()}
    for col in ("features", "memory_refs", "memory_ids"):
        if col not in have:
            conn.execute(f"ALTER TABLE cases ADD COLUMN {col} TEXT")


@contextmanager
def tx(conn):
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def next_id(conn, table):
    p = PREFIX[table]
    n = conn.execute(f"SELECT MAX(CAST(SUBSTR(id,{len(p) + 1}) AS INTEGER)) FROM {table}").fetchone()[0]
    return f"{p}{(n or 0) + 1:05d}"


def insert(conn, table, row):
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table}({cols}) VALUES({qs})", list(row.values()))
    return row.get("id")


def rows(conn, sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def is_empty(conn):
    return conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0] == 0


def chain_for(conn, entity_type, entity_id):
    """按任一单据追溯完整 PO → 收货 → 发票 → 付款 单据链。"""
    if entity_type == "po":
        po_id = entity_id
    elif entity_type == "receipt":
        po_id = conn.execute("SELECT po_id FROM receipts WHERE id=?", (entity_id,)).fetchone()[0]
    elif entity_type == "invoice":
        po_id = conn.execute("SELECT po_id FROM invoices WHERE id=?", (entity_id,)).fetchone()[0]
    else:
        po_id = conn.execute(
            "SELECT i.po_id FROM payments p JOIN invoices i ON i.id=p.invoice_id WHERE p.id=?",
            (entity_id,)).fetchone()[0]
    po = rows(conn, "SELECT * FROM purchase_orders WHERE id=?", (po_id,))
    return {
        "po": po[0] if po else None,
        "receipts": rows(conn, "SELECT * FROM receipts WHERE po_id=? ORDER BY received_at", (po_id,)),
        "invoices": rows(conn, "SELECT * FROM invoices WHERE po_id=? ORDER BY id", (po_id,)),
        "payments": rows(conn, """SELECT p.* FROM payments p JOIN invoices i ON i.id=p.invoice_id
                                  WHERE i.po_id=? ORDER BY p.paid_at""", (po_id,)),
    }
