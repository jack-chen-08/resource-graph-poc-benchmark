# 资源图谱 POC：两种建模范式的实测对比

一个可一键跑起来的 Postgres 17 + pgvector 环境，用**同一批虚构数据**（20,000 节点 / 100,000 关系）
把资源图谱的两种建模范式跑一遍，给出四类目标查询的 P50 / P95 实测值，并说明哪种范式在什么规模下更划算。

- 方案 A（建模层）：单张关系表 `node` / `edge` + 递归 CTE，查询时实时展开邻接边。
- 方案 B（预计算层）：在一张窄关系表上叠加 `closure2`（两跳闭包）、`org_reach3`（三跳路径表）、
  `need_org`（需求到企业的物化可达对）以及仅覆盖 org 的部分 HNSW 索引，用空间换时间。

> **数据全部虚构**：由 `src/gen_data.py` 按固定随机种子生成，不含任何真实企业、联系人或项目信息。
> 节点类型、关系类型按需求文档 S6-PLAN-009 的字面口径落地；原文未随订单附带，若类型体系不同，
> `src/gen_data.py` 顶部的常量表是唯一改动点。

## 快速开始

```bash
docker compose up --build          # 建库 → 造数 → 跑两个方案 → 出报告
```

跑完看两份产物：

- `docs/BENCHMARK.md` —— 完整报告（目标达成、两方案对比、构建成本、执行计划证据、规模外推、已知局限）
- `reports/benchmark.json` —— 同样内容的机器可读版

其他命令：

```bash
docker compose run --rm poc python -m src.selftest   # 自检：数据规模 / 两方案结果等价 / 目标阈值
docker compose down -v                               # 连数据卷一起清掉，回到干净状态
make psql                                            # 进 psql 手动查
```

自检会打印 PASS/FAIL 清单，全过时退出码为 0；任何一项不过直接非 0 退出，可以挂到 CI 上。

## 目录

```
docker-compose.yml        Postgres 17 + pgvector 与一次性 POC 容器
Dockerfile                POC 运行环境（python:3.12-slim）
sql/schema_plan_a.sql     基础结构：node / edge 与索引（方案 A 的全部表）
src/gen_data.py           虚构数据生成（固定种子，规模可调）
src/queries.py            四类目标查询的 SQL + 参数采样器（两方案共用同一批参数）
src/plan_b.py             方案 B 的预计算与索引构建
src/bench.py              压测、执行计划取证、ANN 调参
src/scale_test.py         规模外推：精确扫描 vs HNSW 的交叉点
src/report.py             报告生成（Markdown + JSON）
src/selftest.py           自检
docs/BENCHMARK.md         实测报告
reports/benchmark.json    机器可读结果
```

## 四类目标查询

| 编号 | 查询 | 目标 |
| --- | --- | --- |
| Q1 | 某企业两跳内的联系人 + 合作企业 | P95 < 300 ms |
| Q2 | 某需求的两跳可承接企业（需求→能力→企业） | 与 Q1 同批复测 |
| Q3 | 向量相似 + 同产业标签的混合检索 | 与 Q1 同批复测 |
| Q4 | 两家企业之间的三跳最短引荐路径 | P95 < 1000 ms |

每组查询各取 200 组参数，两个方案跑**完全相同**的参数批次，预热 10 次不计入统计；
结果条数也一并对比，用来验证两个方案在语义上等价（不一致会直接在报告里标出来）。

## 环境与网络注意事项

- 镜像需要 `pgvector/pgvector:pg16` 与 `python:3.12-slim`。国内网络下 Docker Hub 常被墙，
  先给镜像源打好本地 tag 再用 compose 起，或自行配置 `registry-mirrors`。
- 依赖安装默认走官方 PyPI；国内可用
  `PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker compose build` 覆盖，
  无需改 Dockerfile。
- 压测全部在容器内单机、单连接串行执行，覆盖的是**查询本身的代价**，不含并发与网络开销；
  生产容量评估需另做并发压测（报告第 10 节列了这条局限）。

## 复现性

- 数据：`src/gen_data.py` 固定种子，同样的代码在任何机器上生成同样的 20,000 / 100,000 规模。
- 参数：采样器同样固定种子，两个方案共用。
- 报告里的每个数字都来自 `reports/benchmark.json`，重跑即可复现（机器性能不同则绝对值不同，
  但方案之间的倍数关系稳定）。
