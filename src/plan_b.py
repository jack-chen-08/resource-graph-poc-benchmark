"""方案 B（推荐方案的可实现等价物）：空间换时间。

S6-PLAN-010 的推荐方案原文本单未随附，这里按其「预计算 + 图投影」思路落地：
  1) closure2   两跳内可达闭包表（employers/cooperates 关系），代替实时递归 CTE
  2) org_reach3 企业间 ≤3 跳最短引荐路径（含路径），代替实时递归 CTE
  3) need_org   需求 -> 企业的预计算命中表（带 weight），Q2 直接索引取前 20
  4) 部分 HNSW 索引（仅 org 节点），向量检索走 ANN
建表耗时与磁盘占用一并记录，作为方案 B 的成本。
"""
from __future__ import annotations

import time

import psycopg

from .db import connect, table_size

CLOSURE2 = """
CREATE TABLE closure2 AS
WITH RECURSIVE step AS (
    SELECT e.src, e.dst, 1 AS hops
      FROM edge e
     WHERE e.rel IN ('employs', 'cooperates')
    UNION
    SELECT s.src, e.dst, s.hops + 1
      FROM step s
      JOIN edge e ON e.src = s.dst
     WHERE s.hops < 2
       AND e.rel IN ('employs', 'cooperates')
       AND e.dst <> s.src
)
SELECT src, dst, min(hops)::smallint AS hops
  FROM step
 GROUP BY src, dst;
"""

REACH3 = """
CREATE TABLE org_reach3 AS
WITH RECURSIVE p AS (
    SELECT e.src, e.dst, 1 AS hops, ARRAY[e.src, e.dst] AS path
      FROM edge e
     WHERE e.rel = 'cooperates'
    UNION ALL
    SELECT p.src, e.dst, p.hops + 1, p.path || e.dst
      FROM p
      JOIN edge e ON e.src = p.dst
     WHERE p.hops < 3
       AND e.rel = 'cooperates'
       AND NOT (e.dst = ANY (p.path))
)
SELECT DISTINCT ON (src, dst) src, dst, hops, path
  FROM p
 WHERE src <> dst
 ORDER BY src, dst, hops;
"""

NEED_ORG = """
CREATE TABLE need_org AS
SELECT r.src                    AS need_id,
       hc.src                   AS org_id,
       max(hc.weight)::real     AS weight
  FROM edge r
  JOIN edge hc ON hc.rel = 'has_capability' AND hc.dst = r.dst
  JOIN edge nt ON nt.rel = 'need_tag'       AND nt.src = r.src
  JOIN edge ot ON ot.rel = 'tagged'         AND ot.src = hc.src AND ot.dst = nt.dst
 WHERE r.rel = 'requires'
 GROUP BY r.src, hc.src;
"""

INDEXES = [
    "CREATE INDEX idx_closure2_src ON closure2 (src, hops)",
    "CREATE INDEX idx_reach3_src_dst ON org_reach3 (src, dst)",
    "CREATE INDEX idx_need_org ON need_org (need_id, weight DESC)",
    "CREATE INDEX idx_org_hnsw ON node USING hnsw (embedding vector_cosine_ops) "
    "WHERE type = 'org'",
]


def build(conn: psycopg.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or connect()
    t0 = time.perf_counter()

    # 幂等：重跑前清掉上一次的预计算产物
    conn.execute("DROP TABLE IF EXISTS closure2, org_reach3, need_org CASCADE")
    conn.execute("DROP INDEX IF EXISTS idx_org_hnsw")

    for name, ddl in (("closure2", CLOSURE2), ("org_reach3", REACH3), ("need_org", NEED_ORG)):
        t = time.perf_counter()
        conn.execute(ddl)
        conn.execute(f"ANALYZE {name}")
        print(f"[plan-b] {name} 建表 {time.perf_counter() - t:.1f}s", flush=True)

    for ddl in INDEXES:
        t = time.perf_counter()
        conn.execute(ddl)
        print(f"[plan-b] 索引 {ddl.split()[2]} {time.perf_counter() - t:.1f}s", flush=True)

    stats = {"seconds": round(time.perf_counter() - t0, 1)}
    for name in ("closure2", "org_reach3", "need_org", "node"):
        rows = conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        stats[name] = {"rows": rows, "size": table_size(conn, name)}
    print(f"[plan-b] 总构建 {stats['seconds']}s {stats}", flush=True)
    if own:
        conn.close()
    return stats
