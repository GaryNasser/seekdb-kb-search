# seekdb 知识库检索 · 参考文档

## 集合与表结构

用 seekdb SDK（`seekdb@1.4.0`，npm / PyPI 同名包）创建的集合，在数据库里对应一张物理表：

| 项 | 值 |
|---|---|
| 元数据表 | `sdk_collections`（`collection_id` CHAR(32)、`collection_name` STRING、UNIQUE INDEX `idx_name`） |
| 物理表名 | `` c$v2$<collection_id> `` |
| 主键 | `_id` VARBINARY(512) NOT NULL |
| 原文 | `document` STRING |
| 向量 | `embedding` VECTOR(512) |
| 元数据 | `metadata` JSON |
| 组织方式 | `ORGANIZATION HEAP` |

```sql
CREATE TABLE `c$v2$<collection_id>` (
  `_id`       VARBINARY(512) PRIMARY KEY NOT NULL,
  `document`  STRING,
  `embedding` VECTOR(512),
  `metadata`  JSON,
  FULLTEXT INDEX `idx_fts` (`document`)
    WITH PARSER ik PARSER_PROPERTIES=(ik_mode="smart"),
  VECTOR INDEX `idx_vec` (`embedding`)
    WITH (DISTANCE=COSINE, TYPE=HNSW, LIB=VSAG, M=16, EF_CONSTRUCTION=200, EF_SEARCH=64)
) ORGANIZATION HEAP;
```

## 索引参数

| 索引 | 参数 | 含义 |
|---|---|---|
| `idx_vec` | `DISTANCE=COSINE` | 余弦距离；**向量必须先 L2 归一化**，否则排序失真 |
| | `TYPE=HNSW, LIB=VSAG` | HNSW 近似最近邻，底层用 VSAG 实现 |
| | `M=16` | 每个节点的出边数量（召回 vs 内存的权衡） |
| | `EF_CONSTRUCTION=200` | 建索引时的候选集大小 |
| | `EF_SEARCH=64` | 查询时的候选集大小（越大越准越慢） |
| `idx_fts` | `PARSER ik` | 中文 ik 分词器 |
| | `ik_mode="smart"` | ik 的智能分词模式（另有 `max_word`） |

## 混合检索的 SQL 形态

```sql
SET @search_parm = '{
  "query": {"query_string": {"fields": ["document"], "query": "怎么创建向量索引"}},
  "knn":   {"field": "embedding", "k": 5, "query_vector": [ ... 512 个 float ... ]},
  "rank":  {"rrf": {"rank_window_size": 50, "rank_constant": 60}},
  "size":  5
}';

-- 数据库自己生成最终 SQL 并执行，融合与排序都在库内完成
SELECT DBMS_HYBRID_SEARCH.GET_SQL('c$v2$<collection_id>', @search_parm) FROM dual;
```

参数含义：

- `query.query_string.query`：全文查询串，字段由 `fields` 指定；
- `knn.field` / `knn.k` / `knn.query_vector`：向量检索的列、K 值与查询向量；
- `rank.rrf.rank_window_size`：RRF 的候选窗口大小（默认 50）；
- `rank.rrf.rank_constant`：RRF 的常数（默认 60），越小两路排名差异被放大得越厉害；
- `size`：最终返回条数。

纯全文检索（不走混合）时用：

```sql
SELECT _id, document, metadata FROM `c$v2$<collection_id>`
WHERE MATCH(document) AGAINST ('怎么创建向量索引' IN NATURAL LANGUAGE MODE)
LIMIT 5;
```

## 增量写入与索引刷新

seekdb 的向量索引与全文索引都是**增量构建**的，写入后不刷新是搜不到的：

```sql
CALL dbms_index_manager.refresh();
```

- 批量写入后统一刷新一次，比逐条刷新快得多；
- 脚本的 `--add` 已内置「先删同 id → 再插入 → 最后刷新」的幂等流程；
- 直接用 SDK 的 `add()` 是纯 INSERT，重复 id 会报主键冲突，所以**不要**绕过脚本直接写。

## 已知坑

| 现象 | 原因 | 处理 |
|---|---|---|
| `Duplicate entry 'xxx' for key 'idx_name'` | `DROP TABLE` 不会清掉 SDK 元数据表里的行 | 用 `client.deleteCollection(name)` 删集合，而不是 `DROP TABLE` |
| `Embedding function 'default-embed' is not registered` | 用了带默认 embedding 函数的 `getCollection` | 传 `embeddingFunction: null` |
| 全文检索时服务端崩溃 | seekdb 1.4.0 的 `space` 分词器在中文多字词上有缺陷（`compare_nullsafe` 断言失败） | 改用 `ik` 分词器；官方发行版与自编译版均复现 |
| `npm install seekdb` 报 `EUNSUPPORTEDPROTOCOL workspace:*` | peer 依赖使用了 workspace 协议 | `npm install --legacy-peer-deps` |
| `Failed to connect to …` 但其实是库不存在 | SDK 把 `ER_BAD_DB_ERROR` 包成了连接错误 | 先用 `SeekdbAdminClient` 建库再连 |

## 相关链接

- seekdb 项目与发行包：https://github.com/oceanbase/seekdb
- OceanBase 训练营「阶跃星辰」赛题（端侧推理 × seekdb 混合检索）：本仓库 `stepfun/` 目录
