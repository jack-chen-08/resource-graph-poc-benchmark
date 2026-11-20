"""虚构资源图谱数据生成（固定随机种子，可复现）。

节点 20,000 个 / 关系 100,000 条 / 企业与能力节点附 768 维虚构向量。
所有名称均为构造值，不含任何真实企业或个人信息。

节点类型（S6-PLAN-009 的类型定义本单未随附，此处按其字面口径落地，README 已注明对接点）：
  org 企业 / person 联系人 / capability 能力 / industry_tag 产业标签 / need 需求
关系类型：
  employs        person -> org
  knows          person -> person
  cooperates     org    -> org
  has_capability org    -> capability   (weight 0.0~1.0)
  cap_tag        capability -> industry_tag
  tagged         org    -> industry_tag
  requires       need   -> capability
  need_tag       need   -> industry_tag
"""
from __future__ import annotations

import os
import random
import time

import numpy as np
import psycopg

from .db import connect

DIM = 768
N_ORG, N_PERSON, N_CAP, N_TAG, N_NEED = 6000, 8000, 2000, 3000, 1000
N_NODES = N_ORG + N_PERSON + N_CAP + N_TAG + N_NEED          # 20,000
N_EMPLOYS, N_KNOWS, N_COOP = 8000, 13000, 20000
N_HASCAP, N_CAPTAG, N_TAGGED = 30000, 5000, 20000
N_REQUIRES, N_NEEDTAG = 3000, 1000
N_EDGES = (N_EMPLOYS + N_KNOWS + N_COOP + N_HASCAP
           + N_CAPTAG + N_TAGGED + N_REQUIRES + N_NEEDTAG)   # 100,000

ORG = (1, N_ORG)
PERSON = (N_ORG + 1, N_ORG + N_PERSON)
CAP = (PERSON[1] + 1, PERSON[1] + N_CAP)
TAG = (CAP[1] + 1, CAP[1] + N_TAG)
NEED = (TAG[1] + 1, TAG[1] + N_NEED)
assert NEED[1] == N_NODES, NEED


def _vec_text(rng: np.random.Generator) -> str:
    v = rng.normal(size=DIM).astype(np.float32)
    v /= np.linalg.norm(v) + 1e-12
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


def generate(seed: int = 42) -> dict:
    t0 = time.perf_counter()
    rnd = random.Random(seed)
    rng = np.random.default_rng(seed)

    conn = connect()
    conn.execute("SET synchronous_commit = off")

    # ---------- 节点 ----------
    names = {
        "org": [f"虚构企业{i:05d}" for i in range(1, N_ORG + 1)],
        "person": [f"虚构联系人{i:05d}" for i in range(1, N_PERSON + 1)],
        "capability": [f"虚构能力{i:05d}" for i in range(1, N_CAP + 1)],
        "industry_tag": [f"虚构产业标签{i:05d}" for i in range(1, N_TAG + 1)],
        "need": [f"虚构需求{i:05d}" for i in range(1, N_NEED + 1)],
    }
    with conn.cursor() as cur, cur.copy(
        "COPY node (id, type, name, org_id, site_id, visibility, embedding) FROM STDIN"
    ) as cp:
        for i, nid in enumerate(range(ORG[0], ORG[1] + 1)):
            cp.write_row((nid, "org", names["org"][i], nid, rnd.randint(1, 50), "org", _vec_text(rng)))
        for i, nid in enumerate(range(CAP[0], CAP[1] + 1)):
            cp.write_row((nid, "capability", names["capability"][i], None, None, "org", _vec_text(rng)))
        for i, nid in enumerate(range(PERSON[0], PERSON[1] + 1)):
            cp.write_row((nid, "person", names["person"][i], None, rnd.randint(1, 50), "org", None))
        for i, nid in enumerate(range(TAG[0], TAG[1] + 1)):
            cp.write_row((nid, "industry_tag", names["industry_tag"][i], None, None, "public", None))
        for i, nid in enumerate(range(NEED[0], NEED[1] + 1)):
            cp.write_row((nid, "need", names["need"][i], None, None, "org", None))
    conn.execute("SELECT setval('node_id_seq', %s)", (N_NODES,))

    # ---------- 关系 ----------
    edges: list[tuple[int, int, str, float]] = []

    # employs：企业 -> 联系人（方向从 org 出发，Q1「企业两跳内的联系人」才能成立）
    for pid in range(PERSON[0], PERSON[1] + 1):
        edges.append((rnd.randint(ORG[0], ORG[1]), pid, "employs", 1.0))

    # knows：联系人之间的社交边
    seen: set[tuple[int, int]] = set()
    while len(seen) < N_KNOWS:
        a = rnd.randint(PERSON[0], PERSON[1])
        b = rnd.randint(PERSON[0], PERSON[1])
        if a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        edges.append((a, b, "knows", 1.0))

    # cooperates：企业间合作关系
    seen = set()
    while len(seen) < N_COOP:
        a = rnd.randint(ORG[0], ORG[1])
        b = rnd.randint(ORG[0], ORG[1])
        if a == b or (a, b) in seen:
            continue
        seen.add((a, b))
        edges.append((a, b, "cooperates", round(rnd.uniform(0.1, 1.0), 3)))

    # has_capability：企业拥有的能力（带 weight，Q2 排序用）
    has_cap: list[tuple[int, int, float]] = []
    seen = set()
    while len(has_cap) < N_HASCAP:
        o = rnd.randint(ORG[0], ORG[1])
        c = rnd.randint(CAP[0], CAP[1])
        if (o, c) in seen:
            continue
        seen.add((o, c))
        w = round(rnd.uniform(0.1, 1.0), 3)
        has_cap.append((o, c, w))
        edges.append((o, c, "has_capability", w))

    # cap_tag：能力 -> 产业标签（先保证每个能力至少 1 个标签，再补到 5000 条）
    cap_tags: dict[int, list[int]] = {}
    for c in range(CAP[0], CAP[1] + 1):
        t = rnd.randint(TAG[0], TAG[1])
        cap_tags[c] = [t]
        edges.append((c, t, "cap_tag", 1.0))
    produced = len(cap_tags)
    while produced < N_CAPTAG:
        c = rnd.randint(CAP[0], CAP[1])
        t = rnd.randint(TAG[0], TAG[1])
        if t in cap_tags[c]:
            continue
        cap_tags[c].append(t)
        produced += 1
        edges.append((c, t, "cap_tag", 1.0))

    # tagged：企业 -> 产业标签（由其能力对应的标签派生，保证 Q2 过滤有意义）
    org_caps: dict[int, list[int]] = {}
    for o, c, _w in has_cap:
        org_caps.setdefault(o, []).append(c)
    produced = 0
    seen = set()
    while produced < N_TAGGED:
        o = rnd.choice(list(org_caps.keys()))
        caps = org_caps[o]
        cand_tags = [t for c in rnd.sample(caps, min(3, len(caps))) for t in cap_tags.get(c, [])]
        if not cand_tags:
            continue
        t = rnd.choice(cand_tags)
        if (o, t) in seen:
            continue
        seen.add((o, t))
        produced += 1
        edges.append((o, t, "tagged", 1.0))

    # requires：需求 -> 能力（每个需求恰好 3 条，保证 need_tag 恰好 1000 条）
    need_caps: dict[int, list[int]] = {}
    for n in range(NEED[0], NEED[1] + 1):
        caps: list[int] = []
        while len(caps) < 3:
            c = rnd.randint(CAP[0], CAP[1])
            if c not in caps:
                caps.append(c)
        need_caps[n] = caps
        for c in caps:
            edges.append((n, c, "requires", 1.0))

    # need_tag：需求 -> 产业标签（取某条所需能力的标签，保证 Q2 一定命中）
    for n in range(NEED[0], NEED[1] + 1):
        tags = [t for c in need_caps[n] for t in cap_tags.get(c, [])]
        edges.append((n, rnd.choice(tags), "need_tag", 1.0))

    assert len(edges) == N_EDGES, f"关系条数 {len(edges)} != {N_EDGES}"

    with conn.cursor() as cur, cur.copy("COPY edge (src, dst, rel, weight) FROM STDIN") as cp:
        for row in edges:
            cp.write_row(row)

    conn.execute("ANALYZE node")
    conn.execute("ANALYZE edge")
    stats = {}
    for label, sql in (
        ("nodes", "SELECT count(*) FROM node"),
        ("edges", "SELECT count(*) FROM edge"),
        ("vectors", "SELECT count(*) FROM node WHERE embedding IS NOT NULL"),
    ):
        stats[label] = conn.execute(sql).fetchone()[0]
    stats["seconds"] = round(time.perf_counter() - t0, 1)
    conn.close()
    print(f"[gen] 节点 {stats['nodes']} / 关系 {stats['edges']} / 向量 {stats['vectors']} "
          f"/ 用时 {stats['seconds']}s", flush=True)
    return stats
