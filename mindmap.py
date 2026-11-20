"""思维导图：M1 结构式（不调模型）+ M2 概念式（调用 mindmap-builder skill）。

对外函数见 docs/模块接口约定.md：
  generate / list_maps / get_map / save_nodes / regenerate
所有错误使用 AppError 子类（llm.py），由 app.py 统一转成 {"error":{"code","message"}}。
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict

import config
import db
import llm
from llm import AppError, ModelOutputInvalid

MODE_STRUCTURE = "structure"
MODE_CONCEPT = "concept"
MODES = (MODE_STRUCTURE, MODE_CONCEPT)

_HEADING_SEP = " > "
_DEFAULT_TITLE = "资料结构导图"
_INSUFFICIENT = "当前资料不足以生成导图。"
_INVALID_FORMAT = "该格式没有可用的标题层级，不能生成结构导图"
_ALLOWED_MODEL_STATUS = {"ready", "partial", "insufficient_evidence", "blocked_scope"}
_MODE = {"llm": "llm", "rule": "rule"}
_MODEL_ERROR_CODES = {"model_output_invalid", "model_request_failed", "model_not_configured"}

# ---------------------------------------------------------------------------
# v3.3 导图编辑器：主题 / 形状 / 自由布局（文本 + 关系线）
# ---------------------------------------------------------------------------

THEMES = ("fresh", "ocean", "amber", "graphite", "midnight")
SHAPES = ("roundrect", "ellipse", "circle", "pill", "diamond")
DEFAULT_THEME = "fresh"
DEFAULT_SHAPE = "roundrect"
MAX_FREE_TEXTS = 30
MAX_FREE_TEXT_LEN = 200
MAX_FREE_EDGES = 100
MAX_EDGE_LABEL_LEN = 20
MAX_STROKES = 300
MAX_STROKE_POINTS = 1000
MAX_TOTAL_STROKE_POINTS = 30000
POS_LIMIT = 5000.0
_EMPTY_LAYOUT = {"texts": [], "edges": [], "strokes": []}


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _doc_ext(doc) -> str:
    ext = str(doc["ext"] or "").strip().lower()
    return ext if ext.startswith(".") else "." + ext


def _clean_text(text: str | None) -> str:
    return " ".join((text or "").split())


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > POS_LIMIT:
        return None
    return round(number, 2)


def _parse_layout(raw) -> dict:
    """把库里的 layout_json 读成 {texts, edges, strokes}；损坏/旧数据返回空结构（不抛错）。"""
    empty = {"texts": [], "edges": [], "strokes": []}
    if not raw:
        return empty
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return empty
    if not isinstance(data, dict):
        return empty
    raw_texts = data.get("texts") if isinstance(data.get("texts"), list) else []
    raw_edges = data.get("edges") if isinstance(data.get("edges"), list) else []
    raw_strokes = data.get("strokes") if isinstance(data.get("strokes"), list) else []
    texts: list[dict] = []
    for index, item in enumerate(raw_texts):
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        x = _finite_number(item.get("x"))
        y = _finite_number(item.get("y"))
        if not text or x is None or y is None:
            continue
        texts.append({
            "id": str(item.get("id") or f"t{index + 1}"),
            "text": text[:MAX_FREE_TEXT_LEN],
            "x": x,
            "y": y,
        })
    edges: list[dict] = []
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            continue
        from_key = str(item.get("from_key") or "").strip()
        to_key = str(item.get("to_key") or "").strip()
        if not from_key or not to_key or from_key == to_key:
            continue
        edges.append({
            "id": str(item.get("id") or f"e{index + 1}"),
            "from_key": from_key,
            "to_key": to_key,
            "label": str(item.get("label") or "").strip()[:MAX_EDGE_LABEL_LEN],
        })
    strokes: list[dict] = []
    total_points = 0
    for index, item in enumerate(raw_strokes):
        if not isinstance(item, dict):
            continue
        color = str(item.get("color") or "").strip()
        if not color or len(color) > 20:
            color = "#b06a12"
        width = _finite_number(item.get("width"))
        if width is None or width <= 0 or width > 8:
            width = 2
        raw_points = item.get("points")
        if not isinstance(raw_points, list):
            continue
        points: list[dict] = []
        for point in raw_points:
            if not isinstance(point, dict):
                continue
            x = _finite_number(point.get("x"))
            y = _finite_number(point.get("y"))
            if x is None or y is None:
                continue
            points.append({"x": x, "y": y})
            total_points += 1
            if total_points >= MAX_TOTAL_STROKE_POINTS:
                break
        if len(points) < 2:
            continue
        strokes.append({
            "id": str(item.get("id") or f"s{index + 1}"),
            "color": color,
            "width": round(width, 2),
            "points": points[:MAX_STROKE_POINTS],
        })
        if len(strokes) >= MAX_STROKES or total_points >= MAX_TOTAL_STROKE_POINTS:
            break
    return {
        "texts": texts[:MAX_FREE_TEXTS],
        "edges": edges[:MAX_FREE_EDGES],
        "strokes": strokes[:MAX_STROKES],
    }


def _validate_layout(layout, node_keys: set[str]) -> dict:
    """校验前端提交的自由布局；非法数据抛 invalid_request（不静默吞掉）。"""
    if not isinstance(layout, dict):
        raise AppError("invalid_request", "导图布局数据不合法。")
    raw_texts = layout.get("texts")
    raw_edges = layout.get("edges")
    raw_strokes = layout.get("strokes")
    if raw_texts is None:
        raw_texts = []
    if raw_edges is None:
        raw_edges = []
    if raw_strokes is None:
        raw_strokes = []
    if (not isinstance(raw_texts, list) or not isinstance(raw_edges, list)
            or not isinstance(raw_strokes, list)):
        raise AppError("invalid_request", "导图布局数据不合法。")
    if len(raw_texts) > MAX_FREE_TEXTS:
        raise AppError("invalid_request", f"文本数量已达上限（{MAX_FREE_TEXTS}）。")
    if len(raw_edges) > MAX_FREE_EDGES:
        raise AppError("invalid_request", f"关系线数量已达上限（{MAX_FREE_EDGES}）。")

    texts: list[dict] = []
    text_ids: set[str] = set()
    for index, item in enumerate(raw_texts):
        if not isinstance(item, dict):
            raise AppError("invalid_request", "导图布局数据不合法。")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        if len(text) > MAX_FREE_TEXT_LEN:
            raise AppError("invalid_request", "单条文本长度超过 200 字。")
        x = _finite_number(item.get("x"))
        y = _finite_number(item.get("y"))
        if x is None or y is None:
            raise AppError("invalid_request", "导图布局数据不合法。")
        text_id = str(item.get("id") or f"t{index + 1}")
        if text_id in text_ids:
            text_id = f"t{index + 1}"
        text_ids.add(text_id)
        texts.append({"id": text_id, "text": text, "x": x, "y": y})

    edges: list[dict] = []
    edge_ids: set[str] = set()
    pairs: set[tuple[str, str]] = set()
    for index, item in enumerate(raw_edges):
        if not isinstance(item, dict):
            raise AppError("invalid_request", "导图布局数据不合法。")
        from_key = str(item.get("from_key") or "").strip()
        to_key = str(item.get("to_key") or "").strip()
        if from_key not in node_keys or to_key not in node_keys:
            raise AppError("invalid_request", "关系线端点不存在。")
        if from_key == to_key:
            raise AppError("invalid_request", "不能连接到自身。")
        pair = (from_key, to_key)
        if pair in pairs:
            raise AppError("invalid_request", "这两个节点已有关系线。")
        pairs.add(pair)
        label = str(item.get("label") or "").strip()
        if len(label) > MAX_EDGE_LABEL_LEN:
            raise AppError("invalid_request", f"关系线标签不能超过 {MAX_EDGE_LABEL_LEN} 字。")
        edge_id = str(item.get("id") or f"e{index + 1}")
        if edge_id in edge_ids:
            edge_id = f"e{index + 1}"
        edge_ids.add(edge_id)
        edges.append({"id": edge_id, "from_key": from_key, "to_key": to_key, "label": label})

    strokes: list[dict] = []
    stroke_ids: set[str] = set()
    total_points = 0
    if len(raw_strokes) > MAX_STROKES:
        raise AppError("invalid_request", f"涂鸦数量已达上限（{MAX_STROKES}）。")
    for index, item in enumerate(raw_strokes):
        if not isinstance(item, dict):
            raise AppError("invalid_request", "导图布局数据不合法。")
        raw_points = item.get("points")
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            continue
        points: list[dict] = []
        for point in raw_points:
            if not isinstance(point, dict):
                raise AppError("invalid_request", "导图布局数据不合法。")
            x = _finite_number(point.get("x"))
            y = _finite_number(point.get("y"))
            if x is None or y is None:
                raise AppError("invalid_request", "导图布局数据不合法。")
            points.append({"x": x, "y": y})
            total_points += 1
            if total_points > MAX_TOTAL_STROKE_POINTS:
                raise AppError("invalid_request", "涂鸦数据过大，请清理后重试。")
        if len(points) > MAX_STROKE_POINTS:
            raise AppError("invalid_request", f"单条涂鸦点数不能超过 {MAX_STROKE_POINTS}。")
        color = str(item.get("color") or "").strip() or "#b06a12"
        if len(color) > 20:
            raise AppError("invalid_request", "涂鸦颜色不合法。")
        width = _finite_number(item.get("width"))
        if width is None or width <= 0 or width > 8:
            raise AppError("invalid_request", "涂鸦线条宽度不合法。")
        stroke_id = str(item.get("id") or f"s{index + 1}")
        if stroke_id in stroke_ids:
            stroke_id = f"s{index + 1}"
        stroke_ids.add(stroke_id)
        strokes.append({"id": stroke_id, "color": color, "width": round(width, 2), "points": points})
    return {"texts": texts, "edges": edges, "strokes": strokes}


def _clean_layout(layout: dict, node_keys: set[str]) -> dict:
    """重新生成时保留用户布局，但清理端点已不存在的自由连线。"""
    texts = [dict(item) for item in (layout.get("texts") or []) if isinstance(item, dict)]
    edges = [
        dict(item) for item in (layout.get("edges") or [])
        if isinstance(item, dict)
        and item.get("from_key") in node_keys
        and item.get("to_key") in node_keys
        and item.get("from_key") != item.get("to_key")
    ]
    strokes = [
        dict(item) for item in (layout.get("strokes") or [])
        if isinstance(item, dict)
        and isinstance(item.get("points"), list)
        and len(item.get("points") or []) >= 2
    ]
    return {
        "texts": texts[:MAX_FREE_TEXTS],
        "edges": edges[:MAX_FREE_EDGES],
        "strokes": strokes[:MAX_STROKES],
    }


def _node_shape(raw) -> str:
    shape = raw.strip() if isinstance(raw, str) else ""
    if shape not in SHAPES:
        raise AppError("invalid_request", "不支持的节点形状。")
    return shape


def _node_pos(raw, axis: str):
    if raw is None or raw == "":
        return None
    number = _finite_number(raw)
    if number is None:
        raise AppError("invalid_request", "节点位置不合法。")
    return number



def _require_zone(zone_id: str):
    row = db.q1("SELECT id, name FROM zones WHERE id = ?", (zone_id,))
    if row is None:
        raise AppError("zone_not_found", "知识区不存在。")
    return row


def _require_map(mindmap_id: str):
    row = db.q1("SELECT * FROM mindmaps WHERE id = ?", (mindmap_id,))
    if row is None:
        raise AppError("invalid_request", "导图不存在。")
    return row


def _selected_docs(zone_id: str, document_ids) -> list:
    if not isinstance(document_ids, list) or not document_ids:
        raise AppError("invalid_request", "请先选择资料。")
    ids: list[str] = []
    for raw in document_ids:
        if isinstance(raw, str) and raw.strip() and raw.strip() not in ids:
            ids.append(raw.strip())
    if not ids:
        raise AppError("invalid_request", "请先选择资料。")
    placeholders = ",".join("?" for _ in ids)
    rows = db.q(
        f"SELECT * FROM documents WHERE zone_id = ? AND id IN ({placeholders})",
        (zone_id, *ids),
    )
    by_id = {row["id"]: row for row in rows}
    missing = [doc_id for doc_id in ids if doc_id not in by_id]
    if missing:
        raise AppError("invalid_request", "所选资料不存在或不属于该知识区。")
    return [by_id[doc_id] for doc_id in ids]


def _chunks_of_docs(zone_id: str, document_ids: list[str]):
    if not document_ids:
        return []
    placeholders = ",".join("?" for _ in document_ids)
    return db.q(
        "SELECT id, document_id, ordinal, heading_path, page, text FROM chunks"
        f" WHERE zone_id = ? AND document_id IN ({placeholders}) ORDER BY document_id, ordinal",
        (zone_id, *document_ids),
    )


# ---------------------------------------------------------------------------
# 节点读写
# ---------------------------------------------------------------------------

def _insert_nodes(mindmap_id: str, nodes: list[dict], updated_at: str) -> None:
    seq = [
        (
            db.new_id(),
            mindmap_id,
            node["node_key"],
            node.get("parent_key"),
            node["title"],
            node.get("note") or "",
            node.get("source_document_id"),
            json.dumps(node.get("source_chunk_ids") or [], ensure_ascii=False),
            node.get("origin") or "ai",
            int(node.get("unverified") or 0),
            node.get("shape") or DEFAULT_SHAPE,
            node.get("pos_x") if node.get("pos_x") is None else _finite_number(node.get("pos_x")),
            node.get("pos_y") if node.get("pos_y") is None else _finite_number(node.get("pos_y")),
            updated_at,
        )
        for node in nodes
    ]
    db.xmany(
        "INSERT INTO mindmap_nodes (id, mindmap_id, node_key, parent_key, title, note,"
        " source_document_id, source_chunk_ids, origin, unverified, shape, pos_x, pos_y, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        seq,
    )


def _node_rows(mindmap_id: str):
    return db.q(
        "SELECT rowid AS _rowid, id, mindmap_id, node_key, parent_key, title, note,"
        " source_document_id, source_chunk_ids, origin, unverified, shape, pos_x, pos_y, updated_at"
        " FROM mindmap_nodes WHERE mindmap_id = ? ORDER BY rowid",
        (mindmap_id,),
    )


def _row_to_node(row) -> dict:
    try:
        chunk_ids = json.loads(row["source_chunk_ids"] or "[]")
    except (TypeError, ValueError):
        chunk_ids = []
    if not isinstance(chunk_ids, list):
        chunk_ids = []
    return {
        "id": row["id"],
        "node_key": row["node_key"],
        "parent_key": row["parent_key"],
        "title": row["title"],
        "note": row["note"] or "",
        "source_document_id": row["source_document_id"],
        "source_chunk_ids": [str(cid) for cid in chunk_ids],
        "origin": row["origin"],
        "unverified": int(row["unverified"] or 0),
        "shape": row["shape"] or DEFAULT_SHAPE,
        "pos_x": float(row["pos_x"]) if row["pos_x"] is not None else None,
        "pos_y": float(row["pos_y"]) if row["pos_y"] is not None else None,
    }


def _order_rows(rows) -> list:
    """按树结构做广度优先排序；根缺失/孤儿节点兜底追加，保证前端拿到稳定顺序。"""
    if not rows:
        return []
    by_key = {row["node_key"]: row for row in rows}
    children: dict[str | None, list] = {}
    for row in rows:
        children.setdefault(row["parent_key"], []).append(row)
    ordered = []
    seen: set[str] = set()
    queue = list(children.get(None, []))
    if "root" in by_key and by_key["root"] not in queue:
        queue.insert(0, by_key["root"])
    while queue:
        row = queue.pop(0)
        key = row["node_key"]
        if key in seen:
            continue
        seen.add(key)
        ordered.append(row)
        queue.extend(children.get(key, []))
    for row in rows:
        if row["node_key"] not in seen:
            ordered.append(row)
            seen.add(row["node_key"])
    return ordered


def _rows_to_nodes(rows) -> list[dict]:
    return [_row_to_node(row) for row in _order_rows(rows)]


# ---------------------------------------------------------------------------
# M1 结构式
# ---------------------------------------------------------------------------

def _split_heading_path(heading_path: str | None) -> list[str]:
    path = _clean_text(heading_path)
    if not path:
        return []
    if _HEADING_SEP in path:
        return [part.strip() for part in path.split(_HEADING_SEP) if part.strip()]
    # 兼容旧数据：若没有 rag 约定的 " > " 分隔符，再尝试 "/"
    if "/" in path:
        return [part.strip() for part in path.split("/") if part.strip()]
    return [path]


def _generate_structure(zone_id: str, docs: list, title: str) -> tuple[list[dict], str, bool]:
    for doc in docs:
        if _doc_ext(doc) not in config.OUTLINE_DOC_EXTS:
            raise AppError("invalid_request", _INVALID_FORMAT)
    chunks = _chunks_of_docs(zone_id, [doc["id"] for doc in docs])
    if not chunks:
        raise AppError("insufficient_evidence", _INSUFFICIENT)

    nodes: list[dict] = [{
        "node_key": "root",
        "parent_key": None,
        "title": title,
        "note": "",
        "source_document_id": None,
        "source_chunk_ids": [],
        "origin": "ai",
        "unverified": 0,
    }]

    for doc in docs:
        doc_chunks = [c for c in chunks if c["document_id"] == doc["id"]]
        if not doc_chunks:
            continue
        doc_key = _sha12(f"{zone_id}|{doc['id']}|")
        loose_ids = [c["id"] for c in doc_chunks if not _clean_text(c["heading_path"])]
        nodes.append({
            "node_key": doc_key,
            "parent_key": "root",
            "title": str(doc["filename"]),
            "note": "",
            "source_document_id": doc["id"],
            "source_chunk_ids": loose_ids,
            "origin": "ai",
            "unverified": 0,
        })

        path_chunks: "OrderedDict[tuple[str, ...], list]" = OrderedDict()
        for chunk in doc_chunks:
            parts = tuple(_split_heading_path(chunk["heading_path"]))
            if not parts:
                continue
            for depth in range(1, len(parts) + 1):
                prefix = parts[:depth]
                if prefix not in path_chunks:
                    path_chunks[prefix] = []
            path_chunks[parts].append(chunk)

        for prefix, items in path_chunks.items():
            node_key = _sha12(f"{zone_id}|{doc['id']}|" + _HEADING_SEP.join(prefix))
            parent_key = doc_key if len(prefix) == 1 else _sha12(
                f"{zone_id}|{doc['id']}|" + _HEADING_SEP.join(prefix[:-1])
            )
            first = items[0] if items else None
            nodes.append({
                "node_key": node_key,
                "parent_key": parent_key,
                "title": prefix[-1],
                "note": _clean_text(first["text"])[:60] if first is not None else "",
                "source_document_id": doc["id"],
                "source_chunk_ids": [chunk["id"] for chunk in items],
                "origin": "ai",
                "unverified": 0,
            })
    return nodes, "ready", False


# ---------------------------------------------------------------------------
# M2 概念式
# ---------------------------------------------------------------------------

def _build_m2_evidence(zone_id: str, docs: list):
    import rag  # 延迟导入：M1 不依赖 rag.py

    doc_ids = [doc["id"] for doc in docs]
    by_document: dict[str, list] = {doc_id: [] for doc_id in doc_ids}
    for chunk in _chunks_of_docs(zone_id, doc_ids):
        by_document[chunk["document_id"]].append(chunk)

    evidence = []
    coverage_gaps = []
    for doc in docs:
        chunks = by_document[doc["id"]]
        if not chunks:
            coverage_gaps.append(f'{doc["filename"]}：没有已解析片段')
        elif doc["warning"] == rag.WARNING_OCR_PAGE_LIMIT:
            coverage_gaps.append(f'{doc["filename"]}：{doc["warning"]}')
        for chunk in chunks:
            evidence.append({
                "evidence_id": f"e{len(evidence) + 1}",
                "document_id": doc["id"],
                "document_version": doc["sha256"],
                "chunk_id": chunk["id"],
                "ordinal": chunk["ordinal"],
                "locator": {"page": chunk["page"], "section": chunk["heading_path"] or ""},
                "text": chunk["text"],
                "parse_status": "ready",
            })
    if not evidence:
        raise AppError("insufficient_evidence", _INSUFFICIENT)

    pack = {
        "scope": {
            "workspace_id": "local",
            "knowledge_base_id": zone_id,
            "conversation_id": "",
            "corpus_revision": rag.corpus_revision(zone_id),
        },
        "documents": [{"document_id": doc["id"], "filename": doc["filename"]} for doc in docs],
        "evidence": evidence,
        "coverage_gaps": coverage_gaps,
        "max_nodes": config.MINDMAP_MAX_NODES,
        "max_depth": config.MINDMAP_MAX_DEPTH,
    }
    return pack, {entry["evidence_id"]: entry for entry in evidence}


def _m2_batches(entries: list[dict]) -> list[list[dict]]:
    batches: list[list[dict]] = []
    batch: list[dict] = []
    size = 0
    for entry in entries:
        chars = len(entry["text"]) + len(entry.get("title", ""))
        if batch and (len(batch) >= config.MINDMAP_EVIDENCE_MAX_CHUNKS
                      or size + chars > config.MINDMAP_EVIDENCE_MAX_CHARS):
            batches.append(batch)
            batch, size = [], 0
        batch.append(entry)
        size += chars
    if batch:
        batches.append(batch)
    return batches


def _m2_refs(entries: list[dict]):
    for entry in entries:
        if "evidence_id" in entry:
            yield entry["evidence_id"]
        else:
            yield from entry["refs"]


def _summarize_m2_batch(pack: dict, batch: list[dict], title: str, mapping: dict[str, dict]) -> list[dict]:
    limit = max(1, min(6, len(batch) // 2))
    summary_pack = {**pack, "phase": "summarize", "evidence": batch, "max_sections": limit}
    obj, _usage = llm.chat_json(
        "mindmap", f"导图标题：{title}\n处理本批资料，返回 sections 和各段原始 refs。",
        kind="mindmap", system_extra=json.dumps(summary_pack, ensure_ascii=False),
        max_tokens=config.MINDMAP_MAX_TOKENS, temperature=config.MINDMAP_TEMPERATURE,
    )
    sections = obj.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= limit:
        raise ModelOutputInvalid("model_output_invalid", "资料分段提炼结果为空或超出分段上限。")

    allowed = set(_m2_refs(batch))
    summaries = []
    for section in sections:
        if not isinstance(section, dict):
            raise ModelOutputInvalid("model_output_invalid", "资料分段提炼缺少段落对象。")
        heading, text, refs = section.get("title"), section.get("text"), section.get("refs")
        if not isinstance(heading, str) or not heading.strip() or not isinstance(text, str) or not text.strip():
            raise ModelOutputInvalid("model_output_invalid", "资料分段提炼缺少标题或正文。")
        if not isinstance(refs, list) or not refs or any(not isinstance(ref, str) or ref not in allowed for ref in refs):
            raise ModelOutputInvalid("model_output_invalid", "资料分段提炼包含无效的原文引用。")
        refs = sorted(set(refs), key=lambda ref: int(ref[1:]))
        source = mapping[refs[0]]
        summaries.append({
            "title": _clean_text(heading)[:60],
            "text": text.strip()[:360],
            "refs": refs,
            "source_documents": list(dict.fromkeys(mapping[ref]["document_id"] for ref in refs)),
            "document_id": source["document_id"],
            "ordinal": source["ordinal"],
            "locator": source["locator"],
        })
    return sorted(summaries, key=lambda section: int(section["refs"][0][1:]))


def _validate_model_tree(raw_nodes):
    """校验模型 nodes 的 id/parent_id/根/环；返回 (by_id, root_id, children, depth, bfs_order)。"""
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ModelOutputInvalid("model_output_invalid", "模型输出 nodes 不是非空数组。")
    by_id: dict[str, dict] = {}
    order: list[str] = []
    for index, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            raise ModelOutputInvalid("model_output_invalid", f"nodes[{index}] 不是对象。")
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id.strip():
            raise ModelOutputInvalid("model_output_invalid", f"nodes[{index}] 缺少字符串 id。")
        node_id = node_id.strip()
        if node_id in by_id:
            raise ModelOutputInvalid("model_output_invalid", f"节点 id 重复：{node_id}。")
        title = node.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ModelOutputInvalid("model_output_invalid", f"节点 {node_id} 缺少 title。")
        parent_id = node.get("parent_id")
        if parent_id is not None and not isinstance(parent_id, str):
            raise ModelOutputInvalid("model_output_invalid", f"节点 {node_id} 的 parent_id 非法。")
        parent_id = parent_id.strip() if isinstance(parent_id, str) and parent_id.strip() else None
        normalized = dict(node)
        normalized["id"] = node_id
        normalized["parent_id"] = parent_id
        normalized["title"] = title.strip()
        by_id[node_id] = normalized
        order.append(node_id)

    roots = [node_id for node_id in order if by_id[node_id]["parent_id"] is None]
    if len(roots) != 1:
        raise ModelOutputInvalid("model_output_invalid", f"模型输出必须恰有 1 个根节点，实际 {len(roots)} 个。")
    root_id = roots[0]

    children: dict[str, list[str]] = {node_id: [] for node_id in order}
    for node_id in order:
        parent_id = by_id[node_id]["parent_id"]
        if parent_id is None:
            continue
        if parent_id not in by_id:
            raise ModelOutputInvalid("model_output_invalid", f"节点 {node_id} 的 parent_id 不存在：{parent_id}。")
        children[parent_id].append(node_id)

    depth: dict[str, int] = {root_id: 1}
    bfs_order: list[str] = []
    queue = [root_id]
    while queue:
        node_id = queue.pop(0)
        bfs_order.append(node_id)
        for child_id in children[node_id]:
            depth[child_id] = depth[node_id] + 1
            queue.append(child_id)
    if len(bfs_order) != len(order):
        raise ModelOutputInvalid("model_output_invalid", "模型输出节点存在环或不可从根到达的节点。")
    return by_id, root_id, children, depth, bfs_order


def _parent_title_path(node_id: str, by_id: dict, titles: dict[str, str]) -> str:
    parts: list[str] = []
    current = by_id[node_id].get("parent_id")
    guard = 0
    while current is not None and guard <= len(by_id):
        parts.append(titles[current])
        current = by_id[current].get("parent_id")
        guard += 1
    parts.reverse()
    return "/".join(parts)


def _parse_concept_output(obj: dict, zone_id: str, mapping: dict[str, dict]):
    if not isinstance(obj, dict):
        raise ModelOutputInvalid("model_output_invalid", "模型输出不是 JSON 对象。")
    model_status = obj.get("status")
    if model_status not in _ALLOWED_MODEL_STATUS:
        raise ModelOutputInvalid("model_output_invalid", f"模型输出 status 非法：{model_status!r}。")
    if model_status in ("insufficient_evidence", "blocked_scope"):
        raise AppError("insufficient_evidence", _INSUFFICIENT)

    by_id, root_id, _children, depth, bfs_order = _validate_model_tree(obj.get("nodes"))

    # 广度优先截断：节点 ≤ MINDMAP_MAX_NODES、深度 ≤ MINDMAP_MAX_DEPTH
    kept: list[str] = []
    truncated = False
    for node_id in bfs_order:
        if depth[node_id] > config.MINDMAP_MAX_DEPTH:
            truncated = True
            break
        if len(kept) >= config.MINDMAP_MAX_NODES:
            truncated = True
            break
        kept.append(node_id)
    kept_set = set(kept)

    titles = {node_id: by_id[node_id]["title"] for node_id in kept}
    key_map: dict[str, str] = {}
    nodes: list[dict] = []
    for node_id in kept:
        node = by_id[node_id]
        title = titles[node_id]
        if node_id == root_id:
            node_key = "root"
            parent_key = None
        else:
            parent_id = node["parent_id"]
            if parent_id is None or parent_id not in kept_set:
                raise ModelOutputInvalid("model_output_invalid", "截断后子节点父节点缺失。")
            parent_title_path = _parent_title_path(node_id, by_id, titles)
            node_key = _sha12(f"m2|{zone_id}|{parent_title_path}|{title}")
            parent_key = key_map[parent_id]
        if node_key != "root" and node_key in key_map.values():
            raise ModelOutputInvalid("model_output_invalid", f"节点标识重复（父路径与标题相同）：{title}。")
        key_map[node_id] = node_key

        refs = node.get("refs") or []
        if not isinstance(refs, list):
            refs = []
        chunk_ids: list[str] = []
        first_doc_id = None
        invalid_ref = False
        for ref in refs:
            if not isinstance(ref, str) or ref not in mapping:
                invalid_ref = True
                continue
            item = mapping[ref]
            chunk_id = item["chunk_id"]
            if chunk_id not in chunk_ids:
                chunk_ids.append(chunk_id)
                if first_doc_id is None:
                    first_doc_id = item["document_id"]
        if invalid_ref:
            # 规格 10.3：非法 ref 的节点 unverified=1 且 source_chunk_ids=[]
            unverified = 1
            chunk_ids = []
            first_doc_id = None
        else:
            # 规格 8.4/10.4：非根节点不得空 source_chunk_ids 却标 unverified=0
            unverified = 0 if (node_id == root_id or chunk_ids) else 1
        nodes.append({
            "node_key": node_key,
            "parent_key": parent_key,
            "title": title,
            "note": node.get("note") if isinstance(node.get("note"), str) else "",
            "source_document_id": first_doc_id,
            "source_chunk_ids": chunk_ids,
            "origin": "ai",
            "unverified": unverified,
        })

    non_root = [node for node in nodes if node["node_key"] != "root"]
    if model_status == "ready":
        if len(nodes) < 3:
            raise ModelOutputInvalid("model_output_invalid", "status=ready 时节点数必须 ≥3。")
        if not non_root:
            raise ModelOutputInvalid("model_output_invalid", "status=ready 时缺少非根节点。")
        if all(not node["source_chunk_ids"] for node in non_root):
            # 规格 8.4：ready 但非根节点全部无有效 refs → 降级 insufficient_evidence
            raise AppError("insufficient_evidence", _INSUFFICIENT)
        if any(node["unverified"] or not node["source_chunk_ids"] for node in non_root):
            status = "partial"
        else:
            status = "ready"
    else:
        status = "partial"
    return nodes, status, truncated


def _generate_concept(zone_id: str, docs: list, title: str):
    pack, mapping = _build_m2_evidence(zone_id, docs)
    entries = pack.pop("evidence")
    batches = _m2_batches(entries)
    while len(batches) > 1:
        entries = [summary for batch in batches for summary in _summarize_m2_batch(pack, batch, title, mapping)]
        batches = _m2_batches(entries)
    final_mapping = {ref: mapping[ref] for ref in _m2_refs(entries)}
    system_extra = json.dumps({**pack, "phase": "build", "evidence": entries}, ensure_ascii=False)
    user_content = (
        f"标题：{title}\n"
        f"max_nodes：{config.MINDMAP_MAX_NODES}\n"
        f"max_depth：{config.MINDMAP_MAX_DEPTH}\n"
        "生成要求：按资料顺序生成复习导图；非根节点必须给出原文 evidence_id 或提炼段落中的 refs，"
        "根节点 parent_id 为 null；只输出一个 JSON 对象，字段为 status/nodes/missing。"
    )

    def call(content: str):
        return llm.chat_json(
            "mindmap",
            content,
            kind="mindmap",
            system_extra=system_extra,
            max_tokens=config.MINDMAP_MAX_TOKENS,
            temperature=config.MINDMAP_TEMPERATURE,
        )

    obj, _usage = call(user_content)
    try:
        nodes, status, truncated = _parse_concept_output(obj, zone_id, final_mapping)
    except ModelOutputInvalid as exc:
        correction = (
            "\n\n上一次输出未通过结构校验："
            f"{exc.message}"
            "。请重新输出一个完整 JSON 对象（status/nodes）；每个节点必须有唯一字符串 id 与 title，"
            "parent_id 必须指向同一对象中已存在的节点，根节点 parent_id 为 null。"
        )
        obj2, _usage2 = call(user_content + correction)
        nodes, status, truncated = _parse_concept_output(obj2, zone_id, final_mapping)

    warning = None
    if pack["coverage_gaps"]:
        status = "partial"
        warning = "导图基于已解析内容。覆盖缺口：" + "；".join(pack["coverage_gaps"]) + "。"
    return nodes, status, truncated, warning


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def generate(zone_id: str, document_ids: list[str], title: str, mode: str) -> dict:
    _require_zone(zone_id)
    if mode not in MODES:
        raise AppError("invalid_request", "不支持的导图模式。")
    docs = _selected_docs(zone_id, document_ids)
    map_title = (title or "").strip() or _DEFAULT_TITLE

    if mode == MODE_STRUCTURE:
        nodes, status, truncated = _generate_structure(zone_id, docs, map_title)
        method = "structure"
        warning = None
    else:
        try:
            nodes, status, truncated, warning = _generate_concept(zone_id, docs, map_title)
            method = _MODE["llm"]
        except AppError as exc:
            if exc.code not in _MODEL_ERROR_CODES:
                raise
            nodes, status, truncated = _generate_structure(zone_id, docs, map_title)
            method = _MODE["rule"]
            warning = f"AI 生成失败（{exc.message}），已用结构式生成。"

    mindmap_id = db.new_id()
    now = db.now()
    db.x(
        "INSERT INTO mindmaps (id, zone_id, title, mode, generated_by, theme, layout_json,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (mindmap_id, zone_id, map_title, mode, method, DEFAULT_THEME,
         json.dumps(_EMPTY_LAYOUT, ensure_ascii=False), now, now),
    )
    _insert_nodes(mindmap_id, nodes, now)
    return {
        "mindmap_id": mindmap_id,
        "mode": mode,
        "status": status,
        "truncated": bool(truncated),
        "method": method,
        "warning": warning,
        "theme": DEFAULT_THEME,
        "layout": {"texts": [], "edges": [], "strokes": []},
        "nodes": _rows_to_nodes(_node_rows(mindmap_id)),
    }


def list_maps(zone_id: str) -> list[dict]:
    rows = db.q(
        "SELECT m.id, m.zone_id, m.title, m.mode, m.generated_by, m.theme, m.updated_at,"
        " (SELECT COUNT(*) FROM mindmap_nodes n WHERE n.mindmap_id = m.id) AS node_count"
        " FROM mindmaps m WHERE m.zone_id = ? ORDER BY m.created_at DESC",
        (zone_id,),
    )
    return [
        {
            "id": row["id"],
            "zone_id": row["zone_id"],
            "title": row["title"],
            "mode": row["mode"],
            "generated_by": row["generated_by"],
            "theme": row["theme"] or DEFAULT_THEME,
            "updated_at": row["updated_at"],
            "node_count": int(row["node_count"] or 0),
        }
        for row in rows
    ]


def get_map(mindmap_id: str) -> dict:
    mindmap = _require_map(mindmap_id)
    return {
        "id": mindmap["id"],
        "zone_id": mindmap["zone_id"],
        "title": mindmap["title"],
        "mode": mindmap["mode"],
        "generated_by": mindmap["generated_by"],
        "theme": mindmap["theme"] or DEFAULT_THEME,
        "layout": _parse_layout(mindmap["layout_json"]),
        "updated_at": mindmap["updated_at"],
        "nodes": _rows_to_nodes(_node_rows(mindmap_id)),
    }


def _validate_saved_nodes(mindmap_id: str, nodes, old_rows) -> list[dict]:
    if not isinstance(nodes, list):
        raise AppError("invalid_request", "nodes 必须是数组。")
    if len(nodes) > config.MINDMAP_EDIT_MAX_NODES:
        raise AppError("invalid_request", f"节点数超过上限 {config.MINDMAP_EDIT_MAX_NODES}。")
    old_by_key = {row["node_key"]: row for row in old_rows}

    submitted: list[dict] = []
    seen: set[str] = set()
    for index, item in enumerate(nodes):
        if not isinstance(item, dict):
            raise AppError("invalid_request", f"nodes[{index}] 不是对象。")
        key = item.get("node_key")
        key = key.strip() if isinstance(key, str) else ""
        if not key:
            key = "u" + _sha12(f"{mindmap_id}|{index}|{item.get('title', '')}")
        if key in seen:
            raise AppError("invalid_request", "节点 node_key 重复。")
        seen.add(key)
        title = item.get("title")
        title = title.strip() if isinstance(title, str) else ""
        if not title:
            raise AppError("invalid_request", "节点标题不能为空。")
        note = item.get("note")
        note = note if isinstance(note, str) else ("" if note is None else str(note))
        parent_key = item.get("parent_key")
        parent_key = parent_key.strip() if isinstance(parent_key, str) and parent_key.strip() else None
        shape = _node_shape(item.get("shape") or DEFAULT_SHAPE)
        pos_x = _node_pos(item.get("pos_x"), "x")
        pos_y = _node_pos(item.get("pos_y"), "y")
        if (pos_x is None) != (pos_y is None):
            raise AppError("invalid_request", "节点位置不合法。")
        submitted.append({
            "node_key": key,
            "parent_key": parent_key,
            "title": title,
            "note": note,
            "shape": shape,
            "pos_x": pos_x,
            "pos_y": pos_y,
        })

    if "root" not in seen:
        raise AppError("invalid_request", "根节点不能删除。")
    for node in submitted:
        if node["node_key"] == "root":
            if node["parent_key"] is not None:
                raise AppError("invalid_request", "根节点不能有父节点。")
        else:
            if node["parent_key"] is None:
                raise AppError("invalid_request", "根节点只能有一个。")
            if node["parent_key"] not in seen:
                raise AppError("invalid_request", "父节点不存在。")
    for node in submitted:
        current = node["parent_key"]
        steps = 0
        while current is not None:
            steps += 1
            if steps > len(submitted):
                raise AppError("invalid_request", "节点父子关系存在环。")
            current = next((n["parent_key"] for n in submitted if n["node_key"] == current), None)
        if node["node_key"] != "root" and steps == 0:
            raise AppError("invalid_request", "根节点只能有一个。")

    new_rows: list[dict] = []
    for node in submitted:
        old = old_by_key.get(node["node_key"])
        if old is not None:
            changed = (
                node["title"] != old["title"]
                or node["note"] != (old["note"] or "")
                or node["parent_key"] != old["parent_key"]
                or node["shape"] != (old["shape"] or DEFAULT_SHAPE)
                or node["pos_x"] != (float(old["pos_x"]) if old["pos_x"] is not None else None)
                or node["pos_y"] != (float(old["pos_y"]) if old["pos_y"] is not None else None)
            )
            new_rows.append({
                "node_key": node["node_key"],
                "parent_key": node["parent_key"],
                "title": node["title"],
                "note": node["note"],
                "source_document_id": old["source_document_id"],
                "source_chunk_ids": _json_chunk_ids(old["source_chunk_ids"]),
                "origin": "user" if changed else old["origin"],
                "unverified": int(old["unverified"] or 0),
                "shape": node["shape"],
                "pos_x": node["pos_x"],
                "pos_y": node["pos_y"],
            })
        else:
            new_rows.append({
                "node_key": node["node_key"],
                "parent_key": node["parent_key"],
                "title": node["title"],
                "note": node["note"],
                "source_document_id": None,
                "source_chunk_ids": [],
                "origin": "user",
                "unverified": 0,
                "shape": node["shape"],
                "pos_x": node["pos_x"],
                "pos_y": node["pos_y"],
            })
    return new_rows


def _json_chunk_ids(raw) -> list[str]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data]


def save_nodes(mindmap_id: str, nodes: list[dict], theme=None, layout=None) -> dict:
    """保存导图节点 + 主题 + 自由布局（文本/关系线）。theme/layout 缺省时沿用库中值。"""
    mindmap = _require_map(mindmap_id)
    old_rows = _node_rows(mindmap_id)
    new_rows = _validate_saved_nodes(mindmap_id, nodes, old_rows)
    node_keys = {row["node_key"] for row in new_rows}

    if theme is None:
        theme = mindmap["theme"] or DEFAULT_THEME
    elif not isinstance(theme, str) or theme not in THEMES:
        raise AppError("invalid_request", "不支持的主题。")

    if layout is None:
        layout = _clean_layout(_parse_layout(mindmap["layout_json"]), node_keys)
    else:
        layout = _validate_layout(layout, node_keys)

    now = db.now()
    db.x("DELETE FROM mindmap_nodes WHERE mindmap_id = ?", (mindmap_id,))
    _insert_nodes(mindmap_id, new_rows, now)
    db.x(
        "UPDATE mindmaps SET theme = ?, layout_json = ?, updated_at = ? WHERE id = ?",
        (theme, json.dumps(layout, ensure_ascii=False), now, mindmap_id),
    )
    return {"ok": True, "updated_at": now, "theme": theme, "layout": layout}


def _infer_regenerate_docs(zone_id: str, old_rows) -> list:
    doc_ids: list[str] = []
    for row in old_rows:
        doc_id = row["source_document_id"]
        if isinstance(doc_id, str) and doc_id and doc_id not in doc_ids:
            doc_ids.append(doc_id)
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        rows = db.q(
            f"SELECT * FROM documents WHERE zone_id = ? AND id IN ({placeholders})",
            (zone_id, *doc_ids),
        )
        by_id = {row["id"]: row for row in rows}
        docs = [by_id[doc_id] for doc_id in doc_ids if doc_id in by_id]
        if docs:
            return docs
    # 溯源字段无法确定原选择时，退回该知识区全部资料（导图记录未保存 document_ids）
    return db.q("SELECT * FROM documents WHERE zone_id = ? ORDER BY created_at", (zone_id,))


def _merge_regenerated(old_rows, new_nodes: list[dict]):
    old_by_key = {row["node_key"]: row for row in old_rows}
    merged: list[dict] = []
    new_keys: set[str] = set()
    kept_user = 0
    for node in new_nodes:
        item = dict(node)
        old = old_by_key.get(node["node_key"])
        if old is not None:
            # v3.3：节点形状与手动位置属于用户视觉调整，重新生成时按 node_key 保留
            item["shape"] = old["shape"] or DEFAULT_SHAPE
            item["pos_x"] = float(old["pos_x"]) if old["pos_x"] is not None else None
            item["pos_y"] = float(old["pos_y"]) if old["pos_y"] is not None else None
        if old is not None and old["origin"] == "user":
            item["title"] = old["title"]
            item["note"] = old["note"] or ""
            item["origin"] = "user"
            kept_user += 1
        elif old is not None:
            item["origin"] = "ai"
        merged.append(item)
        new_keys.add(node["node_key"])
    removed = 0
    for row in old_rows:
        key = row["node_key"]
        if key in new_keys:
            continue
        if row["origin"] == "user":
            parent_key = row["parent_key"]
            if parent_key not in new_keys:
                parent_key = "root"
            merged.append({
                "node_key": key,
                "parent_key": parent_key,
                "title": row["title"],
                "note": (row["note"] or "") + "[来源已变更]",
                "source_document_id": row["source_document_id"],
                "source_chunk_ids": _json_chunk_ids(row["source_chunk_ids"]),
                "origin": "user",
                "unverified": int(row["unverified"] or 0),
                "shape": row["shape"] or DEFAULT_SHAPE,
                "pos_x": float(row["pos_x"]) if row["pos_x"] is not None else None,
                "pos_y": float(row["pos_y"]) if row["pos_y"] is not None else None,
            })
            kept_user += 1
        else:
            removed += 1
    return merged, kept_user, removed


def regenerate(mindmap_id: str) -> dict:
    mindmap = _require_map(mindmap_id)
    zone_id = mindmap["zone_id"]
    mode = mindmap["mode"]
    title = mindmap["title"]
    old_rows = _node_rows(mindmap_id)
    docs = _infer_regenerate_docs(zone_id, old_rows)
    if not docs:
        raise AppError("insufficient_evidence", _INSUFFICIENT)
    if mode == MODE_STRUCTURE:
        new_nodes, _status, _truncated = _generate_structure(zone_id, docs, title)
        method = "structure"
        warning = None
    else:
        try:
            new_nodes, _status, _truncated, warning = _generate_concept(zone_id, docs, title)
            method = _MODE["llm"]
        except AppError as exc:
            if exc.code not in _MODEL_ERROR_CODES:
                raise
            new_nodes, _status, _truncated = _generate_structure(zone_id, docs, title)
            method = _MODE["rule"]
            warning = f"AI 生成失败（{exc.message}），已用结构式生成。"

    merged, kept_user, removed = _merge_regenerated(old_rows, new_nodes)
    node_keys = {row["node_key"] for row in merged}
    theme = mindmap["theme"] or DEFAULT_THEME
    layout = _clean_layout(_parse_layout(mindmap["layout_json"]), node_keys)
    now = db.now()
    db.x("DELETE FROM mindmap_nodes WHERE mindmap_id = ?", (mindmap_id,))
    _insert_nodes(mindmap_id, merged, now)
    db.x(
        "UPDATE mindmaps SET updated_at = ?, generated_by = ?, theme = ?, layout_json = ?"
        " WHERE id = ?",
        (now, method, theme, json.dumps(layout, ensure_ascii=False), mindmap_id),
    )
    return {
        "mindmap_id": mindmap_id,
        "mode": mode,
        "nodes": _rows_to_nodes(_node_rows(mindmap_id)),
        "kept_user_nodes": kept_user,
        "removed_nodes": removed,
        "method": method,
        "warning": warning,
        "theme": theme,
        "layout": layout,
    }
