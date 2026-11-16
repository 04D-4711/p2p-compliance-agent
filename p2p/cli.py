"""命令行：python -m p2p.cli {init,scan,cases,export,eval}"""
import argparse
import json

from . import cases, config, db, seed


def main():
    ap = argparse.ArgumentParser(prog="p2p")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="重置数据库并生成合成数据")
    s = sub.add_parser("scan", help="运行巡检 Agent")
    s.add_argument("--version", default="v2", choices=["v1", "v2"])
    s.add_argument("--judge", default="auto", choices=["auto", "heuristic", "claude"])
    s.add_argument("--sync", action="store_true", help="结果写入风险看板")
    sub.add_parser("cases", help="列出案件")
    e = sub.add_parser("export", help="导出证据包")
    e.add_argument("case_id", type=int)
    sub.add_parser("eval", help="运行评估并写 reports/eval_report.md")
    a = ap.parse_args()

    if a.cmd == "init":
        _, labels = seed.reset_and_build()
        print(f"已生成 {config.DB_PATH}，标注 {len(labels)} 条 → {config.LABELS_PATH}")
    elif a.cmd == "scan":
        from agent import pipeline
        conn = db.connect()
        res = pipeline.run(conn, a.version, a.judge)
        for r in res:
            print(f"{r['rule']:<10} {r['entity_id']:<8} {r['amount']:>12,.2f}  {r['route']:<5} "
                  f"conf={r['confidence']:.2f} cited={','.join(r['cited'])}  {r['detail']}")
        print(f"\n共 {len(res)} 条；自动确认 {sum(r['route'] == '自动确认' for r in res)}，"
              f"转人工 {sum(r['route'] == '人工复核' for r in res)}")
        if a.sync:
            print("新建案件", cases.sync(conn, res))
    elif a.cmd == "cases":
        for c in cases.list_cases(db.connect()):
            print(json.dumps({k: c[k] for k in ("id", "rule", "entity_id", "status", "owner", "due_date")},
                             ensure_ascii=False))
    elif a.cmd == "export":
        cases.evidence_pack(db.connect(), a.case_id)
        print(config.EXPORT_DIR / f"case_{a.case_id}_evidence.zip")
    elif a.cmd == "eval":
        from evaluation.evaluate import evaluate, to_markdown
        md = to_markdown(evaluate())
        config.REPORT_DIR.mkdir(exist_ok=True)
        (config.REPORT_DIR / "eval_report.md").write_text(md, encoding="utf-8")
        print(md)


if __name__ == "__main__":
    main()
