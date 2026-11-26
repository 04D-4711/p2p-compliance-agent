# ERP 采购到付款（P2P）财务合规审查 Agent：长期记忆与上下文管理

本地可运行的原型，复现 ERP **PO → 收货 → 发票 → 付款**单据流。研判流程用 **LangGraph** 编排，
Agent 带**长期记忆**（把人工复核结论沉淀为先例，相似案件直接复用）和**上下文管理**（在 token 预算内组装提示词）。

提供两类合规控制：

- **事前拦截**：录单时校验职责分离、越权审批、拆单、重复发票、三单匹配、付款前置条件；一旦命中就拦下单据并引用制度条款。
- **事后巡检**：Agent 工作流依次做 SQL 规则初筛、条款检索取证、记忆检索、研判和人审路由，结果进入风险看板。看板上的案件按“风险识别 → 异常确认 → 整改推进 → 复测验收 → 证据沉淀”流转，形成闭环；人审结论写回长期记忆。

## 长期记忆与上下文管理

```
gather ─→ recall ─┬─(先例达成共识且非大额)─→ memory_judge ─┐
                  └──────────→ build_context ─→ llm_judge ─┴─→ route ─→ 人审 ─→ 写入记忆
```

| 模块 | 做法 |
|---|---|
| 先例记忆（情景） | 人审结论按“规则 + 特征签名 + 结论 + 复核意见”存储；新命中按特征相似度检索，加权投票（相似度 × 支持数 × 时间衰减） |
| 实体事实（语义） | 复核时可登记供应商级事实（如“V14 为月度固定服务费合同”），按实体检索后放入 LLM 上下文 |
| 写入策略 | 只接受人工复核结论，LLM / 自动确认结论不回写，防止模型错误自我强化；近似同结论先例合并累加支持数 |
| 冲突与纠错 | 近邻先例结论不一致 → 判“先例冲突”转人工；被记忆自动排除的案件可人工纠正，纠正结论同样入库；记忆可撤销，所有变更写审计日志 |
| 失效 | 时间衰减（半衰期 180 天）；制度文本哈希变化后旧记忆不再参与研判；每条规则容量上限，淘汰权重最低的 |
| 记忆研判 | 相似先例有效支持 ≥ 2.5 且一致度 ≥ 0.9 → 直接按记忆确认 / 排除，**不调用 LLM**；大额（≥ 10 万）始终人审 |
| 冷启动 | 启用记忆时，没有可靠先例的命中转人工（“无可靠先例”），人审结论积累后自动化比例逐步上升 |
| 上下文管理 | 按优先级组装提示词：命中单据、特征、首条条款必选；单据链先裁剪字段，超预算再摘要长列表、删减先例 / 事实 / 条款，记录降级步骤 |

### 评测（`python -m p2p.cli memory-eval`，报告见 `reports/memory_eval_v1.md`）

用 v1 规则的 440 条命中（88 条真异常、352 条误报）按单据日期组成时间流，逐条研判；研判只能用此前人审写入的记忆，
转人工的由模拟审核员按标注给结论并写回记忆（prequential，无信息泄漏）。

| 配置 | 人审量 | 自动确认精确率 | 自动排除 | 错排真异常 | 最终召回 | 需调用 LLM |
|---|---|---|---|---|---|---|
| 无记忆（原流程） | 98（22%） | 0.24 | 0 | 0 | 1.00 | 440 |
| 全部人审 | 440（100%） | - | 0 | 0 | 1.00 | 440 |
| 记忆 | 202（46%） | 1.00 | 210 | 0 | 1.00 | 202 |
| 记忆 + 审核员 5% 错误 | 215（49%） | 1.00 | 204 | 0 | 0.94 | 215 |

- 人审率随记忆积累从首段 80% 降到末段 27%；剩余人审中 54 条是制度要求的大额复核。
- 审核员 5% 错误时，记忆仍未错排真异常；召回下降来自审核员本身判错。
- 相似度阈值从 0.8 降到 0.5 能再减少人审，但会把 2 条“间隔 9 天的拆单”难例按误报先例排除，所以默认取 0.8。
- 上下文：全部命中平均提示词 517 → 411 token；40 笔收货 + 36 期付款的长单据链 3046 → 476 token（预算 1200），命中单据及其发票完整保留。
- v2 规则本身已很精确，启用记忆反而因冷启动增加人审（见 `reports/memory_eval_v2.md`）。记忆的价值在规则宽、误报多的场景。
- 合成数据，特征签名由作者按规则设计；结果只说明机制可行，不代表真实业务效果。

## 快速开始

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1            # Windows PowerShell；macOS/Linux：source .venv/bin/activate
$env:PYTHONUTF8 = "1"                  # Windows：让 Python 默认用 UTF-8（每个新终端设置一次）
python -m pip install -r requirements.txt

python -m p2p.cli init                 # 生成 data/p2p.db 与 data/labels.csv
python -m streamlit run app.py         # 打开界面（首次启动若无数据库会自动生成）

python -m p2p.cli scan --sync          # 命令行巡检并写入看板
python -m p2p.cli scan --memory       # 启用长期记忆巡检（冷启动时全部转人工）
python -m p2p.cli memory               # 查看记忆；--revoke <id> 撤销
python -m p2p.cli eval                 # 规则评估，输出 reports/eval_report.md
python -m p2p.cli memory-eval          # 记忆与上下文管理评测，输出 reports/memory_eval_v1.md
python -m pytest                       # 41 个测试（Windows 上跳过 4 个假 CLI 测试；pyarrow 无法加载时再跳过 4 个界面测试）
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

## Windows 常见问题

| 现象 | 原因 | 解决 |
|---|---|---|
| `'gbk' codec can't decode` | Windows 默认用 GBK 读写文本 | 每个新终端先运行 `$env:PYTHONUTF8 = "1"`，或运行一次 `setx PYTHONUTF8 1` 设为永久 |
| `streamlit` 无法识别 | 虚拟环境的 Scripts 目录不在 PATH 上 | 改用 `python -m streamlit run app.py` |
| `DLL load failed ... 应用程序控制策略已阻止此文件` | Windows 智能应用控制拦截了 pyarrow 的 DLL | 先试 `python -m pip install "pyarrow==24.*"`；不行就在 WSL 里运行界面。命令行功能不受影响，界面测试会自动跳过 |
| `claude-code` 研判按 API 扣费 | Claude Code 登录的是 API 计费账户 | 在 Claude Code 里 `/login`，改用 claude.ai 账户登录 |

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
  graph.py       LangGraph 研判流程：取证 → 记忆检索 →（记忆研判 | 上下文组装 → LLM 研判）→ 路由
  memory.py      长期记忆：先例 / 实体事实，合并、冲突、撤销、衰减、容量、制度版本
  context.py     上下文管理：token 预算、分级降级、单据链压缩
  features.py    规则特征提取与相似度（记忆检索签名）
  retriever.py   制度条款检索（中文字二元组 + BM25）
  judge.py       ClaudeCodeJudge（claude -p）/ ClaudeJudge（API）/ HeuristicJudge（离线兜底）
  pipeline.py    入口：规则初筛 + 逐条调用 LangGraph
evaluation/evaluate.py   v1 / v2 精确率、召回率，误报来源，路由分布
evaluation/judge_eval.py 研判器对比（启发式 / Claude Code / API）
evaluation/memory_eval.py 记忆流式评测、阈值敏感性、上下文 token 对比
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
- 启用记忆时：近邻先例结论冲突，或没有可靠先例

记忆研判为“不违规”且无上述原因时，路由为“自动排除”，案件可在看板上人工纠正。

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

- 用真实 LLM 跑记忆评测：`python -m p2p.cli memory-eval --judge claude-code:sonnet --limit 120`，比较有 / 无先例上下文时 LLM 研判的一致率
- 用 LangGraph 的 interrupt + checkpointer 把“人审”做成图内的暂停 / 恢复节点

- v3 规则：用累计发票口径（覆盖 R5 难例）、滑动窗口链式拆单（覆盖 R3 难例）、发票号编辑距离加金额容差（覆盖 R4 难例）
- 在独立留出集上重新评估，对比启发式研判和 Claude 研判（`judge-eval`）
- 预付款例外：关联合同，满足 P-12 时不命中 R6
