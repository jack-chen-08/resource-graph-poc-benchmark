"""数据库连接与通用工具。"""
from __future__ import annotations

import os
import time

import psycopg


def conninfo() -> str:
    return (
        f"host={os.environ.get('PGHOST', 'localhost')} "
        f"port={os.environ.get('PGPORT', '55432')} "
        f"user={os.environ.get('PGUSER', 'postgres')} "
        f"password={os.environ.get('PGPASSWORD', 'postgres')} "
        f"dbname={os.environ.get('PGDATABASE', 'graph_poc')}"
    )


def connect(retries: int = 30, delay: float = 2.0) -> psycopg.Connection:
    """带重试的连接：容器刚起时 db 可能还没 ready。"""
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            conn = psycopg.connect(conninfo(), autocommit=True)
            return conn
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"[db] 连接失败（第 {attempt}/{retries} 次）：{exc}", flush=True)
            time.sleep(delay)
    raise RuntimeError(f"无法连接数据库：{last}")


def table_size(conn: psycopg.Connection, table: str) -> str:
    row = conn.execute("SELECT pg_size_pretty(pg_total_relation_size(%s))", (table,)).fetchone()
    return row[0] if row else "n/a"
