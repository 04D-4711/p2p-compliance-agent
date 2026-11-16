"""评估：在合成标注集上比较 v1 / v2 规则，并统计 Agent 路由结果。

用法：python -m evaluation.evaluate   （在全新内存库上运行，不影响 data/p2p.db）
"""
from collections import Counter, defaultdict

from agent import pipeline
from p2p import config, db, seed
from p2p.rules import RULES, hit_keys, run_rules


def score(pred, gold):
    tp = len(pred & gold)
    p = tp / len(pred) if pred else 0.0
    r = tp / len(gold) if gold else 0.0
    return {"tp": tp, "fp": len(pred - gold), "fn": len(gold - pred), "precision": p, "recall": r}


def evaluate(conn=None, labels=None, judge="heuristic"):
    if conn is None:
        conn = db.connect(":memory:")
        labels = seed.build(conn)
    gold = {(l["rule"], l["entity_id"]) for l in labels if l["kind"] == "anomaly"}
    distractor = {(l["rule"], l["entity_id"]) for l in labels if l["kind"] == "distractor"}
    note = {(l["rule"], l["entity_id"]): l["note"] for l in labels}
    out = {"n_anomaly": len(gold), "n_distractor": len(distractor), "versions": {}}
    for v in ("v1", "v2"):
        pred = hit_keys(run_rules(conn, v))
        per_rule = {rid: score({k for k in pred if k[0] == rid}, {k for k in gold if k[0] == rid}) for rid in RULES}
        fp = pred - gold
        out["versions"][v] = {
            "overall": score(pred, gold), "per_rule": per_rule,
            "fp_from_distractor": len(fp & distractor), "fp_from_normal": len(fp - distractor),
            "fn": sorted((k, note.get(k, "")) for k in gold - pred),
        }
    res = pipeline.run(conn, "v2", judge)
    out["routing"] = dict(Counter(r["route"] for r in res))
    reasons = Counter(x for r in res for x in r["route_reasons"])
    out["route_reasons"] = dict(reasons)
    auto = [r for r in res if r["route"] == "自动确认"]
    out["auto_precision"] = (sum((r["rule"], r["entity_id"]) in gold for r in auto) / len(auto)) if auto else None
    return out


def to_markdown(o):
    L = ["# 评估报告（合成数据）", "",
         f"标注异常 {o['n_anomaly']} 条，近似合规干扰样本 {o['n_distractor']} 条。", "",
         "| 版本 | 命中 | TP | FP | FN | 精确率 | 召回率 | 干扰样本误报 | 正常单据误报 |",
         "|---|---|---|---|---|---|---|---|---|"]
    for v, d in o["versions"].items():
        s = d["overall"]
        L.append(f"| {v} | {s['tp'] + s['fp']} | {s['tp']} | {s['fp']} | {s['fn']} | {s['precision']:.2f} | "
                 f"{s['recall']:.2f} | {d['fp_from_distractor']} | {d['fp_from_normal']} |")
    L += ["", "## 分规则", "", "| 规则 | v1 精确率 | v1 召回率 | v2 精确率 | v2 召回率 |", "|---|---|---|---|---|"]
    for rid in RULES:
        a, b = o["versions"]["v1"]["per_rule"][rid], o["versions"]["v2"]["per_rule"][rid]
        L.append(f"| {rid} {RULES[rid]['name']} | {a['precision']:.2f} | {a['recall']:.2f} | "
                 f"{b['precision']:.2f} | {b['recall']:.2f} |")
    L += ["", "## v2 漏报（难例）", ""] + [f"- {k[0]} {k[1]}：{n}" for k, n in o["versions"]["v2"]["fn"]]
    L += ["", "## Agent 人审路由（v2 命中）", "", f"- 路由分布：{o['routing']}",
          f"- 转人工原因：{o['route_reasons']}",
          f"- 自动确认部分的精确率：{o['auto_precision']:.2f}" if o["auto_precision"] is not None else "",
          "", "> 说明：评估集由 `p2p/seed.py` 生成，规则与数据出自同一作者，",
          "> 指标只反映规则在构造场景下的表现，不代表真实业务数据效果。"]
    return "\n".join(L)


if __name__ == "__main__":
    o = evaluate()
    md = to_markdown(o)
    config.REPORT_DIR.mkdir(exist_ok=True)
    (config.REPORT_DIR / "eval_report.md").write_text(md, encoding="utf-8")
    print(md)
