"""研判器对比：在同一批规则候选上比较不同研判器（启发式 / Claude Code / API）。

研判任务：给定规则命中，判断“违规 / 不违规 / 不确定”。以合成标注为准：
候选在标注异常集中 → 应判“违规”；否则（干扰样本、正常单据）→ 应判“不违规”。

默认候选取 v1 规则命中（误报多，能看出研判器能否筛掉误报），按“真异常 / 误报”分层抽样，
控制调用次数。每条结果写入 reports/judge_eval_<时间>.jsonl，便于复查。

用法：
  python -m evaluation.judge_eval --judges heuristic claude-code:sonnet claude-code:opus --limit 40
  python -m evaluation.judge_eval --judges heuristic api:claude-opus-5-5 --candidates v2
"""
import argparse
import datetime as dt
import json
import random
import time

from agent import pipeline
from agent.judge import get_judge
from p2p import config, db, seed
from p2p.rules import run_rules


def sample_candidates(conn, labels, version="v1", limit=40, seed_=7):
    gold = {(l["rule"], l["entity_id"]) for l in labels if l["kind"] == "anomaly"}
    hits = run_rules(conn, version)
    pos = [h for h in hits if (h["rule"], h["entity_id"]) in gold]
    neg = [h for h in hits if (h["rule"], h["entity_id"]) not in gold]
    r = random.Random(seed_)
    if limit and len(hits) > limit:
        k = min(len(pos), limit // 2)
        pos, neg = r.sample(pos, k), r.sample(neg, min(len(neg), limit - k))
    return [(h, True) for h in pos] + [(h, False) for h in neg]


def evaluate_judge(conn, judge, cands, log=None):
    rows = []
    for h, truth in cands:
        t0 = time.time()
        res = pipeline.analyse(conn, h, judge)
        rows.append({"rule": h["rule"], "entity_id": h["entity_id"], "truth": truth, "verdict": res["verdict"],
                     "confidence": res["confidence"], "cited": res["cited"], "route": res["route"],
                     "route_reasons": res["route_reasons"], "reasoning": res["reasoning"],
                     "failed": res["reasoning"].startswith("LLM 研判失败"), "latency": round(time.time() - t0, 2)})
        if log:
            log.write(json.dumps({"judge": judge.name, **rows[-1]}, ensure_ascii=False) + "\n")
    n = len(rows)
    pred = [r for r in rows if r["verdict"] == "违规"]
    tp = sum(r["truth"] for r in pred)
    pos = sum(r["truth"] for r in rows)
    agree = sum((r["verdict"] == "违规") == r["truth"] and r["verdict"] != "不确定" for r in rows)
    auto = [r for r in rows if r["route"] == "自动确认"]
    return {
        "judge": judge.name, "n": n,
        "agreement": agree / n if n else 0,
        "precision": tp / len(pred) if pred else 0,
        "recall": tp / pos if pos else 0,
        "uncertain_rate": sum(r["verdict"] == "不确定" for r in rows) / n if n else 0,
        "out_of_evidence_rate": sum("引用证据外条款" in r["route_reasons"] for r in rows) / n if n else 0,
        "fail_rate": sum(r["failed"] for r in rows) / n if n else 0,
        "auto_rate": len(auto) / n if n else 0,
        "auto_precision": (sum(r["truth"] for r in auto) / len(auto)) if auto else None,
        "avg_latency": sum(r["latency"] for r in rows) / n if n else 0,
        "errors": [r for r in rows if (r["verdict"] == "违规") != r["truth"]][:10],
    }


def to_markdown(results, version, n_cands, n_pos):
    L = [f"# 研判器对比（候选：{version} 规则命中抽样 {n_cands} 条，其中真异常 {n_pos} 条）", "",
         "| 研判器 | 一致率 | “违规”精确率 | “违规”召回率 | 不确定率 | 引用证据外条款 | 失败率 | 自动确认率 | 自动确认精确率 | 平均耗时(s) |",
         "|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        ap = f"{r['auto_precision']:.2f}" if r["auto_precision"] is not None else "-"
        L.append(f"| {r['judge']} | {r['agreement']:.2f} | {r['precision']:.2f} | {r['recall']:.2f} | "
                 f"{r['uncertain_rate']:.2f} | {r['out_of_evidence_rate']:.2f} | {r['fail_rate']:.2f} | "
                 f"{r['auto_rate']:.2f} | {ap} | {r['avg_latency']:.1f} |")
    L += ["", "一致率：研判结论（违规/不违规）与标注一致的比例，“不确定”计为不一致。",
          "启发式研判对所有命中都判“违规”，它的精确率就是候选本身的精确率，可作基线。", ""]
    for r in results:
        if r["errors"]:
            L.append(f"## {r['judge']} 判错样例")
            L += [f"- {e['rule']} {e['entity_id']}：标注={'异常' if e['truth'] else '非异常'}，"
                  f"研判={e['verdict']}（{e['confidence']}）{e['reasoning']}" for e in r["errors"]]
            L.append("")
    L.append("> 合成数据上的结果，只用于比较研判器之间的相对表现。")
    return "\n".join(L)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--judges", nargs="+", default=["heuristic"])
    ap.add_argument("--candidates", default="v1", choices=["v1", "v2"])
    ap.add_argument("--limit", type=int, default=40, help="抽样条数（0 = 全部）")
    a = ap.parse_args(argv)
    conn = db.connect(":memory:")
    labels = seed.build(conn)
    cands = sample_candidates(conn, labels, a.candidates, a.limit)
    config.REPORT_DIR.mkdir(exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    with open(config.REPORT_DIR / f"judge_eval_{stamp}.jsonl", "w", encoding="utf-8") as log:
        for spec in a.judges:
            j = get_judge(spec)
            print(f"运行 {j.name}（{len(cands)} 条）…", flush=True)
            results.append(evaluate_judge(conn, j, cands, log))
    md = to_markdown(results, a.candidates, len(cands), sum(t for _, t in cands))
    (config.REPORT_DIR / f"judge_eval_{stamp}.md").write_text(md, encoding="utf-8")
    print(md)
    return results


if __name__ == "__main__":
    main()
