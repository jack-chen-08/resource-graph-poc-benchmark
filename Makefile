.PHONY: up rerun test psql down reset clean

# 一条命令：建表 → 造数 → 方案 A 压测 → 构建方案 B → 方案 B 压测 → 出报告
up:
	docker compose up --build --abort-on-container-exit poc

# 重跑整套流程（表会重建、数据会重造，结果可复现）
rerun:
	docker compose run --rm poc

# 自检：数据规模 / 两方案结果等价性 / 目标阈值
test:
	docker compose run --rm poc python -m src.selftest

psql:
	docker compose exec db psql -U postgres -d graph_poc

down:
	docker compose down

reset:
	docker compose down -v

clean:
	rm -f reports/*.json docs/BENCHMARK.md
