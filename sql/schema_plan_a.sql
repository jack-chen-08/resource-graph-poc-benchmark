-- 资源图谱 POC · 基础结构
-- 全部数据为虚构测试数据（固定随机种子生成），不含任何真实企业或个人信息。

CREATE EXTENSION IF NOT EXISTS vector;

DROP TABLE IF EXISTS edge CASCADE;
DROP TABLE IF EXISTS node CASCADE;

CREATE TABLE node (
    id         BIGSERIAL PRIMARY KEY,
    type       TEXT    NOT NULL,           -- org | person | capability | industry_tag | need
    name       TEXT    NOT NULL,
    org_id     BIGINT,                     -- person / capability 所属企业（虚构）
    site_id    BIGINT,
    visibility TEXT    NOT NULL DEFAULT 'org',   -- org | public
    embedding  vector(768)                 -- 企业 / 能力节点附 768 维虚构向量
);

CREATE TABLE edge (
    id     BIGSERIAL PRIMARY KEY,
    src    BIGINT NOT NULL REFERENCES node(id),
    dst    BIGINT NOT NULL REFERENCES node(id),
    rel    TEXT   NOT NULL,                -- employs | cooperates | knows | has_capability
                                           -- tagged | cap_tag | requires | need_tag
    weight REAL   NOT NULL DEFAULT 1.0
);

-- 方案 A（关系表 + 递归 CTE）只用 B-tree 索引：不建任何向量 ANN 索引、不建预计算表。
CREATE INDEX idx_node_type        ON node (type);
CREATE INDEX idx_node_org         ON node (org_id);
CREATE INDEX idx_edge_src         ON edge (src);
CREATE INDEX idx_edge_dst         ON edge (dst);
CREATE INDEX idx_edge_src_rel     ON edge (src, rel);
CREATE INDEX idx_edge_dst_rel     ON edge (dst, rel);
CREATE INDEX idx_edge_rel_src     ON edge (rel, src);

ANALYZE node;
ANALYZE edge;
