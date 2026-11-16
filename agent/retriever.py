"""制度条款检索：中文字二元组 + BM25，无外部依赖。"""
import math
import re
from collections import Counter
from functools import lru_cache

from p2p import config


def load_policies(path=None):
    text = open(path or config.POLICY_PATH, encoding="utf-8").read()
    out = []
    for m in re.finditer(r"^## (P-\d+) (.+?)\n(.+?)(?=^## |\Z)", text, re.S | re.M):
        out.append({"id": m.group(1), "title": m.group(2).strip(), "text": m.group(3).strip()})
    return out


def tokenize(s):
    toks = [w.lower() for w in re.findall(r"[A-Za-z0-9]+", s)]
    for seg in re.sub(r"[A-Za-z0-9\W_]+", " ", s).split():
        toks += [seg[i:i + 2] for i in range(len(seg) - 1)] or [seg]
    return toks


class BM25:
    def __init__(self, docs, k1=1.5, b=0.75):
        self.docs = docs
        self.tf = [Counter(tokenize(d["title"] + " " + d["text"])) for d in docs]
        self.len = [sum(t.values()) for t in self.tf]
        self.avg = sum(self.len) / len(self.len)
        df = Counter(w for t in self.tf for w in t)
        n = len(docs)
        self.idf = {w: math.log(1 + (n - f + 0.5) / (f + 0.5)) for w, f in df.items()}
        self.k1, self.b = k1, b

    def search(self, query, k=3):
        q = tokenize(query)
        scores = []
        for i, tf in enumerate(self.tf):
            s = 0.0
            for w in q:
                if w in tf:
                    f = tf[w]
                    s += self.idf[w] * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * self.len[i] / self.avg))
            scores.append((s, i))
        scores.sort(reverse=True)
        return [dict(self.docs[i], score=round(s, 3)) for s, i in scores[:k] if s > 0]


@lru_cache(maxsize=1)
def default_index():
    return BM25(load_policies())


def retrieve(query, k=3):
    return default_index().search(query, k)


def policy_by_id():
    return {p["id"]: p for p in default_index().docs}
