"""压测执行：4 类查询 × 2 方案，每类 200 次，记录 P50 / P95 与结果条数。"""
from __future__ import annotations

import json
import time

import numpy as np
import psycopg

from .queries import QUERIES, Sampler

WARMUP = 10


def summarize(times: list[float], rows: list[int]) -> dict:
    a = np.array(times)
    return {
        "runs": len(times),
        "p50_ms": round(float(np.percentile(a, 50)), 2),
        "p95_ms": round(float(np.percentile(a, 95)), 2),
        "avg_ms": round(float(a.mean()), 2),
        "min_ms": round(float(a.min()), 2),
        "max_ms": round(float(a.max()), 2),
        "rows_avg": round(float(np.mean(rows)), 1),
        "rows_p95": int(np.percentile(rows, 95)),
        "errors": 0,
    }


def _timed(conn, sql, params) -> tuple[float, list]:
    t = time.perf_counter()
    got = conn.execute(sql, params).fetchall()
    return (time.perf_counter() - t) * 1000, got


def run_plan(conn: psycopg.Connection, sampler: Sampler, plan: str) -> dict:
    conn.execute("SET statement_timeout = '60s'")
    if plan == "B":
        conn.execute("SET hnsw.ef_search = 100")

    out: dict[str, dict] = {}
    for kind in ("q1", "q2", "q3", "q4"):
        sql = QUERIES[(kind, plan)]
        cases = sampler.params(kind)

        for i in range(min(WARMUP, len(cases))):
            conn.execute(sql, cases[i]).fetchall()

        times, rows = [], []
        errors = 0
        for case in cases:
            try:
                dt, got = _timed(conn, sql, case)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                print(f"[bench:{plan}:{kind}] 查询失败：{exc}", flush=True)
                continue
            times.append(dt)
            rows.append(len(got))

        res = summarize(times, rows)
        res["errors"] = errors
        out[kind] = res
        print(f"[bench] 方案 {plan} {kind}: P50 {res['p50_ms']}ms "
              f"P95 {res['p95_ms']}ms 结果 {res['rows_avg']} 条", flush=True)
    return out


def q3_exact_sets(conn: psycopg.Connection, sampler: Sampler, runs: int = 100) -> list[set[int]]:
    """方案 A（精确扫描）下的 Q3 结果集，作为召回率基准。必须在建 HNSW 之前调用。"""
    sql = QUERIES[("q3", "A")]
    cases = sampler.params("q3")[:runs]
    return [{r[0] for r in conn.execute(sql, c).fetchall()} for c in cases]


def q3_sweep(conn: psycopg.Connection, sampler: Sampler, exact: list[set[int]],
             ef_values=(40, 100, 200)) -> list[dict]:
    """方案 B 的 Q3 在 ef_search 取不同值时的耗时与召回率（相对精确扫描）。"""
    sql = QUERIES[("q3", "B")]
    cases = sampler.params("q3")[:len(exact)]
    out = []
    for ef in ef_values:
        conn.execute(f"SET hnsw.ef_search = {ef}")
        conn.execute("SET enable_seqscan = off")   # 强制走 ANN，观察索引本身的代价与召回
        times, got_sets = [], []
        for case in cases:
            dt, got = _timed(conn, sql, case)
            times.append(dt)
            got_sets.append({r[0] for r in got})
        base_total = sum(len(b) for b in exact) or 1
        hit_total = sum(len(g & b) for g, b in zip(got_sets, exact))
        a = np.array(times)
        out.append({
            "ef_search": ef,
            "p50_ms": round(float(np.percentile(a, 50)), 2),
            "p95_ms": round(float(np.percentile(a, 95)), 2),
            "recall": round(hit_total / base_total, 4),
            "runs": len(times),
        })
        print(f"[sweep] q3 ef_search={ef}: P50 {out[-1]['p50_ms']}ms "
              f"P95 {out[-1]['p95_ms']}ms 召回 {out[-1]['recall']}", flush=True)
    conn.execute("SET hnsw.ef_search = 100")
    conn.execute("SET enable_seqscan = on")
    return out


def explain(conn: psycopg.Connection, kind: str, plan: str, params: dict) -> dict:
    """抓执行计划，作为「索引是否真的生效」的证据。"""
    sql = "EXPLAIN (ANALYZE, FORMAT JSON) " + QUERIES[(kind, plan)]
    raw = conn.execute(sql, params).fetchone()[0]
    plan_json = json.loads(raw) if isinstance(raw, str) else raw
    root = plan_json[0] if isinstance(plan_json, list) else plan_json

    scanned, indexes = [], []
    def walk(node):
        nt = node.get("Node Type")
        if nt:
            scanned.append(nt)
        idx = node.get("Index Name")
        if idx and idx not in indexes:
            indexes.append(idx)
        for child in node.get("Plans", []) or []:
            walk(child)
    walk(root["Plan"])
    return {"indexes": indexes, "node_types": sorted(set(scanned)),
            "exec_ms": round(float(root.get("Execution Time", 0.0)), 2)}
