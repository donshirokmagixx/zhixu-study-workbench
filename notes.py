"""学术笔记的本地保存和带来源的 AI 起稿。"""
from __future__ import annotations

import json
import re
import sqlite3

import config
import db
import llm
import rag
import tree

_METADATA = "id, title, style, zone_id, node_id, created_at, updated_at"
_HTML_TAG = re.compile(r"<(?:!--|!doctype\b|/?[a-z][a-z0-9:_-]*(?=[\s/>]))[^>]*>", re.I)
_DRAFT_MARKUP = re.compile(
    r"</[a-z][a-z0-9:_-]*\s*>|<(?:!--|!doctype\b|(?:script|style|iframe|img|svg|input|link|meta|br|hr)\b)[^>]*>",
    re.I,
)


def _text(payload: dict, key: str, limit: int, label: str) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise llm.AppError("invalid_request", f"{label}须为不超过 {limit} 个字符的文本。")
    return value


def _optional_id(value, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 128:
        raise llm.AppError("invalid_request", f"{name} 必须是有效的字符串或空值。")
    return value.strip() or None


def _scope(payload: dict) -> tuple[str | None, str | None]:
    zone_id = _optional_id(payload.get("zone_id"), "zone_id")
    node_id = _optional_id(payload.get("node_id"), "node_id")
    if node_id and not zone_id:
        raise llm.AppError("invalid_request", "选择知识节点前须指定知识区。")
    if zone_id and not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    if node_id:
        tree.ensure_zone_node(zone_id, node_id)
    return zone_id, node_id


def _live_sources(conn: sqlite3.Connection, sources: list, zone_id: str | None,
                  node_id: str | None, *, strict: bool) -> list[dict]:
    if not sources:
        return []
    allowed = None
    if zone_id and node_id:
        try:
            allowed = set(tree.scope_document_ids(zone_id, node_id))
        except llm.AppError as exc:
            if not strict and exc.code == "node_not_found":
                return []
            raise
    result = []
    seen = set()
    for source in sources:
        doc_id = source.get("document_id") if isinstance(source, dict) else None
        page = source.get("page") if isinstance(source, dict) else None
        if (not isinstance(doc_id, str) or len(doc_id) > 128
                or (page is not None and (type(page) is not int or not 1 <= page <= 2**63 - 1))):
            raise llm.AppError("invalid_request", "来源须包含资料 ID 和有效页码。")
        if (doc_id, page) in seen:
            continue
        seen.add((doc_id, page))
        row = None
        if zone_id and (allowed is None or doc_id in allowed):
            row = conn.execute(
                "SELECT d.id AS document_id, d.filename, c.page, d.ext"
                " FROM documents d JOIN chunks c ON c.document_id = d.id"
                " WHERE d.id = ? AND d.zone_id = ? AND c.page IS ? LIMIT 1",
                (doc_id, zone_id, page),
            ).fetchone()
        if row:
            result.append(dict(row))
        elif strict:
            raise llm.AppError("invalid_request", "来源页码不存在或不在当前知识范围内。")
    return result


def list_notes() -> list[dict]:
    return [dict(row) for row in db.q(
        f"SELECT {_METADATA} FROM scholar_notes ORDER BY updated_at DESC, rowid DESC"
    )]


def get_note(note_id: str) -> dict:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM scholar_notes WHERE id = ?", (note_id,)).fetchone()
        if row is None:
            raise llm.AppError("note_not_found", "笔记不存在。")
        note = dict(row)
        note["sources"] = _live_sources(
            conn, json.loads(note["sources"]), note["zone_id"], note["node_id"], strict=False,
        )
        return note


def save_note(payload: dict, note_id: str | None = None) -> dict:
    title = _text(payload, "title", config.NOTE_TITLE_MAX_CHARS, "标题").strip()
    if not title:
        raise llm.AppError("invalid_request", "请输入笔记标题。")
    body = _text(payload, "body", config.NOTE_BODY_MAX_CHARS, "正文")
    style = payload.get("style")
    if style not in ("paper", "journal"):
        raise llm.AppError("invalid_request", "请选择有效的笔记模板。")
    sources = payload.get("sources", [])
    if not isinstance(sources, list) or len(sources) > config.RETRIEVE_FINAL:
        raise llm.AppError("invalid_request", "来源列表无效或超出数量上限。")

    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        zone_id, node_id = _scope(payload)
        sources = _live_sources(conn, sources, zone_id, node_id, strict=True)
        now = db.now()
        values = title, body, style, zone_id, node_id, json.dumps(sources, ensure_ascii=False)
        if note_id is None:
            row = conn.execute(
                "INSERT INTO scholar_notes (title, body, style, zone_id, node_id, sources, id, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING *",
                (*values, db.new_id(), now, now),
            ).fetchone()
        else:
            row = conn.execute(
                "UPDATE scholar_notes SET title = ?, body = ?, style = ?, zone_id = ?, node_id = ?,"
                " sources = ?, updated_at = ? WHERE id = ? RETURNING *",
                (*values, now, note_id),
            ).fetchone()
            if row is None:
                raise llm.AppError("note_not_found", "笔记不存在。")
        return {**dict(row), "sources": sources}


def delete_note(note_id: str) -> None:
    if not db.x("DELETE FROM scholar_notes WHERE id = ?", (note_id,)):
        raise llm.AppError("note_not_found", "笔记不存在。")


def _scope_passages(zone_id: str, node_id: str | None, query: str) -> list[dict]:
    document_ids = tree.scope_document_ids(zone_id, node_id)
    if query:
        return rag.retrieve(zone_id, query, document_ids=document_ids)
    if document_ids == []:
        return []
    sql = (
        "SELECT c.id AS chunk_id, c.document_id, c.page, c.heading_path, c.text,"
        " c.text_sha256, d.filename, d.ext FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE c.zone_id = ?"
    )
    params = [zone_id]
    if document_ids is not None:
        placeholders = ",".join("?" for _ in document_ids)
        sql += f" AND c.document_id IN ({placeholders})"
        params.extend(document_ids)
    sql += " ORDER BY c.ordinal, d.created_at, d.id LIMIT ?"
    return [dict(row) for row in db.q(sql, (*params, config.RETRIEVE_FINAL))]


def draft_note(payload: dict) -> dict:
    title = _text(payload, "title", config.NOTE_TITLE_MAX_CHARS, "标题").strip()
    body = _text(payload, "body", config.NOTE_BODY_MAX_CHARS, "正文").strip()
    zone_id, node_id = _scope(payload)
    scope = None
    evidence, allowed = [], {}
    if zone_id:
        scope = tree.scope_label(zone_id, node_id)
        query = f"{title}\n{body[:config.CONTEXT_MAX_CHARS]}".strip()
        items = _scope_passages(zone_id, node_id, query)
        pack, allowed = rag.build_evidence(zone_id, "", items)
        evidence = pack["evidence"]
        if not allowed and not body:
            raise llm.AppError("insufficient_evidence", "未在当前知识范围内找到可用片段，请调整主题或补充正文。")
    elif not title and not body:
        raise llm.AppError("invalid_request", "请输入笔记标题或正文，或选择知识范围。")

    result, _ = llm.chat_json(
        "notebook_draft", json.dumps({
            "title": title, "body": body, "scope": scope, "evidence": evidence,
        }, ensure_ascii=False),
        kind="notebook_draft", max_tokens=config.NOTE_DRAFT_MAX_TOKENS, temperature=config.CHAT_TEMPERATURE,
    )
    draft = result.get("body")
    source_ids = result.get("source_ids")
    if (not isinstance(draft, str) or not draft.strip() or len(draft) > config.NOTE_BODY_MAX_CHARS
            or not isinstance(source_ids, list) or _DRAFT_MARKUP.search(draft)):
        raise llm.ModelOutputInvalid(message="模型返回的笔记格式无效，请重试。")
    sources = []
    seen = set()
    for key in source_ids:
        item = allowed.get(key) if isinstance(key, str) else None
        if item is None:
            continue
        locator = (item["document_id"], item["page"])
        if locator not in seen:
            seen.add(locator)
            sources.append({field: item[field] for field in ("document_id", "filename", "page", "ext")})
    if allowed and not body and not sources:
        raise llm.ModelOutputInvalid(message="模型未返回可核对的笔记来源，请重试。")
    return {"body": draft.strip(), "sources": sources}


def _recap_items(result: dict, key: str, label: str, maximum: int) -> list[str]:
    value = result.get(key, [])
    if not isinstance(value, list) or len(value) > maximum:
        raise llm.ModelOutputInvalid(message=f"课堂摘要的{label}格式无效，请重试。")
    items = []
    for item in value:
        if not isinstance(item, str):
            raise llm.ModelOutputInvalid(message=f"课堂摘要的{label}格式无效，请重试。")
        text = item.strip()
        if not text or len(text) > 300 or _HTML_TAG.search(text):
            raise llm.ModelOutputInvalid(message=f"课堂摘要的{label}格式无效，请重试。")
        items.append(text)
    return items


def _recap_sections(result: dict) -> list[dict]:
    value = result.get("sections")
    if not isinstance(value, list) or not 1 <= len(value) <= 12:
        raise llm.ModelOutputInvalid(message="课堂摘要的章节结构无效，请重试。")
    sections = []
    for item in value:
        if not isinstance(item, dict):
            raise llm.ModelOutputInvalid(message="课堂摘要的章节结构无效，请重试。")
        title = item.get("title")
        summary = item.get("summary")
        points = _recap_items(item, "key_points", "章节要点", 6)
        if (not isinstance(title, str) or not 2 <= len(title.strip()) <= 40
                or not isinstance(summary, str) or not 10 <= len(summary.strip()) <= 600
                or not points or _HTML_TAG.search(title + summary)):
            raise llm.ModelOutputInvalid(message="课堂摘要的章节结构无效，请重试。")
        sections.append({
            "title": title.strip(),
            "summary": summary.strip(),
            "key_points": points,
        })
    return sections


def summarize_transcript(payload: dict) -> dict:
    """把整堂课的录音转写整理为总览 + 分节回忆摘要；转写只作数据。"""
    title = _text(payload, "title", config.NOTE_TITLE_MAX_CHARS, "标题").strip()
    transcript = _text(
        payload, "transcript", config.NOTE_TRANSCRIPT_MAX_CHARS, "录音转写",
    ).strip()
    if len(transcript) < 10:
        raise llm.AppError("invalid_request", "录音转写内容过短，暂时无法生成摘要。")
    zone_id, node_id = _scope(payload)
    scope = tree.scope_label(zone_id, node_id) if zone_id else ""
    result, _ = llm.chat_json(
        "lecture_recap",
        json.dumps({
            "title": title,
            "scope": scope,
            "transcript": transcript,
        }, ensure_ascii=False),
        kind="lecture_recap",
        max_tokens=config.NOTE_RECAP_MAX_TOKENS,
        temperature=config.CHAT_TEMPERATURE,
    )
    overview = result.get("overview")
    if (not isinstance(overview, str) or not overview.strip() or len(overview) > 2000
            or _HTML_TAG.search(overview)):
        raise llm.ModelOutputInvalid(message="模型返回的课堂摘要格式无效，请重试。")
    return {
        "overview": overview.strip(),
        "sections": _recap_sections(result),
        "action_items": _recap_items(result, "action_items", "行动项", 8),
        "questions": _recap_items(result, "questions", "待确认问题", 8),
    }
