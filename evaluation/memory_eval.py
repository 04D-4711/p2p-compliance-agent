"""记忆与上下文管理评测。

一、流式评测（prequential，无信息泄漏）
把规则命中按单据日期排成一条时间流，逐条研判：
  研判时只能看到“此前”人审写入的记忆 → 转人工的由模拟审核员按标注给出结论 → 结论写入记忆。
审核员可设错误率（如 5%），检验记忆在错误标注下是否稳健。

对比配置：
  - 无记忆（原流程）：启发式研判 + 原路由
  - 全部人审：所有命中都交给人（人审量上限，精确率上限）
  - 记忆：无可靠先例转人工，先例达成共识后按记忆自动确认 / 自动排除
  - 记忆 + 审核员 5% 错误

二、上下文管理
  - 全部命中：原提示词（完整 JSON 单据链）与预算内提示词的 token 对比
  - 长单据链压力测试：构造 40 笔收货 + 36 期付款的订单，检查预算是否守住、命中单据是否保留

用法：
  python -m evaluation.memory_eval                       # 启发式研判，v1 命中（误报多，最能体现记忆作用）
  python -m evaluation.memory_eval --version v2
  python -m evaluation.memory_eval --judge claude-code:sonnet --limit 120   # 用真实 LLM（会产生调用）
报告写入 reports/memory_eval_<版本>[_<研判器>][_n<条数>].md，不会覆盖启发式版本的报告。
"""
import argparse
import datetime as dt
import random
from statistics import mean

from agent import context as ctxmod
from agent.features import signature
from agent.graph import entity_date
from agent.memory import MemoryStore
from agent.pipeline import Agent
from p2p import config, db, seed
from p2p.rules import run_rules


def stream(conn, version):
    hits = run_rules(conn, version)
    for h in hits:
        h["_date"] = entity_date(h, db.chain_for(conn, h["entity_type"], h["entity_id"]))
    return sorted(hits, key=lambda h: (h["_date"], h["rule"], h["entity_id"]))


class Reviewer:
    """模拟审核员：按标注给结论，可设错误率；月度固定服务费等登记为供应商事实。"""

    def __init__(self, gold, notes, error_rate=0.0, seed_=11):
        self.gold, self.notes, self.err, self.r = gold, notes, error_rate, random.Random(seed_)

    def review(self, hit, chain):
        key = (hit["rule"], hit["entity_id"])
        truth = key in self.gold
        said = (not truth) if self.r.random() < self.err else truth
        note = self.notes.get(key, "") or ("" if truth else "正常业务单据")
        fact = None
        if not said and "月度固定服务费" in note:
            vid = chain["po"]["vendor_id"]
            fact = ("vendor", vid, f"供应商 {vid} 为月度固定服务费合同，同金额不同号码的发票属正常")
        return ("违规" if said else "不违规"), note, fact


def run_config(conn, hits, gold, notes, judge="heuristic", memory=False, error_rate=0.0, limit=None):
    store = MemoryStore(conn) if memory else None
    if store:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM memory_events")
    agent = Agent(conn, judge, store or False, explore=bool(memory))
    rev = Reviewer(gold, notes, error_rate)
    rows = []
    for h in hits[:limit] if limit else hits:
        hit = {k: v for k, v in h.items() if k != "_date"}
        res = agent.analyse(hit, today=h["_date"])
        truth = (h["rule"], h["entity_id"]) in gold
        human = res["route"] == "人工复核"
        said = None
        if human:
            chain = db.chain_for(conn, h["entity_type"], h["entity_id"])
            said, note, fact = rev.review(hit, chain)
            if store:
                store.write_review(h["rule"], signature(h["rule"], res["features"]), said, note,
                                   ref=f"{h['rule']}:{h['entity_id']}", today=h["_date"], fact=fact)
        final = said == "违规" if human else res["route"] == "自动确认"
        rows.append({"truth": truth, "route": res["route"], "high": "大额" in res["route_reasons"], "final": final, "judge": res["judge"],
                     "llm": res["judge"] != "memory", "tokens": res["prompt_tokens"],
                     "raw_tokens": res["raw_prompt_tokens"]})
    n, pos = len(rows), sum(r["truth"] for r in rows)
    auto = [r for r in rows if r["route"] == "自动确认"]
    dis = [r for r in rows if r["route"] == "自动排除"]
    hum = [r for r in rows if r["route"] == "人工复核"]
    flagged = [r for r in rows if r["final"]]
    q = max(1, n // 5)
    curve = [mean(r["route"] == "人工复核" for r in rows[i:i + q]) for i in range(0, q * 5, q)]
    mem = {}
    if store:
        st = store.stats()
        mem = {"precedents": sum(s["count"] for s in st if s["kind"] == "precedent" and s["status"] == "active"),
               "facts": sum(s["count"] for s in st if s["kind"] == "fact" and s["status"] == "active")}
    return {"n": n, "pos": pos, "human": len(hum), "human_high": sum(r["high"] for r in hum), "auto": len(auto),
            "auto_precision": mean(r["truth"] for r in auto) if auto else None,
            "dismissed": len(dis), "dismissed_true": sum(r["truth"] for r in dis),
            "recall": sum(r["final"] and r["truth"] for r in rows) / pos if pos else 0,
            "precision": mean(r["truth"] for r in flagged) if flagged else 0,
            "llm_calls": sum(r["llm"] for r in rows), "curve": curve, "memory": mem}


def all_human(hits, gold):
    pos = sum((h["rule"], h["entity_id"]) in gold for h in hits)
    return {"n": len(hits), "pos": pos, "human": len(hits), "human_high": None, "auto": 0, "auto_precision": None, "dismissed": 0,
            "dismissed_true": 0, "recall": 1.0, "precision": 1.0, "llm_calls": len(hits), "curve": [1.0] * 5,
            "memory": {}}


def long_chain_stress(budget=ctxmod.DEFAULT_BUDGET):
    """构造一张长单据链，比较原提示词与预算内提示词。"""
    conn = db.connect(":memory:")
    seed.build(conn, n_normal=0)
    d0 = dt.date(2026, 1, 5)
    with db.tx(conn):
        db.insert(conn, "purchase_orders", dict(id="PO99999", requester_id="U01", buyer_id="U04", approver_id="U07",
                                                vendor_id="V03", item="仓储配送", qty=40, unit_price=4000, amount=160000,
                                                created_at=d0.isoformat()))
        for i in range(40):
            db.insert(conn, "receipts", dict(id=f"GR9{i:04d}", po_id="PO99999", receiver_id="U09", qty=1,
                                             amount=4000, received_at=(d0 + dt.timedelta(days=7 + i * 7)).isoformat()))
        db.insert(conn, "invoices", dict(id="IV99999", po_id="PO99999", vendor_id="V03", invoice_no="INV-2026-99999",
                                         amount=160000, invoice_date=(d0 + dt.timedelta(days=10)).isoformat(),
                                         entered_by="U11"))
        for i in range(36):
            db.insert(conn, "payments", dict(id=f"PY9{i:04d}", invoice_id="IV99999", amount=4500,
                                             paid_at=(d0 + dt.timedelta(days=3 + i * 7)).isoformat(), paid_by="U12"))
    hits = [h for h in run_rules(conn, "v2", ["R6_PAY"]) if h["entity_id"].startswith("PY9")]
    h = hits[-1]
    res = Agent(conn, "heuristic").graph.invoke({"hit": h})
    c = res["context"]
    return {"hit": h["entity_id"], "detail": h["detail"], "raw": c.raw_tokens, "managed": c.tokens,
            "budget": budget, "steps": c.steps, "kept_hit": h["entity_id"] in c.prompt and "IV99999" in c.prompt}


def sweep(conn, hits, gold, notes, judge, thresholds=(0.8, 0.67, 0.5)):
    """相似度阈值敏感性：阈值越低，记忆覆盖越广，但越可能把真异常错排。"""
    import agent.memory as M
    old, out = M.SIM_THRESHOLD, []
    try:
        for th in thresholds:
            M.SIM_THRESHOLD = th
            for err in (0.0, 0.05):
                r = run_config(conn, hits, gold, notes, judge, memory=True, error_rate=err)
                out.append((th, err, r))
    finally:
        M.SIM_THRESHOLD = old
    return out


def to_markdown(version, judge, results, stress, tokens, sw=()):
    L = [f"# 记忆与上下文管理评测（{version} 规则命中，研判器 {judge}）", "",
         f"命中 {results[0][1]['n']} 条，其中标注异常 {results[0][1]['pos']} 条。按单据日期组成时间流逐条处理，"
         "研判只能使用此前人审写入的记忆。", "",
         "| 配置 | 人审量 | 其中大额 | 自动确认 | 自动确认精确率 | 自动排除 | 错排真异常 | 最终召回 | 最终精确率 | 需调用 LLM | 记忆条数 |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for name, r in results:
        ap = f"{r['auto_precision']:.2f}" if r["auto_precision"] is not None else "-"
        mem = f"{r['memory'].get('precedents', 0)} 先例 / {r['memory'].get('facts', 0)} 事实" if r["memory"] else "-"
        hh = "-" if r["human_high"] is None else r["human_high"]
        L.append(f"| {name} | {r['human']}（{r['human'] / r['n']:.0%}） | {hh} | {r['auto']} | {ap} | {r['dismissed']} | "
                 f"{r['dismissed_true']} | {r['recall']:.2f} | {r['precision']:.2f} | {r['llm_calls']} | {mem} |")
    L += ["", "人审率学习曲线（时间流五等分）：", "", "| 配置 | 1 | 2 | 3 | 4 | 5 |", "|---|---|---|---|---|---|"]
    L += [f"| {name} | " + " | ".join(f"{x:.0%}" for x in r["curve"]) + " |" for name, r in results]
    if sw:
        L += ["", "## 相似度阈值敏感性（默认 0.8）", "",
              "| 阈值 | 审核员错误率 | 人审量 | 自动排除 | 错排真异常 | 最终召回 |", "|---|---|---|---|---|---|"]
        L += [f"| {th} | {err:.0%} | {r['human']} | {r['dismissed']} | {r['dismissed_true']} | {r['recall']:.2f} |"
              for th, err, r in sw]
        L += ["", "阈值调低能进一步减少人审，但开始把真异常（如间隔 9 天的拆单难例）按误报先例排除；"
              "默认取 0.8，宁可多转人工。阈值未在本评测集上调优。"]
    L += ["", "## 上下文管理", "",
          f"- 全部命中平均提示词 token：原版（完整 JSON 单据链）{tokens['raw']:.0f} → 预算内 {tokens['managed']:.0f}"
          f"（{1 - tokens['managed'] / tokens['raw']:.0%} 减少），最大 {tokens['max']}，预算 {ctxmod.DEFAULT_BUDGET}",
          f"- 长单据链压力测试（40 笔收货 + 36 期付款，命中 {stress['hit']}：{stress['detail']}）："
          f"原版 {stress['raw']} → {stress['managed']} token；降级步骤 {stress['steps']}；"
          f"命中付款及其发票{'完整保留' if stress['kept_hit'] else '未保留（异常）'}", "",
          "## 说明", "",
          "- 指标定义：人审量 = 路由到人工复核的条数；最终召回 / 精确率按“自动确认 + 人审判定违规”计算；"
          "需调用 LLM = 未被记忆直接研判、需要走研判器的条数（启发式研判时为假想调用数）。",
          "- 记忆只从人审结论写入，自动确认和 LLM 结论不回写；大额（≥10 万）始终人审。",
          "- 数据为合成数据，特征签名由作者按规则设计。结果说明机制有效，不代表真实业务效果。"]
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default="v1", choices=["v1", "v2"])
    ap.add_argument("--judge", default="heuristic")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args(argv)
    conn = db.connect(":memory:")
    labels = seed.build(conn)
    MemoryStore(conn)
    gold = {(l["rule"], l["entity_id"]) for l in labels if l["kind"] == "anomaly"}
    notes = {(l["rule"], l["entity_id"]): l["note"] for l in labels}
    hits = stream(conn, a.version)[:a.limit or None]
    base = run_config(conn, hits, gold, notes, a.judge)
    results = [("无记忆（原流程）", base), ("全部人审", all_human(hits, gold)),
               ("记忆", run_config(conn, hits, gold, notes, a.judge, memory=True)),
               ("记忆 + 审核员 5% 错误", run_config(conn, hits, gold, notes, a.judge, memory=True, error_rate=0.05))]
    agent = Agent(conn, "heuristic")
    toks = [agent.graph.invoke({"hit": {k: v for k, v in h.items() if k != "_date"}})["context"] for h in hits]
    tokens = {"raw": mean(c.raw_tokens for c in toks), "managed": mean(c.tokens for c in toks),
              "max": max(c.tokens for c in toks)}
    sw = sweep(conn, hits, gold, notes, a.judge) if a.judge == "heuristic" else ()
    md = to_markdown(a.version, a.judge, results, long_chain_stress(), tokens, sw)
    config.REPORT_DIR.mkdir(exist_ok=True)
    suffix = "" if a.judge == "heuristic" else "_" + a.judge.replace(":", "-")
    if a.limit:
        suffix += f"_n{a.limit}"
    (config.REPORT_DIR / f"memory_eval_{a.version}{suffix}.md").write_text(md, encoding="utf-8")
    print(md)
    return results


if __name__ == "__main__":
    main()
