"""全局参数：审批权限、容差、路由阈值等。可用环境变量覆盖数据库路径。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = os.getenv("P2P_DB", str(ROOT / "data" / "p2p.db"))
LABELS_PATH = ROOT / "data" / "labels.csv"
POLICY_PATH = ROOT / "policies" / "p2p_policy.md"
EXPORT_DIR = ROOT / "exports"
REPORT_DIR = ROOT / "reports"

L1_LIMIT = 50_000          # 一级审批权限（P-02）
TOLERANCE = 0.05           # 三单匹配容差（P-05）
SPLIT_WINDOW_DAYS = 7      # 拆单识别窗口（P-03）
HIGH_AMOUNT = 100_000      # 大额异常须人工复核（P-09）
CONF_THRESHOLD = 0.75      # 低于该置信度转人工
DUE_DAYS = 14              # 整改时限（约 10 个工作日，P-07）

RULE_PARAMS = {"l1": L1_LIMIT, "tol": TOLERANCE, "win": SPLIT_WINDOW_DAYS}
