"""Agent 工作流入口：SQL 规则初筛 → （LangGraph）取证 → 记忆检索 → 研判 → 人审路由。

流程编排见 agent/graph.py，长期记忆见 agent/memory.py，上下文管理见 agent/context.py。
"""
from p2p.rules import run_rules

from .context import DEFAULT_BUDGET
from .features import extract as features  # noqa: F401  兼容旧接口
from .graph import build_graph, route, to_result  # noqa: F401
from .judge import get_judge
from .memory import MemoryStore


class Agent:
    """可复用的研判 Agent：编译一次图，逐条处理命中。"""

    def __init__(self, conn, judge="auto", memory=False, budget=DEFAULT_BUDGET, explore=False):
        self.judge = get_judge(judge) if isinstance(judge, str) else judge
        if memory is True:
            memory = MemoryStore(conn)
        self.store = memory or None
        self.graph = build_graph(conn, self.judge, self.store, budget, explore)

    def analyse(self, hit, today=None):
        return to_result(self.graph.invoke({"hit": hit, "today": today}))


def analyse(conn, hit, judge, memory=False):
    return Agent(conn, judge, memory).analyse(hit)


def run(conn, version="v2", judge="auto", rules=None, memory=False, explore=False):
    agent = Agent(conn, judge, memory, explore=explore)
    return [agent.analyse(h) for h in run_rules(conn, version, rules)]
