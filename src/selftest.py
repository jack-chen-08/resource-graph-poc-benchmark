"""自检：数据规模、两方案结果等价性、目标阈值。在已有数据库上跑，秒级返回。

用法（容器已起来后）：
    docker compose run --rm poc python -m src.selftest
"""
from __future__ import annotations

import json
import pathlib
import sys

from .db import connect
from .queries import QUERIES, Sampler

TARGETS = {"q1": 300.0, "q4": 1000.0}   # P95 上限（ms）
SAMPLE_CASES = 20


def main() -> int:
    conn = connect()
    failures: list[str] = []
    checks = 0

    def check(ok: bool, msg: str) -> None:
        nonlocal checks
        checks += 1
        print(("  PASS  " if ok else "  FAIL  ") + msg, flush=True)
        if not ok:
            failures.append(msg)

    print("== 1. 数据规模 ==", flush=True)
    n_node = conn.execute("SELECT count(*) FROM node").fetchone()[0]
    n_edge = conn.execute("SELECT count(*) FROM edge").fetchone()[0]
    n_vec = conn.execute("SELECT count(*) FROM node WHERE embedding IS NOT NULL").fetchone()[0]
    check(n_node == 20000, f"节点 20,000（实际 {n_node}）")
    check(n_edge == 100000, f"关系 100,000（实际 {n_edge}）")
    check(n_vec == 8000, f"768 维向量 8,000 条（实际 {n_vec}）")

    print("== 2. 两方案结果等价性 ==", flush=True)
    sampler = Sampler(conn, SAMPLE_CASES)
    for kind in ("q1", "q2", "q3", "q4"):
        cases = sampler.params(kind)[:SAMPLE_CASES]
        same = True
        for case in cases:
            a = {r[0] for r in conn.execute(QUERIES[(kind, "A")], case).fetchall()}
            b = {r[0] for r in conn.execute(QUERIES[(kind, "B")], case).fetchall()}
            if a != b:
                same = False
                break
        check(same, f"{kind.upper()}：方案 A 与 B 返回同一结果集（{len(cases)} 组参数）")

    print("== 3. 目标阈值 ==", flush=True)
    report_path = pathlib.Path("reports/benchmark.json")
    if not report_path.exists():
        check(False, "reports/benchmark.json 不存在（先跑 make up）")
    else:
        data = json.loads(report_path.read_text())
        for plan in ("plan_a", "plan_b"):
            for kind, limit in TARGETS.items():
                p95 = data[plan][kind]["p95_ms"]
                check(p95 < limit, f"{plan} {kind.upper()} P95 {p95} ms < {limit:.0f} ms")
        for plan in ("plan_a", "plan_b"):
            for kind in ("q1", "q2", "q3", "q4"):
                check(data[plan][kind]["runs"] == 200, f"{plan} {kind.upper()} 跑满 200 次")

    print(f"\n自检结论：{checks - len(failures)}/{checks} 项通过", flush=True)
    if failures:
        print("失败项：", flush=True)
        for f in failures:
            print(" - " + f, flush=True)
        return 1
    print("全部通过", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
