"""长期记忆：把人工复核结论沉淀为“先例”，供后续研判检索。

两类记忆
- 先例（precedent，情景记忆）：规则 + 特征签名 + 人审结论 + 复核意见。新命中按特征相似度检索先例。
- 事实（fact，语义记忆）：复核时登记的实体级事实，如“供应商 V14 为月度固定服务费合同”，
  按供应商 / 人员检索，作为上下文提供给 LLM。

写入策略（防止记忆被污染）
- 只接受人工复核来源（source="human"）。LLM 或自动确认的结论不回写，避免模型错误自我强化。
- 合并：与已有同结论先例几乎相同（相似度 ≥ MERGE_SIM）时只累加支持数，不新增条目。
- 冲突：结论相反的先例并存，检索时按“相似度 × 支持数 × 时间衰减”加权投票，一致度不够即判冲突转人工。
- 撤销：复核人可撤销错误记忆（status=revoked），所有变更写入 memory_events 审计表。
- 制度版本：每条记忆记录写入时的制度文本哈希；制度修改后旧记忆不再参与研判（stale）。
- 容量：每条规则最多 MAX_PER_RULE 条，超出时淘汰权重最低的。
"""
import datetime as dt
import hashlib
import json

from p2p import config

from .features import SCHEMA, similarity

SIM_THRESHOLD = 0.8     # 视为“相似先例”的下限
MERGE_SIM = 0.97        # 视为同一先例、合并支持数的下限
MIN_SUPPORT = 2.5       # 记忆研判至少需要的有效支持数（约 3 次近期一致人审，计入时间衰减）
MIN_AGREEMENT = 0.9     # 记忆研判至少需要的投票一致度
HALF_LIFE_DAYS = 180    # 时间衰减半衰期
MAX_PER_RULE = 300
MAX_REFS = 20

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT, rule TEXT, scope_type TEXT, scope_id TEXT,
  signature TEXT, verdict TEXT, note TEXT, support REAL DEFAULT 1,
  refs TEXT, created_at TEXT, updated_at TEXT, last_used_at TEXT, use_count INTEGER DEFAULT 0,
  policy_version TEXT, status TEXT DEFAULT 'active');
CREATE INDEX IF NOT EXISTS ix_mem_rule ON memories(rule, kind, status);
CREATE TABLE IF NOT EXISTS memory_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, memory_id INTEGER, ts TEXT, action TEXT, note TEXT);
"""


def policy_version(path=None):
    return hashlib.sha1(open(path or config.POLICY_PATH, "rb").read()).hexdigest()[:8]


def _date(x):
    return dt.date.fromisoformat(str(x)[:10])


def decay(updated_at, today):
    age = max(0, (_date(today) - _date(updated_at)).days)
    return 0.5 ** (age / HALF_LIFE_DAYS)


class MemoryStore:
    def __init__(self, conn, version=None):
        self.conn = conn
        for stmt in filter(str.strip, SCHEMA_SQL.split(";")):  # 不用 executescript：它会提交外层事务
            conn.execute(stmt)
        self.version = version or policy_version()

    # ------------------------------------------------------------ 写入
    def _event(self, mid, action, note="", ts=None):
        self.conn.execute("INSERT INTO memory_events(memory_id, ts, action, note) VALUES(?,?,?,?)",
                          (mid, str(ts or dt.date.today()), action, note))

    def _rows(self, sql, params=()):
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            d = dict(r)
            d["signature"] = json.loads(d["signature"] or "{}")
            d["refs"] = json.loads(d["refs"] or "[]")
            out.append(d)
        return out

    def _active(self, rule, kind):
        return self._rows("SELECT * FROM memories WHERE rule=? AND kind=? AND status='active' AND policy_version=?",
                          (rule, kind, self.version))

    def write_review(self, rule, signature, verdict, note="", ref=None, today=None, fact=None, source="human"):
        """写入一条人审结论。fact=(scope_type, scope_id, text) 时同时登记实体事实。

        返回 {"action": "insert"|"merge", "id": ..., "conflicts": 相反结论的近邻数}。
        """
        if source != "human":
            raise ValueError("记忆只接受人工复核结论，拒绝写入来源：" + str(source))
        if verdict not in ("违规", "不违规"):
            raise ValueError("先例结论只能是 违规 / 不违规")
        today = str(today or dt.date.today())
        sig = {k: signature.get(k) for k in SCHEMA[rule]}
        best, conflicts = None, 0
        for m in self._active(rule, "precedent"):
            s = similarity(rule, sig, m["signature"])
            if s >= MERGE_SIM:
                if m["verdict"] == verdict and (best is None or s > best[0]):
                    best = (s, m)
                elif m["verdict"] != verdict:
                    conflicts += 1
        if best:
            m = best[1]
            refs = (m["refs"] + [ref])[-MAX_REFS:] if ref else m["refs"]
            self.conn.execute("UPDATE memories SET support=support+1, refs=?, note=?, updated_at=? WHERE id=?",
                              (json.dumps(refs, ensure_ascii=False), note or m["note"], today, m["id"]))
            self._event(m["id"], "merge", f"{ref or ''} {note}".strip(), today)
            out = {"action": "merge", "id": m["id"], "conflicts": conflicts}
        else:
            cur = self.conn.execute(
                """INSERT INTO memories(kind, rule, scope_type, scope_id, signature, verdict, note, support, refs,
                   created_at, updated_at, policy_version) VALUES('precedent',?,'rule',?,?,?,?,1,?,?,?,?)""",
                (rule, rule, json.dumps(sig, ensure_ascii=False), verdict, note,
                 json.dumps([ref] if ref else [], ensure_ascii=False), today, today, self.version))
            out = {"action": "insert", "id": cur.lastrowid, "conflicts": conflicts}
            self._event(cur.lastrowid, "insert", f"{ref or ''} {verdict} {note}".strip(), today)
            self._evict(rule, today)
        if conflicts:
            self._event(out["id"], "conflict", f"与 {conflicts} 条相反结论的先例近似", today)
        if fact:
            self.write_fact(rule, *fact, verdict=verdict, ref=ref, today=today)
        return out

    def write_fact(self, rule, scope_type, scope_id, text, verdict="不违规", ref=None, today=None):
        today = str(today or dt.date.today())
        same = [m for m in self._active(rule, "fact")
                if m["scope_type"] == scope_type and m["scope_id"] == scope_id and m["verdict"] == verdict]
        if same:
            m = same[0]
            self.conn.execute("UPDATE memories SET support=support+1, note=?, updated_at=? WHERE id=?",
                              (text, today, m["id"]))
            self._event(m["id"], "merge", text, today)
            return m["id"]
        cur = self.conn.execute(
            """INSERT INTO memories(kind, rule, scope_type, scope_id, signature, verdict, note, support, refs,
               created_at, updated_at, policy_version) VALUES('fact',?,?,?,'{}',?,?,1,?,?,?,?)""",
            (rule, scope_type, scope_id, verdict, text, json.dumps([ref] if ref else []), today, today, self.version))
        self._event(cur.lastrowid, "insert", text, today)
        return cur.lastrowid

    def revoke(self, mid, reason="人工撤销"):
        self.conn.execute("UPDATE memories SET status='revoked' WHERE id=?", (mid,))
        self._event(mid, "revoke", reason)

    def _evict(self, rule, today):
        rows = self._active(rule, "precedent")
        if len(rows) <= MAX_PER_RULE:
            return
        rows.sort(key=lambda m: m["support"] * decay(m["updated_at"], today))
        for m in rows[:len(rows) - MAX_PER_RULE]:
            self.conn.execute("UPDATE memories SET status='evicted' WHERE id=?", (m["id"],))
            self._event(m["id"], "evict", "超出容量，淘汰权重最低的先例", today)

    def mark_stale(self):
        """制度文本变化后，把旧版本记忆标为 stale。返回条数。"""
        cur = self.conn.execute("UPDATE memories SET status='stale' WHERE status='active' AND policy_version<>?",
                                (self.version,))
        return cur.rowcount

    # ------------------------------------------------------------ 检索
    def recall(self, rule, signature, entities=None, today=None, k=5, touch=True):
        """检索相似先例与相关实体事实，并给出投票结果。

        status：consensus（可直接按记忆研判）/ conflict（近邻结论不一致）/ weak（有近邻但支持不足）/ none。
        """
        today = str(today or dt.date.today())
        sig = {k_: signature.get(k_) for k_ in SCHEMA[rule]}
        scored = []
        for m in self._active(rule, "precedent"):
            s = similarity(rule, sig, m["signature"])
            w = s * m["support"] * decay(m["updated_at"], today)
            scored.append({**m, "similarity": round(s, 3), "weight": round(w, 3)})
        scored.sort(key=lambda m: (m["similarity"], m["weight"]), reverse=True)
        close = [m for m in scored if m["similarity"] >= SIM_THRESHOLD]
        votes = {}
        for m in close:
            votes[m["verdict"]] = votes.get(m["verdict"], 0) + m["weight"]
        total = sum(votes.values())
        support = sum(m["support"] * decay(m["updated_at"], today) for m in close)
        vote = None
        if total:
            verdict = max(votes, key=votes.get)
            vote = {"verdict": verdict, "agreement": round(votes[verdict] / total, 3), "support": round(support, 2)}
        if not close:
            status = "none"
        elif vote["agreement"] < MIN_AGREEMENT:
            status = "conflict"
        elif support < MIN_SUPPORT:
            status = "weak"
        else:
            status = "consensus"
        facts = []
        for scope_type, sid in (entities or {}).items():
            facts += [f for f in self._active(rule, "fact") if f["scope_type"] == scope_type and f["scope_id"] == sid]
        top = close[:k] or scored[:min(k, 2)]
        if touch and top:
            self.conn.executemany("UPDATE memories SET use_count=use_count+1, last_used_at=? WHERE id=?",
                                  [(today, m["id"]) for m in top])
        return {"status": status, "vote": vote, "precedents": top, "facts": facts}

    # ------------------------------------------------------------ 统计
    def stats(self):
        r = self.conn.execute("""SELECT kind, status, COUNT(*), COALESCE(SUM(support),0) FROM memories
                                 GROUP BY kind, status""").fetchall()
        return [{"kind": a, "status": b, "count": c, "support": s} for a, b, c, s in r]

    def list(self, kind=None, status="active"):
        sql, p = "SELECT * FROM memories WHERE 1=1", []
        if kind:
            sql += " AND kind=?"; p.append(kind)
        if status:
            sql += " AND status=?"; p.append(status)
        return self._rows(sql + " ORDER BY rule, id", p)

    def events(self, mid=None, limit=200):
        if mid:
            return [dict(r) for r in self.conn.execute("SELECT * FROM memory_events WHERE memory_id=? ORDER BY id",
                                                       (mid,)).fetchall()]
        return [dict(r) for r in self.conn.execute("SELECT * FROM memory_events ORDER BY id DESC LIMIT ?",
                                                   (limit,)).fetchall()]
