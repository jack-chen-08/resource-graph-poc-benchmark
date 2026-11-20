"""Q4 补测：把「有路径」和「无路径」两种情况的代价分开测。

为什么补：主压测里 Q4 的参数是随机抽的企业对，命中率只有 0.6%——也就是说 99.4% 的样本
根本没有 ≤3 跳路径，查询立刻返回空。那个 0.5 ms 量级的 P95 主要反映「快速判空」，
而不是真实的最短路径查询代价。这里把两种情况分开测，给出各自的口径。
"""
import json
import os
import random
import statistics
import time

from src import queries
from src.db import connect


def pct(xs, p):
    xs = sorted(xs)
    if not xs:
        return 0.0
    k = (len(xs) - 1) * p / 100
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return round(xs[lo] + (xs[hi] - xs[lo]) * (k - lo), 3)


def timeit(conn, sql, params, runs):
    out = []
    rows = 0
    for _ in range(runs):
        t0 = time.perf_counter()
        with conn.cursor() as cur:
            cur.execute(sql, params)
            got = cur.fetchall()
        out.append((time.perf_counter() - t0) * 1000)
        rows = len(got)
    return out, rows


def main():
    rnd = random.Random(42)
    runs = 10
    n_pairs = 100
    with connect() as conn:
        conn.autocommit = True
        # 有路径的企业对（3 跳内可达）
        with conn.cursor() as cur:
            cur.execute("SELECT src, dst, hops FROM org_reach3 WHERE hops <= 3 LIMIT 5000")
            reach_all = cur.fetchall()
        reach = rnd.sample(reach_all, min(n_pairs, len(reach_all)))

        # 无路径的企业对（3 跳内不可达）
        with conn.cursor() as cur:
            cur.execute(
                "SELECT a.id, b.id FROM node a JOIN node b ON a.id < b.id "
                "WHERE a.type = 'org' AND b.type = 'org' AND NOT EXISTS ("
                "  SELECT 1 FROM org_reach3 r WHERE r.src = a.id AND r.dst = b.id"
                ") LIMIT 4000"
            )
            unreach_all = cur.fetchall()
        unreach = rnd.sample(unreach_all, min(n_pairs, len(unreach_all)))

        res = {"runs_per_pair": runs, "pairs": len(reach), "seed": 42}
        for label, pairs, hop_of in (("reachable", reach, 2), ("unreachable", unreach, None)):
            ta, tb, rows_a, rows_b = [], [], 0, 0
            hops = []
            for tup in pairs:
                s, t = tup[0], tup[1]
                if label == "reachable":
                    hops.append(tup[2])
                a, ra = timeit(conn, queries.Q4_A, {"src": s, "dst": t}, runs)
                b, rb = timeit(conn, queries.Q4_B, {"src": s, "dst": t}, runs)
                ta += a
                tb += b
                rows_a += ra
                rows_b += rb
            res[label] = {
                "plan_a_p50": pct(ta, 50), "plan_a_p95": pct(ta, 95),
                "plan_b_p50": pct(tb, 50), "plan_b_p95": pct(tb, 95),
                "rows_returned_avg": round(rows_a / max(1, len(pairs)), 2),
                "rows_B_avg": round(rows_b / max(1, len(pairs)), 2),
            }
            if hops:
                res[label]["hops_avg"] = round(statistics.mean(hops), 2)
                res[label]["hops_dist"] = {str(h): hops.count(h) for h in sorted(set(hops))}
        res["note"] = ("主压测的 Q4 命中率仅 0.6%，其 P95 主要反映判空；本节把可达/不可达分开测，"
                       "暴露真实路径查询代价")

    os.makedirs("reports", exist_ok=True)
    with open("reports/path_cost.json", "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("[path] 已写 reports/path_cost.json")


if __name__ == "__main__":
    main()
