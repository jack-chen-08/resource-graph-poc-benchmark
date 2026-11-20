"""规模外推实验：Q3 的「精确扫描 vs HNSW」交叉点在哪。

2 万节点（其中 8000 条向量）规模下，优化器算下来精确扫描更便宜，方案 B 的 HNSW 不会被选中。
这里用同一批真实向量复制成更大规模的测试表，单独测「纯向量检索 top-100」在不同规模下的耗时，
用来说明 HNSW 的收益从哪个量级开始出现——这是 Q3 在两方案下看不出差异的原因。
"""
from __future__ import annotations

import random
import time

import numpy as np
import psycopg

RUNS = 15
EF_SEARCH = 100


def _load(conn: psycopg.Connection, target: int) -> int:
    """把 vec_src 的向量复制到 vec_bench，凑到 target 行。"""
    conn.execute("DROP TABLE IF EXISTS vec_bench")
    conn.execute("CREATE TABLE vec_bench (id bigserial PRIMARY KEY, embedding vector(768))")
    conn.execute("INSERT INTO vec_bench (embedding) SELECT embedding FROM vec_src")
    cur = conn.execute("SELECT count(*) FROM vec_bench").fetchone()[0]
    while cur < target:
        take = min(cur, target - cur)
        conn.execute("INSERT INTO vec_bench (embedding) "
                     "SELECT embedding FROM vec_src LIMIT %s", (take,))
        cur = conn.execute("SELECT count(*) FROM vec_bench").fetchone()[0]
    return cur


def _vectors(conn: psycopg.Connection, n: int, seed: int = 11) -> list[str]:
    rnd = random.Random(seed)
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM vec_bench ORDER BY random() LIMIT %s", (n,)).fetchall()]
    return [r[0] for r in conn.execute(
        "SELECT embedding::text FROM vec_bench WHERE id = ANY(%s)", (ids,)).fetchall()]


def _time_queries(conn: psycopg.Connection, vectors: list[str]) -> tuple[float, float, list[set]]:
    sql = "SELECT id FROM vec_bench ORDER BY embedding <=> %s::vector LIMIT 100"
    times, sets = [], []
    for v in vectors:
        t = time.perf_counter()
        rows = conn.execute(sql, (v,)).fetchall()
        times.append((time.perf_counter() - t) * 1000)
        sets.append({r[0] for r in rows})
    a = np.array(times)
    return float(np.percentile(a, 50)), float(np.percentile(a, 95)), sets


def scale_curve(conn: psycopg.Connection, sizes=(8000, 24000, 72000)) -> tuple[list[dict], list[dict]]:
    conn.execute("DROP TABLE IF EXISTS vec_src CASCADE")
    conn.execute("CREATE TABLE vec_src AS SELECT embedding FROM node WHERE embedding IS NOT NULL")

    out: list[dict] = []
    ef_sweep: list[dict] = []
    for target in sizes:
        cur = _load(conn, target)
        vectors = _vectors(conn, RUNS)
        exact_p50, exact_p95, exact_sets = _time_queries(conn, vectors)

        t0 = time.perf_counter()
        # 建索引随规模线性增长，会撞上全局 60s statement_timeout，这一步单独放开
        conn.execute("SET statement_timeout = 0")
        conn.execute("SET maintenance_work_mem = '512MB'")   # 加速 HNSW 构建
        conn.execute("CREATE INDEX idx_vec_bench_hnsw ON vec_bench "
                     "USING hnsw (embedding vector_cosine_ops)")
        build_s = time.perf_counter() - t0
        conn.execute("SET statement_timeout = 60000")
        conn.execute("ANALYZE vec_bench")
        conn.execute(f"SET hnsw.ef_search = {EF_SEARCH}")
        conn.execute("SET enable_seqscan = off")      # 强制走 ANN，测其独立代价
        ann_p50, ann_p95, ann_sets = _time_queries(conn, vectors)
        base = sum(len(s) for s in exact_sets) or 1

        # ef_search 调参必须趁索引还在、且仍强制走 ANN 时做，否则测到的又是精确扫描
        if cur == max(sizes):
            for ef in (40, 100, 200):
                conn.execute(f"SET hnsw.ef_search = {ef}")
                p50, p95, sets_ef = _time_queries(conn, vectors)
                rec = sum(len(a & b) for a, b in zip(sets_ef, exact_sets)) / base
                ef_sweep.append({"ef_search": ef, "rows": cur,
                                 "p50_ms": round(p50, 1), "p95_ms": round(p95, 1),
                                 "recall": round(rec, 3), "runs": len(vectors)})
                print(f"[scale] ef_search={ef} @ {cur} 行：P50 {p50:.1f}ms 召回 {rec*100:.1f}%",
                      flush=True)
            conn.execute(f"SET hnsw.ef_search = {EF_SEARCH}")

        conn.execute("SET enable_seqscan = on")
        conn.execute("DROP INDEX idx_vec_bench_hnsw")
        recall = sum(len(a & b) for a, b in zip(ann_sets, exact_sets)) / base
        out.append({
            "rows": cur,
            "exact_p50_ms": round(exact_p50, 2), "exact_p95_ms": round(exact_p95, 2),
            "ann_p50_ms": round(ann_p50, 2), "ann_p95_ms": round(ann_p95, 2),
            "speedup": round(exact_p50 / max(ann_p50, 0.01), 1),
            "recall": round(recall, 3),
            "index_build_s": round(build_s, 1),
            "runs": len(vectors),
        })
        print(f"[scale] {cur:>6} 行：精确 P50 {out[-1]['exact_p50_ms']}ms / "
              f"HNSW P50 {out[-1]['ann_p50_ms']}ms（{out[-1]['speedup']}×）"
              f" 召回 {recall * 100:.1f}% 建索引 {build_s:.0f}s", flush=True)

    conn.execute("DROP TABLE IF EXISTS vec_bench, vec_src CASCADE")
    return out, ef_sweep
