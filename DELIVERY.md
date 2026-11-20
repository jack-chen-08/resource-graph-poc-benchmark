# 交付说明与验收对照

本仓库对应需求「资源图谱两种建模范式对比 POC」。一句话：**一条命令 `docker compose up --build`
就能从空库建出 20,000 节点 / 100,000 关系的虚构图谱，跑完方案 A 与方案 B 的四类目标查询，
并生成压测报告。** 全部数据由固定随机种子 42 生成，不含任何真实企业或个人信息。

## 验收项对照

| 验收项 | 交付物 | 在哪看 | 自检 |
| --- | --- | --- | --- |
| 建表、数据导入 | `sql/schema_plan_a.sql`（node / edge + B-tree 索引）、`src/gen_data.py`（固定种子造数） | `docker compose up --build` 日志前两段；`make psql` 后 `\dt` | `python -m src.selftest` 断言 node=20,000、edge=100,000（含各类型分项） |
| 四类目标查询可执行 | `src/queries.py`（Q1～Q4，两个方案各一套 SQL） | 报告第 1 节表格 | 自检按 id 集合比对两方案结果集相等 |
| 方案 A vs B 的 P50 / P95 对比 | `docs/BENCHMARK.md` 第 1 节；机器可读版 `reports/benchmark.json` | 同上 | 每类查询 200 组参数，预热 10 次不计入 |
| 性能目标判定 | 报告第 2 节 | Q1 P95 < 300 ms、Q4 P95 < 1 s，两方案均达标 | 自检从 benchmark.json 读值判定，非人工填写 |
| 结论与原因分析 | 报告第 2、5、6、8 节 | 含执行计划证据、构建成本、规模外推 | 第 6 节为 `EXPLAIN (ANALYZE, FORMAT JSON)` 实抓 |
| 测试方法可复核 | `docs/VERIFICATION.md`、`src/selftest.py` | 四条命令复核全部结论 | 自检 19 项，任一失败即非 0 退出 |

## 核心结论（三句话）

1. **两类查询的目标都过了，且余量极大**：Q1 两跳查询 P95 方案 A 3.6 ms / 方案 B 0.5 ms（目标 300 ms）；
   Q4 三跳最短路径 P95 A 0.6 ms / B 0.3 ms（目标 1 s）。
2. **方案 B 的价值不在「过线」而在「与数据量近乎无关」**：B 的 Q1/Q4 是一次闭包表 / 路径表查找，
   代价常数级；A 是递归逐跳展开，随度数与跳数增长。数据量涨 10 倍时，A 的 Q1 会跟着涨，B 不会。
3. **建了索引 ≠ 用上索引**：Q3 混合检索在 8,000 条向量这一档，优化器仍选精确扫描（两方案都 17 ms），
   而纯向量检索在同样规模下 HNSW 已快 6.8×。落地时必须把候选集物化或固定执行计划，
   否则 ANN 索引不会自动生效——这是本 POC 最值得带走的一条工程结论。

## 复现

```bash
docker compose up --build                                  # 约 6 分钟，出 report
docker compose run --rm poc python -m src.selftest         # 19 项自检（约 10 秒）
docker compose run --rm poc python -m src.report reports/benchmark.json   # 只重出报告
```

- 国内网络可在 `docker-compose.yml` 里把 `PIP_INDEX_URL` 指到镜像源（默认官方源）。
- Postgres 数据在卷 `pgdata` 里，`docker compose down -v` 可彻底重置。

## 环境与口径

- 8 核 / 15.6 GB / PostgreSQL 16 + pgvector 0.8.x / shared_buffers 512MB（报告首页有本次实测环境）。
- 计时为 `execute + fetchall`，不含连接建立；单连接串行，未做并发压测。
- 机器不同则绝对值不同，方案之间的倍数关系稳定；对照需同机重跑。

## 与原始设计文档的对接点

S6-PLAN-009（节点/关系类型定义）与 S6-PLAN-010（推荐方案）原文未随本单提供。当前实现按两份文档的
**字面口径**落地：

- 节点类型：企业 / 联系人 / 能力 / 需求 / 标签；关系：雇佣 / 供应 / 需求-能力 / 企业-标签 / 联系人-联系人。
- 若 009 的类型体系不同，只需改 `src/gen_data.py` 顶部的常量表与 `sql/schema_plan_a.sql` 的 CHECK 约束；
- 若 010 指定了具体引擎（Neo4j / Apache AGE 等），查询语义与压测脚本可直接替换，指标口径不变。

已按此在报告第 4 节、第 10 节注明，避免验收时把「未随附文档」当成实现缺失。
