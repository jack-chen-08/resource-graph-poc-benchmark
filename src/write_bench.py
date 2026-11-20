"""写侧代价：方案 B 的闭包表 / 路径表在「新增一条关系」时要付多少维护成本。

为什么单独测：查询侧方案 B 几乎完胜，但它的代价被推到了写入侧——每加一条边，
`closure2`（两跳闭包）与 `org_reach3`（≤3 跳路径）都要跟着更新。这正是评审会问的
「上线后关系高频变更怎么办」，而查询压测永远测不出来。

方法（诚实口径，先写清楚）：
- 每一轮挑一对**当前 2 跳内不可达**的 org 节点 (a, b)，插入一条 `cooperates` 边；
- 用「局部删 + 局部重算」维护两张预计算表（受影响的 src 集合 = a 及其 1 跳前驱），
  记录每一步耗时与改动的行数；
- 每轮结束 **ROLLBACK**，数据回到 20,000 / 100,000 原状，自检断言不受影响。

⚠️ 这是「局部全量重算」的代价，属于**上界**：真实实现可用增量算法（只插入新增可达对）
或触发器把成本压低。本脚本给的是保守值，落地时可以用它当"最坏情况"预算。

输出：reports/write_cost.json
运行：docker compose run --rm poc python -m src.write_bench
"""
from __future__ import annotations

import json
import random
import statistics
import time

from .db import connect

ROUNDS = 30
SEED = 42

# 受影响的 src 集合：a 本身 + 1 跳内可达 a 的节点（employs / cooperates）
AFFECTED_SRC = """
SELECT DISTINCT src FROM edge
 WHERE rel IN ('employs', 'cooperates') AND dst = %(a)s
UNION SELECT %(a)s
"""

CLOSURE_REBUILD = """
INSERT INTO closure2 (src, dst, hops)
WITH RECURSIVE step AS (
    SELECT e.src, e.dst, 1 AS hops
      FROM edge e
     WHERE e.rel IN ('employs', 'cooperates')
       AND e.src = ANY (%(srcs)s)
    UNION
    SELECT s.src, e.dst, s.hops + 1
      FROM step s
      JOIN edge e ON e.src = s.dst
     WHERE s.hops < 2
       AND e.rel IN ('employs', 'cooperates')
       AND e.dst <> s.src
)
SELECT src, dst, min(hops)::smallint
  FROM step
 GROUP BY src, dst;
"""

REACH3_REBUILD = """
INSERT INTO org_reach3 (src, dst, hops, path)
WITH RECURSIVE p AS (
    SELECT e.src, e.dst, 1 AS hops, ARRAY[e.src, e.dst] AS path
      FROM edge e
     WHERE e.rel = 'cooperates'
       AND e.src = ANY (%(srcs)s)
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


def pct(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[i], 3)


def main() -> None:
    rnd = random.Random(SEED)
    conn = connect()
    conn.autocommit = False
    cur = conn.cursor()

    orgs = [r[0] for r in cur.execute("SELECT id FROM node WHERE type = 'org'").fetchall()]
    print(f"[write] org 节点 {len(orgs):,}", flush=True)

    edge_ms, col_ms, reach_ms = [], [], []
    col_rows, reach_rows, total_ms, aff_sizes = [], [], [], []

    for i in range(ROUNDS):
        for _ in range(200):                       # 找一对当前不可达的点，保证真的改变闭包
            a, b = rnd.sample(orgs, 2)
            if a == b:
                continue
            skip = cur.execute(
                """SELECT 1 FROM edge WHERE src = %s AND dst = %s AND rel = 'cooperates'
                   UNION ALL
                   SELECT 1 FROM closure2 WHERE src = %s AND dst = %s""", (a, b, a, b)).fetchone()
            if not skip:
                break
        else:
            raise SystemExit("找不到可选的点对")

        srcs = [r[0] for r in cur.execute(AFFECTED_SRC, {"a": a}).fetchall()]
        aff_sizes.append(len(srcs))
        t_all = time.perf_counter()

        t = time.perf_counter()
        cur.execute("INSERT INTO edge (src, dst, rel, weight) VALUES (%s, %s, 'cooperates', 1.0)", (a, b))
        edge_ms.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        cur.execute("DELETE FROM closure2 WHERE src = ANY (%s)", (srcs,))
        deleted = cur.rowcount
        cur.execute(CLOSURE_REBUILD, {"srcs": srcs})
        col_rows.append(deleted + cur.rowcount)
        col_ms.append((time.perf_counter() - t) * 1000)

        t = time.perf_counter()
        cur.execute("DELETE FROM org_reach3 WHERE src = ANY (%s)", (srcs,))
        deleted3 = cur.rowcount
        cur.execute(REACH3_REBUILD, {"srcs": srcs})
        reach_rows.append(deleted3 + cur.rowcount)
        reach_ms.append((time.perf_counter() - t) * 1000)

        total_ms.append((time.perf_counter() - t_all) * 1000)
        conn.rollback()                            # 复原，保持 20k/100k 口径
        if (i + 1) % 10 == 0:
            print(f"[write] {i + 1}/{ROUNDS} 完成", flush=True)

    out = {
        "rounds": ROUNDS,
        "seed": SEED,
        "note": "局部删除 + 局部重算，属保守上界；每轮后 ROLLBACK，数据规模不变",
        "affected_src_avg": round(statistics.mean(aff_sizes), 2),
        "edge_insert": {"p50": pct(edge_ms, 0.50), "p95": pct(edge_ms, 0.95), "max": round(max(edge_ms), 3)},
        "closure2_refresh": {"p50": pct(col_ms, 0.50), "p95": pct(col_ms, 0.95),
                             "rows_p50": int(statistics.median(col_rows)),
                             "rows_max": max(col_rows)},
        "org_reach3_refresh": {"p50": pct(reach_ms, 0.50), "p95": pct(reach_ms, 0.95),
                               "rows_p50": int(statistics.median(reach_rows)),
                               "rows_max": max(reach_rows)},
        "total_per_edge": {"p50": pct(total_ms, 0.50), "p95": pct(total_ms, 0.95),
                           "max": round(max(total_ms), 3)},
    }
    with open("reports/write_cost.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2), flush=True)
    print("[write] 已写 reports/write_cost.json", flush=True)


if __name__ == "__main__":
    main()
