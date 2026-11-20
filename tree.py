"""知识区内的多级知识树。

知识区仍是最高层“科目分区”；知识节点负责在分区内组织章节/专题。
会话可以绑定一个节点，检索范围默认是该节点及其所有子节点。
"""
from __future__ import annotations

import db
import llm


def _require_node(node_id: str):
    row = db.q1("SELECT * FROM knowledge_nodes WHERE id = ?", (node_id,))
    if row is None:
        raise llm.AppError("node_not_found", "知识节点不存在。")
    return row


def _require_zone(zone_id: str) -> None:
    if not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")


def _node_name(raw) -> str:
    if not isinstance(raw, str):
        raise llm.AppError("invalid_request", "节点名称必须是字符串。")
    name = raw.strip()
    if not (1 <= len(name) <= 30):
        raise llm.AppError("invalid_request", "节点名称需为 1–30 个字符。")
    return name


def _parent_id(raw) -> str | None:
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        raise llm.AppError("invalid_request", "parent_id 必须是字符串或空值。")
    return raw.strip() or None


def ensure_zone_node(zone_id: str, node_id: str | None) -> None:
    if not node_id:
        return
    node = _require_node(node_id)
    if node["zone_id"] != zone_id:
        raise llm.AppError("invalid_request", "节点与知识区不匹配。")


def list_nodes(zone_id: str | None = None) -> list[dict]:
    rows = db.q(
        "SELECT n.*,"
        " (SELECT COUNT(*) FROM documents d WHERE d.node_id = n.id) AS doc_count,"
        " (SELECT COUNT(*) FROM knowledge_nodes c WHERE c.parent_id = n.id) AS child_count,"
        " (SELECT COUNT(*) FROM conversations v WHERE v.node_id = n.id) AS conversation_count"
        " FROM knowledge_nodes n"
        + (" WHERE n.zone_id = ?" if zone_id else "")
        + " ORDER BY n.zone_id, n.parent_id, n.sort_order, n.created_at",
        (zone_id,) if zone_id else (),
    )
    by_id = {row["id"]: row for row in rows}

    def path_of(node_id: str) -> list[str]:
        parts: list[str] = []
        current = node_id
        guard = 0
        while current and guard < 100:
            row = by_id.get(current)
            if row is None:
                break
            parts.append(row["name"])
            current = row["parent_id"]
            guard += 1
        return list(reversed(parts))

    out = []
    for row in rows:
        item = dict(row)
        item["path"] = " / ".join(path_of(row["id"]))
        out.append(item)
    return out


def create_node(zone_id: str, parent_id: str | None, name: str) -> dict:
    _require_zone(zone_id)
    name = _node_name(name)
    parent_id = _parent_id(parent_id)
    if parent_id:
        parent = _require_node(parent_id)
        if parent["zone_id"] != zone_id:
            raise llm.AppError("invalid_request", "父节点不属于该知识区。")
    order = db.q1(
        "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM knowledge_nodes WHERE zone_id = ?",
        (zone_id,),
    )["n"]
    node_id = db.new_id()
    now = db.now()
    db.x(
        "INSERT INTO knowledge_nodes (id, zone_id, parent_id, name, sort_order, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (node_id, zone_id, parent_id, name, order, now, now),
    )
    row = _require_node(node_id)
    out = dict(row)
    out["path"] = _node_path(node_id)
    out["doc_count"] = 0
    out["child_count"] = 0
    out["conversation_count"] = 0
    return out


def rename_node(node_id: str, name: str) -> dict:
    _require_node(node_id)
    name = _node_name(name)
    db.x(
        "UPDATE knowledge_nodes SET name = ?, updated_at = ? WHERE id = ?",
        (name, db.now(), node_id),
    )
    row = _require_node(node_id)
    out = dict(row)
    out["path"] = _node_path(node_id)
    return out


def delete_node(node_id: str) -> None:
    row = _require_node(node_id)
    child_count = db.q1("SELECT COUNT(*) AS c FROM knowledge_nodes WHERE parent_id = ?", (node_id,))["c"]
    doc_count = db.q1("SELECT COUNT(*) AS c FROM documents WHERE node_id = ?", (node_id,))["c"]
    conv_count = db.q1("SELECT COUNT(*) AS c FROM conversations WHERE node_id = ?", (node_id,))["c"]
    if child_count or doc_count or conv_count:
        raise llm.AppError(
            "node_not_empty",
            "节点下仍有子节点、资料或会话，暂时不能删除。",
        )
    if row["zone_id"]:
        db.x("DELETE FROM knowledge_nodes WHERE id = ?", (node_id,))


def move_document(document_id: str, node_id: str | None) -> dict:
    doc = db.q1("SELECT * FROM documents WHERE id = ?", (document_id,))
    if doc is None:
        raise llm.AppError("invalid_request", "资料不存在。")
    if node_id:
        node = _require_node(node_id)
        if node["zone_id"] != doc["zone_id"]:
            raise llm.AppError("invalid_request", "节点与资料不属于同一知识区。")
    db.x("UPDATE documents SET node_id = ? WHERE id = ?", (node_id, document_id))
    return {"ok": True}


def _node_path(node_id: str | None) -> str:
    if not node_id:
        return ""
    names: list[str] = []
    current = node_id
    guard = 0
    while current and guard < 100:
        row = db.q1("SELECT name, parent_id FROM knowledge_nodes WHERE id = ?", (current,))
        if row is None:
            break
        names.append(row["name"])
        current = row["parent_id"]
        guard += 1
    return " / ".join(reversed(names))


def subtree_node_ids(node_id: str) -> list[str]:
    _require_node(node_id)
    rows = db.q(
        "WITH RECURSIVE sub(id) AS ("
        " SELECT id FROM knowledge_nodes WHERE id = ?"
        " UNION ALL"
        " SELECT n.id FROM knowledge_nodes n JOIN sub ON n.parent_id = sub.id"
        ") SELECT id FROM sub",
        (node_id,),
    )
    return [row["id"] for row in rows]


def scope_document_ids(zone_id: str, node_id: str | None) -> list[str] | None:
    """返回会话允许检索的文档 ID；None 表示整个知识区。"""
    if not node_id:
        return None
    node = _require_node(node_id)
    if node["zone_id"] != zone_id:
        raise llm.AppError("invalid_request", "会话绑定的节点不属于当前知识区。")
    ids = subtree_node_ids(node_id)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = db.q(
        f"SELECT id FROM documents WHERE zone_id = ? AND node_id IN ({placeholders})",
        (zone_id, *ids),
    )
    return [row["id"] for row in rows]


def scope_label(zone_id: str, node_id: str | None) -> str:
    zone = db.q1("SELECT name FROM zones WHERE id = ?", (zone_id,))
    zone_name = zone["name"] if zone else ""
    node_name = _node_path(node_id)
    if node_name:
        return f"{zone_name} / {node_name}" if zone_name else node_name
    return zone_name