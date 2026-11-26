"""命令行：python -m p2p.cli {init,scan,cases,export,eval,judge-eval,memory,memory-eval}"""
import argparse
import json

from . import cases, config, db, seed


def main():
    ap = argparse.ArgumentParser(prog="p2p")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="重置数据库并生成合成数据")
    s = sub.add_parser("scan", help="运行巡检 Agent")
    s.add_argument("--version", default="v2", choices=["v1", "v2"])
    s.add_argument("--judge", default="auto",
                   help="auto | heuristic | claude-code[:sonnet|opus|haiku] | api[:模型名]")
    s.add_argument("--rules", nargs="*", help="只跑指定规则，如 R2_AUTH R5_3WAY")
    s.add_argument("--sync", action="store_true", help="结果写入风险看板")
    s.add_argument("--memory", action="store_true", help="启用长期记忆（检索人审先例；无可靠先例转人工）")
    m = sub.add_parser("memory", help="查看长期记忆")
    m.add_argument("--revoke", type=int, help="撤销指定记忆")
    sub.add_parser("memory-eval", help="记忆与上下文管理评测，参数同 python -m evaluation.memory_eval")
    sub.add_parser("cases", help="列出案件")
    e = sub.add_parser("export", help="导出证据包")
    e.add_argument("case_id", type=int)
    sub.add_parser("eval", help="运行评估并写 reports/eval_report.md")
    sub.add_parser("judge-eval", help="研判器对比，参数同 python -m evaluation.judge_eval")
    a, extra = ap.parse_known_args()
    if extra and a.cmd not in ("judge-eval", "memory-eval"):
        ap.error(f"unrecognized arguments: {' '.join(extra)}")

    if a.cmd == "init":
        _, labels = seed.reset_and_build()
        print(f"已生成 {config.DB_PATH}，标注 {len(labels)} 条 → {config.LABELS_PATH}")
    elif a.cmd == "scan":
        from agent import pipeline
        conn = db.connect()
        res = pipeline.run(conn, a.version, a.judge, a.rules or None, memory=a.memory, explore=a.memory)
        for r in res:
            print(f"{r['rule']:<10} {r['entity_id']:<8} {r['amount']:>12,.2f}  {r['route']:<5} "
                  f"conf={r['confidence']:.2f} cited={','.join(r['cited'])}  {r['detail']}")
        print(f"\n共 {len(res)} 条；自动确认 {sum(r['route'] == '自动确认' for r in res)}，"
              f"自动排除 {sum(r['route'] == '自动排除' for r in res)}，"
              f"转人工 {sum(r['route'] == '人工复核' for r in res)}，调用研判器 {sum(r['judge'] != 'memory' for r in res)}")
        if a.sync:
            print("新建案件", cases.sync(conn, res))
    elif a.cmd == "cases":
        for c in cases.list_cases(db.connect()):
            print(json.dumps({k: c[k] for k in ("id", "rule", "entity_id", "status", "owner", "due_date")},
                             ensure_ascii=False))
    elif a.cmd == "export":
        cases.evidence_pack(db.connect(), a.case_id)
        print(config.EXPORT_DIR / f"case_{a.case_id}_evidence.zip")
    elif a.cmd == "memory":
        from agent.memory import MemoryStore
        store = MemoryStore(db.connect())
        if a.revoke:
            store.revoke(a.revoke, "命令行撤销")
        for mm in store.list():
            print(json.dumps({k: mm[k] for k in ("id", "kind", "rule", "scope_id", "signature", "verdict", "support",
                                                 "note")}, ensure_ascii=False))
    elif a.cmd == "memory-eval":
        from evaluation import memory_eval
        memory_eval.main(extra)
    elif a.cmd == "judge-eval":
        from evaluation import judge_eval
        judge_eval.main(extra)
    elif a.cmd == "eval":
        from evaluation.evaluate import evaluate, to_markdown
        md = to_markdown(evaluate())
        config.REPORT_DIR.mkdir(exist_ok=True)
        (config.REPORT_DIR / "eval_report.md").write_text(md, encoding="utf-8")
        print(md)


if __name__ == "__main__":
    main()
