#!/usr/bin/env python3
"""
seekdb 知识库检索工具（ModelScope Skill: seekdb-kb-search）

- `--list`    查看集合状态（collection_id / 表名 / 行数 / 向量维度）
- `--query`   检索；带 `--vector-file` 时走「全文 + 向量」混合检索（RRF 融合在 seekdb 内部完成），
              不带时走纯全文检索（MATCH ... AGAINST）
- `--add`     写入文档（按 id 幂等：先删同 id 旧行再写）并刷新索引

连接参数：--host/--port/--user/--password/--database 或对应环境变量
          SEEKDB_HOST / SEEKDB_PORT / SEEKDB_USER / SEEKDB_PASSWORD / SEEKDB_DATABASE

依赖：pip install pymysql
"""

from __future__ import annotations

import argparse
import json
import os
import sys

try:
    import pymysql
    from pymysql.cursors import DictCursor
except ImportError:  # pragma: no cover
    print("缺少依赖：pip install pymysql", file=sys.stderr)
    raise SystemExit(2)

METADATA_TABLE = "sdk_collections"


# --------------------------------------------------------------------------- 连接


def connect(args):
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )


def resolve_collection(conn, collection: str) -> tuple[str, str]:
    """返回 (collection_id, 真实表名)。真实表名规则：c$v2$<collection_id>。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT collection_id FROM `{METADATA_TABLE}` WHERE collection_name = %s",
            (collection,),
        )
        row = cur.fetchone()
    if not row:
        raise SystemExit(
            f"集合 {collection!r} 不在 {METADATA_TABLE} 中。先用 seekdb SDK 的 createCollection 创建。"
        )
    cid = str(row["collection_id"])
    return cid, f"c$v2${cid}"


def table_info(conn, table: str) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s AND COLUMN_NAME = 'embedding'",
            (table,),
        )
        row = cur.fetchone()
    dim = None
    if row and row["COLUMN_TYPE"]:
        digits = "".join(ch for ch in str(row["COLUMN_TYPE"]) if ch.isdigit())
        dim = int(digits) if digits else None
    return {"table": table, "dim": dim}


def row_count(conn, table: str) -> int:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM `{table}`")
        row = cur.fetchone()
    return int(row["c"])


# --------------------------------------------------------------------------- 检索


def _decode(value):
    if isinstance(value, (bytes, bytearray)):
        return value.decode("utf-8", "ignore")
    return value


def _normalize_metadata(value):
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8", "ignore")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def fulltext_search(conn, table: str, query: str, topk: int) -> list[dict]:
    sql = (
        f"SELECT _id, document, metadata FROM `{table}` "
        "WHERE MATCH(document) AGAINST (%s IN NATURAL LANGUAGE MODE) LIMIT %s"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (query, topk))
        rows = cur.fetchall()
    return [
        {
            "id": _decode(r["_id"]),
            "document": _decode(r["document"]),
            "metadata": _normalize_metadata(r["metadata"]),
        }
        for r in rows
    ]


def hybrid_search(conn, table: str, query: str, vector: list[float], topk: int) -> list[dict]:
    """混合检索：全文 + 向量，RRF 融合与排序全部在 seekdb 内部完成。"""
    parm = {
        "query": {"query_string": {"fields": ["document"], "query": query}},
        "knn": {"field": "embedding", "k": topk, "query_vector": vector},
        "rank": {"rrf": {"rank_window_size": 50, "rank_constant": 60}},
        "size": topk,
    }
    with conn.cursor() as cur:
        cur.execute("SET @search_parm = %s", (json.dumps(parm, ensure_ascii=False),))
        cur.execute(
            f"SELECT DBMS_HYBRID_SEARCH.GET_SQL(%s, @search_parm) AS query_sql FROM dual",
            (table,),
        )
        row = cur.fetchone()
    if not row or not row["query_sql"]:
        return []
    query_sql = str(row["query_sql"]).strip().strip("'\"")
    with conn.cursor() as cur:
        cur.execute(query_sql)
        rows = cur.fetchall()
    return [_row_to_hit(r) for r in rows]


def _row_to_hit(r: dict) -> dict:
    hit = {}
    for key, value in r.items():
        name = str(key)
        if name in ("_id", "id"):
            hit["id"] = _decode(value)
        elif name == "document":
            hit["document"] = _decode(value)
        elif name == "metadata":
            hit["metadata"] = _normalize_metadata(value)
        else:
            hit[name] = value
    hit.setdefault("id", None)
    hit.setdefault("document", None)
    hit.setdefault("metadata", None)
    return hit


# --------------------------------------------------------------------------- 写入


def add_documents(conn, table: str, docs: list[dict]) -> int:
    """按 id 幂等写入：先删同 id 旧行，再插入新行，最后刷新索引。"""
    ids = [str(d["id"]) for d in docs]
    if not ids:
        return 0
    with conn.cursor() as cur:
        placeholders = ",".join(["%s"] * len(ids))
        cur.execute(f"DELETE FROM `{table}` WHERE _id IN ({placeholders})", ids)
        for d in docs:
            meta = d.get("metadata")
            meta_json = json.dumps(meta, ensure_ascii=False) if meta is not None else "{}"
            cur.execute(
                f"INSERT INTO `{table}` (_id, document, embedding, metadata) "
                "VALUES (%s, %s, %s, %s)",
                (
                    str(d["id"]),
                    d.get("document", ""),
                    json.dumps(d["vector"], ensure_ascii=False),
                    meta_json,
                ),
            )
        cur.execute("CALL dbms_index_manager.refresh()")
        cur.fetchall()
    return len(docs)


# --------------------------------------------------------------------------- CLI


def main() -> int:
    # Windows 控制台默认 GBK，中文结果会乱码；统一按 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    p = argparse.ArgumentParser(description="seekdb 知识库检索")
    p.add_argument("--host", default=os.environ.get("SEEKDB_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("SEEKDB_PORT", "2881")))
    p.add_argument("--user", default=os.environ.get("SEEKDB_USER", "root"))
    p.add_argument("--password", default=os.environ.get("SEEKDB_PASSWORD", ""))
    p.add_argument("--database", default=os.environ.get("SEEKDB_DATABASE", "stepfun_kb"))
    p.add_argument("--collection", required=True, help="集合名，例如 knowledge_base")
    p.add_argument("--list", action="store_true", help="查看集合状态")
    p.add_argument("--query", help="查询文本")
    p.add_argument("--vector-file", help="查询向量 JSON 文件（数组），提供后走混合检索")
    p.add_argument("--topk", type=int, default=5)
    p.add_argument("--add", help="待写入文档的 JSON 文件（数组）")
    p.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = p.parse_args()

    conn = connect(args)
    try:
        cid, table = resolve_collection(conn, args.collection)
        info = table_info(conn, table)

        if args.add:
            with open(args.add, "r", encoding="utf-8") as fh:
                docs = json.load(fh)
            if not isinstance(docs, list):
                raise SystemExit("--add 文件必须是一个 JSON 数组")
            if info["dim"]:
                for d in docs:
                    if len(d.get("vector", [])) != info["dim"]:
                        raise SystemExit(
                            f"文档 {d.get('id')!r} 的向量维度为 {len(d.get('vector', []))}，"
                            f"与集合维度 {info['dim']} 不一致"
                        )
            n = add_documents(conn, table, docs)
            result = {"action": "add", "collection": args.collection, "count": n}
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"已写入 {n} 篇文档到 {table}，索引已刷新。")
            return 0

        if args.list or not args.query:
            info_out = {
                "collection": args.collection,
                "collection_id": cid,
                "table": table,
                "dim": info["dim"],
                "rows": row_count(conn, table),
                "database": args.database,
                "endpoint": f"{args.host}:{args.port}",
            }
            if args.json:
                print(json.dumps(info_out, ensure_ascii=False, indent=2))
            else:
                for k, v in info_out.items():
                    print(f"{k}: {v}")
            return 0

        hits = []
        mode = "fulltext"
        if args.vector_file:
            with open(args.vector_file, "r", encoding="utf-8") as fh:
                vector = json.load(fh)
            if info["dim"] and len(vector) != info["dim"]:
                raise SystemExit(
                    f"查询向量维度 {len(vector)} 与集合维度 {info['dim']} 不一致"
                )
            hits = hybrid_search(conn, table, args.query, vector, args.topk)
            mode = "hybrid"
        else:
            hits = fulltext_search(conn, table, args.query, args.topk)

        if args.json:
            print(
                json.dumps(
                    {"mode": mode, "query": args.query, "count": len(hits), "hits": hits},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(f"[{mode}] 命中 {len(hits)} 条")
            for i, h in enumerate(hits, 1):
                print(f"\n#{i} {h.get('id')}  {json.dumps(h.get('metadata'), ensure_ascii=False)}")
                doc = h.get("document") or ""
                print("   " + doc.replace("\n", "\n   ")[:500])
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
