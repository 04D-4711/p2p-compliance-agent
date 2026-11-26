"""LangGraph 的最小替代：只实现本项目用到的 StateGraph 接口，按节点顺序执行。

仅在 langgraph 无法导入时使用（见 agent/graph.py）。语义与 LangGraph 一致的部分：
节点返回的字典合并进状态；条件边由函数返回的键选择下一个节点；到达 END 结束。
"""
END = "__end__"


class StateGraph:
    def __init__(self, state_schema=None):
        self.nodes, self.edges, self.cond, self.entry = {}, {}, {}, None

    def add_node(self, name, fn):
        self.nodes[name] = fn

    def set_entry_point(self, name):
        self.entry = name

    def add_edge(self, a, b):
        self.edges[a] = b

    def add_conditional_edges(self, a, fn, mapping):
        self.cond[a] = (fn, mapping)

    def compile(self):
        return _Compiled(self)


class _Compiled:
    def __init__(self, g):
        self.g = g

    def invoke(self, state, max_steps=100):
        state, node = dict(state), self.g.entry
        for _ in range(max_steps):
            if node == END:
                return state
            state.update(self.g.nodes[node](state) or {})
            if node in self.g.cond:
                fn, mapping = self.g.cond[node]
                node = mapping[fn(state)]
            else:
                node = self.g.edges.get(node, END)
        raise RuntimeError("图执行超过最大步数")
