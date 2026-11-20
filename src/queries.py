"""4 类查询 × 2 个方案（A：关系表 + 递归 CTE；B：预计算 / ANN）。

每类查询返回 (sql, params)；params 由 bench 的采样函数给出。
"""
from __future__ import annotations

import random

import psycopg

# ---------- Q1：某企业两跳以内的联系人与企业 ----------
Q1_A = """
WITH RECURSIVE h (id, d) AS (
    SELECT e.dst, 1
      FROM edge e
     WHERE e.src = %(org)s AND e.rel IN ('employs', 'cooperates')
    UNION
    SELECT e.dst, h.d + 1
      FROM h
      JOIN edge e ON e.src = h.id
     WHERE h.d < 2 AND e.rel IN ('employs', 'cooperates')
)
SELECT h.id, n.type
  FROM h
  JOIN node n ON n.id = h.id
 WHERE n.type IN ('person', 'org') AND h.id <> %(org)s
 GROUP BY h.id, n.type;
"""

Q1_B = """
SELECT DISTINCT c.dst AS id, n.type
  FROM closure2 c
  JOIN node n ON n.id = c.dst
 WHERE c.src = %(org)s AND c.hops <= 2 AND c.dst <> %(org)s
   AND n.type IN ('person', 'org');
"""

# ---------- Q2：需求 -> 产业标签 -> 提供该能力的企业，按 weight 取前 20 ----------
Q2_A = """
SELECT hc.src AS org_id, max(hc.weight) AS weight
  FROM edge r
  JOIN edge hc ON hc.rel = 'has_capability' AND hc.dst = r.dst
  JOIN edge nt ON nt.rel = 'need_tag'       AND nt.src = r.src
  JOIN edge ot ON ot.rel = 'tagged'         AND ot.src = hc.src AND ot.dst = nt.dst
 WHERE r.rel = 'requires' AND r.src = %(need)s
 GROUP BY hc.src
 ORDER BY weight DESC
 LIMIT 20;
"""

Q2_B = """
SELECT org_id, weight
  FROM need_org
 WHERE need_id = %(need)s
 ORDER BY weight DESC
 LIMIT 20;
"""

# ---------- Q3：向量前 100 名 -> 按图关系过滤同产业、可见范围 org ----------
Q3_SQL = """
WITH cand AS MATERIALIZED (
    SELECT id, embedding <=> %(vec)s::vector AS dist
      FROM node
     WHERE type = 'org' AND embedding IS NOT NULL
     ORDER BY embedding <=> %(vec)s::vector
     LIMIT 100
)
SELECT c.id
  FROM cand c
  JOIN edge ot ON ot.rel = 'tagged' AND ot.src = c.id AND ot.dst = %(tag)s
  JOIN node n  ON n.id = c.id AND n.visibility = 'org'
 ORDER BY c.dist;
"""

# ---------- Q4：两企业间 ≤3 跳最短引荐路径 ----------
Q4_A = """
WITH RECURSIVE p (id, d, path) AS (
    SELECT e.dst, 1, ARRAY[e.src, e.dst]
      FROM edge e
     WHERE e.src = %(src)s AND e.rel = 'cooperates'
    UNION ALL
    SELECT e.dst, p.d + 1, p.path || e.dst
      FROM p
      JOIN edge e ON e.src = p.id
     WHERE p.d < 3 AND e.rel = 'cooperates' AND NOT (e.dst = ANY (p.path))
)
SELECT d AS hops, path
  FROM p
 WHERE id = %(dst)s
 ORDER BY d
 LIMIT 1;
"""

Q4_B = """
SELECT hops, path
  FROM org_reach3
 WHERE src = %(src)s AND dst = %(dst)s;
"""

QUERIES = {
    ("q1", "A"): Q1_A, ("q1", "B"): Q1_B,
    ("q2", "A"): Q2_A, ("q2", "B"): Q2_B,
    ("q3", "A"): Q3_SQL, ("q3", "B"): Q3_SQL,
    ("q4", "A"): Q4_A, ("q4", "B"): Q4_B,
}

LABELS = {
    "q1": "某企业两跳以内的联系人与企业",
    "q2": "需求→产业标签→能力企业（weight 排序取前 20）",
    "q3": "混合检索：向量前 100 名 → 同产业 + org 可见范围过滤",
    "q4": "两企业间 ≤3 跳最短引荐路径",
}


class Sampler:
    """固定种子的查询参数采样器：每类查询各采 RUNS 组参数，两方案共用同一批参数。"""

    def __init__(self, conn: psycopg.Connection, runs: int, seed: int = 7):
        self.runs = runs
        rnd = random.Random(seed)
        self.orgs = [r[0] for r in conn.execute(
            "SELECT id FROM node WHERE type = 'org' ORDER BY id").fetchall()]
        self.needs = [r[0] for r in conn.execute(
            "SELECT id FROM node WHERE type = 'need' ORDER BY id").fetchall()]

        # Q3：取某企业的向量与该企业的一个产业标签（避免自匹配无意义）
        self.q3_cases = []
        rows = conn.execute("""
            SELECT n.id, n.embedding::text, ot.dst
              FROM node n
              JOIN edge ot ON ot.rel = 'tagged' AND ot.src = n.id
             WHERE n.type = 'org' AND n.embedding IS NOT NULL
             LIMIT 3000
        """).fetchall()
        for _ in range(runs):
            nid, vec, tag = rows[rnd.randrange(len(rows))]
            self.q3_cases.append({"vec": vec, "tag": tag})

        # Q4：在内存里对 cooperates 子图做 BFS 采样（不依赖方案 B 的预计算表，
        # 保证两方案使用完全相同的参数；同时统计「纯随机企业对 3 跳可达率」）
        adj: dict[int, list[int]] = {}
        for s, d in conn.execute(
                "SELECT src, dst FROM edge WHERE rel = 'cooperates'").fetchall():
            adj.setdefault(s, []).append(d)

        def within3(src: int, dst: int) -> bool:
            frontier, seen = {src}, {src}
            for _ in range(3):
                nxt = set()
                for u in frontier:
                    for v in adj.get(u, ()):
                        if v == dst:
                            return True
                        if v not in seen:
                            seen.add(v)
                            nxt.add(v)
                frontier = nxt
                if not frontier:
                    return False
            return False

        def _cand(adj: dict[int, list[int]], src: int) -> list[int]:
            """src 出发 1..3 跳可达的企业集合。"""
            frontier, seen, out = {src}, {src}, []
            for _ in range(3):
                nxt = set()
                for u in frontier:
                    for v in adj.get(u, ()):
                        if v not in seen:
                            seen.add(v)
                            nxt.add(v)
                            out.append(v)
                frontier = nxt
                if not frontier:
                    break
            return out

        self.q4_cases = []
        seen_pairs: set[tuple[int, int]] = set()
        tries = 0
        while len(self.q4_cases) < runs and tries < runs * 200:
            tries += 1
            src = rnd.choice(self.orgs)
            cand = _cand(adj, src)
            if not cand:
                continue
            dst = rnd.choice(cand)
            if (src, dst) in seen_pairs:
                continue
            seen_pairs.add((src, dst))
            self.q4_cases.append({"src": src, "dst": dst})

        probe = 500
        hit = 0
        for _ in range(probe):
            s, d = rnd.choice(self.orgs), rnd.choice(self.orgs)
            hit += bool(s != d and within3(s, d))
        self.q4_hit_rate = round(hit / probe, 3)

        self.q1_cases = [{"org": rnd.choice(self.orgs)} for _ in range(runs)]
        self.q2_cases = [{"need": rnd.choice(self.needs)} for _ in range(runs)]

    def params(self, kind: str) -> list[dict]:
        return {"q1": self.q1_cases, "q2": self.q2_cases,
                "q3": self.q3_cases, "q4": self.q4_cases}[kind]
