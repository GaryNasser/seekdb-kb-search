# seekdb-kb-search

把 **seekdb**（OceanBase 向量数据库）当作 Agent 的知识库后端：
向量、原文、元数据都存在 seekdb 里，**召回 + RRF 融合 + 排序全部发生在数据库内部**，
Agent 侧只负责传入查询、读取结果。

- 支持纯全文检索（`MATCH ... AGAINST`，ik 分词器）
- 支持「全文 + 向量」混合检索（`DBMS_HYBRID_SEARCH`，RRF 在库内融合）
- 支持按 id 幂等写入 + `dbms_index_manager.refresh()` 刷新索引
- 只依赖 `pymysql`，不需要任何外部 API Key

## 目录

```
.
├── SKILL.md              # Skill 说明（Anthropic Agent Skills 规范，frontmatter + 正文）
├── reference.md          # 表结构 / 索引参数 / 混合检索 SQL / 已知坑
└── scripts/
    └── kb_search.py      # 命令行工具：查看集合 / 检索 / 写入并刷新索引
```

## 快速开始

```bash
# 1) 起一个 seekdb
/opt/seekdb/usr/bin/seekdb --base-dir /data/seekdb --port 2881 --nodaemon

# 2) 依赖
pip install pymysql

# 3) 看集合状态
python scripts/kb_search.py --collection knowledge_base --list

# 4) 纯全文检索
python scripts/kb_search.py --collection knowledge_base --query "怎么创建向量索引" --topk 5

# 5) 混合检索（RRF 在 seekdb 内部融合）
python scripts/kb_search.py --collection knowledge_base \
    --query "怎么创建向量索引" --vector-file /tmp/q.json --topk 5

# 6) 写入文档（向量由调用方算好，512 维且已 L2 归一化）
python scripts/kb_search.py --collection knowledge_base --add docs.json
```

连接参数走环境变量或命令行：`SEEKDB_HOST` / `SEEKDB_PORT` / `SEEKDB_USER` / `SEEKDB_PASSWORD` / `SEEKDB_DATABASE`
（默认 `127.0.0.1:2881`、用户 `root`、空密码、库 `stepfun_kb`）。

## 混合检索的 SQL 形态

```sql
SET @search_parm = '{"query":{"query_string":{"fields":["document"],"query":"..."}},
                     "knn":{"field":"embedding","k":5,"query_vector":[...]},
                     "rank":{"rrf":{"rank_window_size":50,"rank_constant":60}},
                     "size":5}';
SELECT DBMS_HYBRID_SEARCH.GET_SQL('c$v2$<collection_id>', @search_parm) FROM dual;
```

最终 SQL 由数据库自己生成并执行 —— **融合与排序在数据库内部完成**，
不是在客户端分别查两路再自己拼排名。

## 说明

本仓库是 [OceanBase 2026 数据库大赛](https://open.oceanbase.com/train/detail/21) 生态赛题
「ModelScope · 发布 Skill」（600075）的作品源码，
用于在 [魔搭社区 Skills 中心](https://modelscope.cn/skills) 通过「从 Github 仓库快速创建」导入并发布。

验证行以 `SKILL.md` 末尾一行为准。
