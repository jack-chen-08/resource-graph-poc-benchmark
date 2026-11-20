"""一条命令跑完：建表 → 造数 → 方案 A 压测 → 建方案 B 预计算 → 方案 B 压测 → 出报告。"""
from __future__ import annotations

import os
import pathlib

from . import bench, db, gen_data, plan_b, report
from .queries import LABELS, Sampler

SEED = int(os.environ.get("SEED", "42"))
RUNS = int(os.environ.get("RUNS", "200"))
SAMPLE_SEED = 7

TARGETS = {"q1": 300.0, "q4": 1000.0}


def apply_schema(conn) -> None:
    sql = pathlib.Path("sql/schema_plan_a.sql").read_text(encoding="utf-8")
    conn.execute(sql)
    print("[schema] 已重建 node / edge（仅方案 A 的 B-tree 索引）", flush=True)


def main() -> dict:
    conn = db.connect()
    apply_schema(conn)

    gen = gen_data.generate(SEED)
    sampler = Sampler(conn, RUNS, seed=SAMPLE_SEED)

    print("[phase] 方案 A（关系表 + 递归 CTE）压测", flush=True)
    res_a = bench.run_plan(conn, sampler, "A")

    # 证据与基准都要在建 HNSW 之前抓：否则方案 A 的 SQL 会被优化器改走 ANN 索引
    evidence_a = {}
    for kind in ("q1", "q2", "q3", "q4"):
        evidence_a[kind] = bench.explain(conn, kind, "A", sampler.params(kind)[0])
    exact_q3 = bench.q3_exact_sets(conn, sampler, runs=100)

    print("[phase] 构建方案 B（预计算表 + 部分 HNSW 索引）", flush=True)
    build = plan_b.build(conn)

    print("[phase] 方案 B 压测", flush=True)
    conn.execute("ANALYZE node")
    res_b = bench.run_plan(conn, sampler, "B")

    evidence_b = {}
    for kind in ("q1", "q2", "q3", "q4"):
        evidence_b[kind] = bench.explain(conn, kind, "B", sampler.params(kind)[0])

    print("[phase] 规模外推：精确扫描 vs HNSW 的交叉点 + ef_search 调参", flush=True)
    from .scale_test import scale_curve
    scale, ef_sweep = scale_curve(conn)

    env = report.collect_env(conn)
    result = {
        "seed": SEED, "sample_seed": SAMPLE_SEED, "runs": RUNS, "warmup": bench.WARMUP,
        "q4_hit_rate": sampler.q4_hit_rate,
        "env": env, "gen": gen, "plan_b_build": build,
        "plan_a": res_a, "plan_b": res_b,
        "evidence": {"A": evidence_a, "B": evidence_b},
        "ef_sweep": ef_sweep,
        "scale": scale,
    }
    report.write_report(result, out_dir="docs")

    print("\n==== 结果（P50 / P95 ms）====", flush=True)
    for kind in ("q1", "q2", "q3", "q4"):
        a, b = res_a[kind], res_b[kind]
        print(f"{kind.upper()} {LABELS[kind][:26]:<28} A {a['p50_ms']:8.1f}/{a['p95_ms']:8.1f}"
              f"   B {b['p50_ms']:8.1f}/{b['p95_ms']:8.1f}   条数 {a['rows_avg']}/{b['rows_avg']}",
              flush=True)
    for kind, limit in TARGETS.items():
        a_ok = res_a[kind]["p95_ms"] < limit
        b_ok = res_b[kind]["p95_ms"] < limit
        print(f"目标 {kind.upper()} P95 < {limit:.0f}ms → A {'达标' if a_ok else '未达标'}"
              f" / B {'达标' if b_ok else '未达标'}", flush=True)
    conn.close()
    return result


if __name__ == "__main__":
    main()
