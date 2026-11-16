# ERP 采购到付款（P2P）财务合规审查 Agent 原型

本地可运行的原型，复现 ERP **PO → 收货 → 发票 → 付款**单据流，提供两类合规控制：

- **事前拦截**：录单时校验职责分离、越权审批、拆单、重复发票、三单匹配、付款前置条件；一旦命中就拦下单据并引用制度条款。
- **事后巡检**：Agent 工作流依次做 SQL 规则初筛、条款检索取证、研判和人审路由，结果进入风险看板。看板上的案件按“风险识别 → 异常确认 → 整改推进 → 复测验收 → 证据沉淀”流转，形成闭环。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1            # Windows PowerShell；macOS/Linux：source .venv/bin/activate
$env:PYTHONUTF8 = "1"                  # Windows：让 Python 默认用 UTF-8（每个新终端设置一次）
python -m pip install -r requirements.txt

python -m p2p.cli init                 # 生成 data/p2p.db 与 data/labels.csv
python -m streamlit run app.py         # 打开界面（首次启动若无数据库会自动生成）

python -m p2p.cli scan --sync          # 命令行巡检并写入看板
python -m p2p.cli eval                 # 评估，输出 reports/eval_report.md
python -m pytest                       # 25 个测试（Windows 上跳过 4 个假 CLI 测试）
```

### 研判器

| 研判器 | 写法 | 说明 |
|---|---|---|
| 启发式 | `heuristic` | 离线兜底，所有命中都判“违规”，按特征给置信度 |
| Claude Code | `claude-code`、`claude-code:sonnet`、`claude-code:opus` | 调用本机 `claude -p`，使用已登录的订阅账户，无需 API Key |
| API | `api`、`api:claude-opus-5-5` | 需要 `ANTHROPIC_API_KEY`，按量计费；默认模型可用 `P2P_LLM_MODEL` 设置 |
| 自动 | `auto` | 有 API Key 时走 API，否则用启发式 |

```bash
# 先只跑一两条规则试一下（每条命中调用一次模型）
python -m p2p.cli scan --judge claude-code:sonnet --rules R2_AUTH

# 研判器对比：在 v1 命中（含大量误报）里分层抽 40 条，比较各研判器能否筛掉误报
python -m p2p.cli judge-eval --judges heuristic claude-code:sonnet claude-code:opus --limit 40
```

`judge-eval` 输出一致率、“违规”精确率和召回率、不确定率、引用证据外条款比例、失败率、自动确认精确率和平均耗时，结果写入 `reports/judge_eval_<时间>.md`，逐条明细写入同名 `.jsonl`。LLM 调用失败（超时、额度用尽、回复无法解析）一律判“不确定”并转人工，不会自动放行。

## 目录

```
p2p/
  db.py          表结构、发票号归一化 norm_inv、单据链追溯
  rules.py       6 条 SQL 规则（v1 初版 / v2 迭代版）
  precheck.py    事前拦截：SAVEPOINT 试写入 → 重跑 v2 规则 → 有新增命中则回滚
  cases.py       案件闭环：确认 / 误报 / 派发 / 整改 / 复测 / 逾期 / 证据包
  seed.py        合成数据与标注（90 条异常 + 95 条近似合规干扰样本 + 300 条正常单据链）
  cli.py         命令行
agent/
  retriever.py   制度条款检索（中文字二元组 + BM25）
  judge.py       ClaudeCodeJudge（claude -p）/ ClaudeJudge（API）/ HeuristicJudge（离线兜底）
  pipeline.py    初筛 → 取证 → 研判 → 人审路由
evaluation/evaluate.py   v1 / v2 精确率、召回率，误报来源，路由分布
evaluation/judge_eval.py 研判器对比（启发式 / Claude Code / API）
policies/p2p_policy.md   示例制度条款 P-01 ~ P-12
docs/user_stories.md     8 个用户故事与验收标准
app.py                   Streamlit 界面
```

## 规则

| 规则 | 条款 | v1（初版） | v2（基于误报分析迭代） |
|---|---|---|---|
| R1 职责分离 | P-01 | 申请人具备审批权限即命中 | 申请人 = 审批人，或采购员 = 收货人 |
| R2 越权审批 | P-02 | 金额 ≥ 权限 | 金额 > 权限（权限为“含”） |
| R3 拆单 | P-03 | 同申请人+供应商 30 日内 ≥2 笔 | 7 日内、单笔 ≤ 5 万、合计 > 审批人权限 |
| R4 重复发票 | P-04 | 同供应商同金额 90 日内 | 发票号归一化后相同，或同订单同金额 |
| R5 三单匹配 | P-05 | 发票 ≠ 收货 | 发票超出收货 > 5% |
| R6 付款前置 | P-06 | 付款 ≠ 发票金额或早于收货 | 早于收货 / 无收货 / 累计付款 > 发票（允许分期付款） |

事前拦截和事后巡检用的是同一套 v2 SQL，两边口径一致。

## 人审路由

满足以下任一条件即转人工复核，其余自动确认并派发整改：

- 金额 ≥ 10 万（P-09）
- 研判置信度 < 0.75
- 研判未引用条款，或引用了检索结果之外的条款（防止 LLM 编造条款）
- 研判结论不是“违规”（包括 LLM 调用失败）

## 评估结果（合成数据，`python -m p2p.cli eval`）

| 版本 | 精确率 | 召回率 | 误报数 |
|---|---|---|---|
| v1 | 0.20 | 0.98 | 352（干扰样本 93，正常单据 259） |
| v2 | 1.00 | 0.94 | 0 |

v2 漏报的 5 条是有意放入的难例，当前规则覆盖不到：

- 间隔 9 天的拆单
- 号码加后缀且金额有尾差的重复发票
- 补开第二张发票、累计超出收货金额

### 评估说明（重要）

评估集由 `p2p/seed.py` 按规则场景构造，数据和规则出自同一作者。因此 v2 精确率 1.00 是**构造出来的结果**，只能说明 v2 正确处理了预设的干扰样本，不能代表真实业务数据上的效果。要得到可信的指标，至少需要：

1. 由他人或真实/脱敏业务数据构造独立的留出集；
2. 规则迭代只使用开发集，留出集只在最后测一次；
3. 用 `judge-eval` 单独统计 LLM 研判的一致率和误判。

## 后续可做

- v3 规则：用累计发票口径（覆盖 R5 难例）、滑动窗口链式拆单（覆盖 R3 难例）、发票号编辑距离加金额容差（覆盖 R4 难例）
- 在独立留出集上重新评估，对比启发式研判和 Claude 研判（`judge-eval`）
- 预付款例外：关联合同，满足 P-12 时不命中 R6
