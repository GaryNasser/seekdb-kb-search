---
name: seekdb-kb-search
description: 把 seekdb（OceanBase 向量数据库）里的知识库交给 AI 助手检索。支持纯全文检索（MATCH ... AGAINST）与「全文 + 向量」混合检索（RRF 融合在数据库内部完成），可写入文档、刷新索引、查看集合状态。当用户需要「查知识库」「在文档库里搜」「语义检索」「混合检索」，或需要在 OceanBase seekdb 上搭建 RAG 知识库时使用。
---

# seekdb 知识库检索

把 seekdb 当作 Agent 的知识库后端：**向量、原文、元数据都存在 seekdb 里，检索（召回 + RRF 融合 + 排序）也全部发生在数据库内部**，Agent 侧只负责传入查询、读取结果。

## 何时使用

- 需要在已有文档库中查资料（FAQ、技术文档、会议纪要、笔记）
- 需要语义检索而不是字面匹配（近义改写、口语化提问）
- 需要搭建一个最小可用的 RAG 知识库（写入 → 刷新索引 → 检索）

## 前置条件

1. 一个可访问的 seekdb 实例（MySQL 协议，默认端口 2881）。没有的话看下面的「快速起一个」。
2. Python 3.9+ 与依赖：`pip install pymysql`。
3. 集合已建好，结构见 `reference.md`（`_id` / `document` / `embedding` / `metadata` + HNSW 向量索引 + ik 全文索引）。
   **集合必须用 seekdb SDK 创建**（`reference.md` 有可直接复制的 Node.js 片段）——
   手工 `CREATE TABLE` 不会在 `sdk_collections` 里登记，脚本就解析不出集合名。

## 快速起一个 seekdb

```bash
# 官方发行包
dpkg -x seekdb_*.deb /opt/seekdb
/opt/seekdb/usr/bin/seekdb --base-dir /data/seekdb --port 2881 --nodaemon
```

## 命令行入口

脚本：`scripts/kb_search.py`（纯标准库 + pymysql，不依赖 SDK）。

```bash
# 1) 看集合状态（集合名、行数、维度、分词器）
python scripts/kb_search.py --collection knowledge_base --list

# 2) 纯全文检索
python scripts/kb_search.py --collection knowledge_base --query "怎么创建向量索引" --topk 5

# 3) 混合检索（全文 + 向量，RRF 在 seekdb 内融合）
python scripts/kb_search.py --collection knowledge_base \
    --query "怎么创建向量索引" --vector-file /tmp/q.json --topk 5

# 4) 写入文档（向量由调用方算好，512 维）
python scripts/kb_search.py --collection knowledge_base --add docs.json
```

连接参数用环境变量或命令行覆盖：

| 参数 | 环境变量 | 默认值 |
|---|---|---|
| `--host` | `SEEKDB_HOST` | `127.0.0.1` |
| `--port` | `SEEKDB_PORT` | `2881` |
| `--user` | `SEEKDB_USER` | `root` |
| `--password` | `SEEKDB_PASSWORD` | 空 |
| `--database` | `SEEKDB_DATABASE` | `stepfun_kb` |

`--add` 的输入是一个 JSON 数组，每项形如：

```json
[
  { "id": "d1", "document": "创建向量索引的 SQL …", "vector": [0.0123, -0.0456, "…512 个 float…"], "metadata": { "title": "向量索引" } }
]
```

写入是**幂等**的：同 `id` 的旧行会先被删除再写入，所以可以放心重复导入。

## 检索是怎么工作的（重要）

混合检索走的是 seekdb 内置的 `DBMS_HYBRID_SEARCH`：

```sql
SET @search_parm = '{"query":{"query_string":{"fields":["document"],"query":"..."}},
                     "knn":{"field":"embedding","k":5,"query_vector":[...]},
                     "rank":{"rrf":{"rank_window_size":50,"rank_constant":60}},
                     "size":5}';
SELECT DBMS_HYBRID_SEARCH.GET_SQL('c$v2$<collection_id>', @search_parm) FROM dual;
```

- `GET_SQL` 由数据库自己生成最终 SQL 并执行，**融合与排序完全在数据库内部**；
- 全文一路用 ik 分词器的 `MATCH(document) AGAINST (... IN NATURAL LANGUAGE MODE)`；
- 向量一路走 HNSW（cosine），因此写入前向量必须已 L2 归一化；
- 两路用 RRF（`rank_window_size=50`、`rank_constant=60`）融合；
- **不要**在客户端分别查两路再自己拼排名，那不是 seekdb 的混合检索。

## 工作流程

1. **确认集合**：`--list` 拿到 `collection_id`、行数、维度。集合名对应 SDK 元数据表 `sdk_collections`，
   真实表名是 `c$v2$<collection_id>`（脚本已自动解析）。
2. **确认是否已刷新索引**：写入后必须执行
   `CALL dbms_index_manager.refresh()`（脚本的 `--add` 会自动调用），否则新文档搜不到。
3. **构造查询**：
   - 只有原文 → 纯全文检索；
   - 有向量 → 混合检索。向量必须与建库时**同一个模型、同一精度、同一 pooling**，
     并做 L2 归一化（`normalize=true`），否则余弦距离排序会失真。
4. **读取结果**：`--json` 输出结构化 JSON（`_id`、`document`、`metadata`、RRF 分数），
   便于直接把原文喂给模型做回答。
5. **回答用户**：引用命中文档的标题与原文，不要编造库里没有的内容。

## 常见问题

- **搜不到刚写入的文档**：没刷新索引。执行 `--add`（自动刷新）或手动 `CALL dbms_index_manager.refresh()`。
- **集合不存在**：先建库建表，见 `reference.md`。
- **全文检索报服务端崩溃**：seekdb 1.4.0 的 `space` 分词器在中文多字词上有已知缺陷，
  改用 `ik` 分词器（`WITH PARSER ik PARSER_PROPERTIES=(ik_mode="smart")`）。
- **向量维度不匹配**：脚本会先校验维度，提示实际维度与期望维度。
- **写入报主键冲突**：不要绕过脚本直接 INSERT；脚本按 id 先删后写。

## 边界

- 脚本只负责「存取与检索」，**不负责算向量**：向量由调用方的 embedding 模型产生
  （浏览器端 ONNX、Python `sentence-transformers`、或任何兼容的 embedding 服务）。
- 检索结果只反映库里已有的内容；库里没有的内容不要编造。

<!-- OB-S:Q-3Edui-vQxj-1xJiAdw2g -->
