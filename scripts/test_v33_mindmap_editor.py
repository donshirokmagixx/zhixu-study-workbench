"""v3.3 导图编辑器后端回归（临时 DATA_DIR，不调模型、不碰真实数据）。

运行：
    .venv\\Scripts\\python.exe scripts\\test_v33_mindmap_editor.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ["ZHIXU_DATA_DIR"] = tempfile.mkdtemp(prefix="zhixu_v33_")
sys.path.insert(0, ROOT)

import db  # noqa: E402
import llm  # noqa: E402
import mindmap  # noqa: E402

PASS = 0
FAILS: list[str] = []


def check(name: str, cond, detail="") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAILS.append(name)
        print(f"FAIL  {name}  |  {str(detail)[:300]}")


def expect_error(name: str, code: str, fn) -> None:
    try:
        fn()
    except llm.AppError as exc:
        check(name, exc.code == code, f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"{type(exc).__name__}: {exc}")
    else:
        check(name, False, "没有抛错")


def seed() -> tuple[str, str]:
    db.init_db()
    zone_id = db.new_id()
    doc_id = db.new_id()
    now = db.now()
    db.x("INSERT INTO zones (id, name, created_at) VALUES (?,?,?)", (zone_id, "测试区", now))
    db.x(
        "INSERT INTO documents (id, zone_id, node_id, filename, ext, size, sha256, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, zone_id, None, "第一章.md", ".md", 10, "sha-test", "ready", now),
    )
    db.x(
        "INSERT INTO chunks (id, document_id, zone_id, ordinal, heading_path, page, text, text_sha256)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (db.new_id(), doc_id, zone_id, 0, "第一章 > 1.1 概念", 1, "概念正文内容。", "h1"),
    )
    db.x(
        "INSERT INTO chunks (id, document_id, zone_id, ordinal, heading_path, page, text, text_sha256)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (db.new_id(), doc_id, zone_id, 1, "第一章 > 1.2 性质", 1, "性质正文内容。", "h2"),
    )
    return zone_id, doc_id


def main() -> int:
    zone_id, doc_id = seed()
    result = mindmap.generate(zone_id, [doc_id], "测试导图", "structure")
    mm_id = result["mindmap_id"]
    check("生成返回 theme/layout", result.get("theme") == "fresh"
          and isinstance(result.get("layout"), dict), result)
    nodes = result["nodes"]
    check("生成节点默认 shape/pos", all(n.get("shape") == "roundrect" and n.get("pos_x") is None for n in nodes), nodes[:2])

    data = mindmap.get_map(mm_id)
    check("get_map 含 theme/layout", data.get("theme") == "fresh" and data.get("layout") == {"texts": [], "edges": [], "strokes": []}, data)
    root = next(n for n in data["nodes"] if n["node_key"] == "root")
    child = next(n for n in data["nodes"] if n["node_key"] != "root")

    # 保存：根节点改名（v3.3 放开）+ 形状 + 手动位置 + 主题 + 自由文本/关系线
    nodes_payload = []
    for node in data["nodes"]:
        item = {
            "node_key": node["node_key"],
            "parent_key": node.get("parent_key"),
            "title": node["title"],
            "note": node.get("note") or "",
            "shape": node.get("shape") or "roundrect",
            "pos_x": node.get("pos_x"),
            "pos_y": node.get("pos_y"),
            "source_document_id": node.get("source_document_id"),
            "source_chunk_ids": node.get("source_chunk_ids") or [],
            "origin": node.get("origin") or "ai",
            "unverified": node.get("unverified") or 0,
        }
        if node["node_key"] == "root":
            item["title"] = "幂级数"
            item["shape"] = "ellipse"
        if node["node_key"] == child["node_key"]:
            item["title"] = "用户改过的节点"
            item["shape"] = "circle"
            item["pos_x"] = 321.5
            item["pos_y"] = 88.25
        nodes_payload.append(item)

    layout = {
        "texts": [{"id": "t1", "text": "注意：收敛半径", "x": 120, "y": 80}],
        "edges": [{"id": "e1", "from_key": "root", "to_key": child["node_key"], "label": "推导"}],
        "strokes": [{"id": "s1", "color": "#b06a12", "width": 2,
                     "points": [{"x": 10, "y": 10}, {"x": 40, "y": 30}]}],
    }
    saved = mindmap.save_nodes(mm_id, nodes_payload, theme="ocean", layout=layout)
    check("save 返回 theme/layout", saved.get("theme") == "ocean" and saved.get("layout", {}).get("edges"), saved)

    again = mindmap.get_map(mm_id)
    root2 = next(n for n in again["nodes"] if n["node_key"] == "root")
    child2 = next(n for n in again["nodes"] if n["node_key"] == child["node_key"])
    check("根节点改名已保存", root2["title"] == "幂级数", root2)
    check("形状已保存", root2["shape"] == "ellipse" and child2["shape"] == "circle", (root2["shape"], child2["shape"]))
    check("手动位置已保存", child2["pos_x"] == 321.5 and child2["pos_y"] == 88.25, child2)
    check("主题已保存", again["theme"] == "ocean", again["theme"])
    check("自由布局已保存", again["layout"]["texts"][0]["text"] == "注意：收敛半径"
          and again["layout"]["edges"][0]["label"] == "推导", again["layout"])
    check("涂鸦已保存", bool(again["layout"].get("strokes"))
          and again["layout"]["strokes"][0]["points"][1]["x"] == 40, again["layout"].get("strokes"))

    # 重新生成：结构式 → 保留根节点新标题/手改节点/形状/位置/主题/布局
    regen = mindmap.regenerate(mm_id)
    root3 = next(n for n in regen["nodes"] if n["node_key"] == "root")
    child3 = next((n for n in regen["nodes"] if n["node_key"] == child["node_key"]), None)
    check("重新生成保留主题/布局", regen.get("theme") == "ocean" and regen.get("layout", {}).get("texts"), regen)
    check("重新生成保留根标题", root3["title"] == "幂级数", root3)
    check("重新生成保留节点形状/位置", child3 is not None and child3["shape"] == "circle"
          and child3["pos_x"] == 321.5, child3)

    # 无效输入
    expect_error("非法 shape 被拒", "invalid_request", lambda: mindmap.save_nodes(
        mm_id, [{"node_key": "root", "parent_key": None, "title": "x", "shape": "star"}]))
    expect_error("非法主题被拒", "invalid_request", lambda: mindmap.save_nodes(
        mm_id, [{"node_key": "root", "parent_key": None, "title": "x"}], theme="pink"))
    base_nodes = [{"node_key": "root", "parent_key": None, "title": "x"}]
    expect_error("文本超上限被拒", "invalid_request", lambda: mindmap.save_nodes(
        mm_id, base_nodes, layout={"texts": [{"id": f"t{i}", "text": "x", "x": 1, "y": 1} for i in range(31)], "edges": []}))
    expect_error("关系线端点不存在被拒", "invalid_request", lambda: mindmap.save_nodes(
        mm_id, base_nodes, layout={"texts": [], "edges": [{"from_key": "root", "to_key": "nope"}]}))
    expect_error("自连被拒", "invalid_request", lambda: mindmap.save_nodes(
        mm_id, base_nodes, layout={"texts": [], "edges": [{"from_key": "root", "to_key": "root"}]}))
    expect_error("根节点缺父合法但唯一根限制", "invalid_request", lambda: mindmap.save_nodes(
        mm_id, [{"node_key": "root", "parent_key": None, "title": "x"},
                {"node_key": "a", "parent_key": None, "title": "y"}]))

    # 旧库兼容：mindmaps 行 theme/layout_json 为空时按默认值返回
    legacy_id = db.new_id()
    db.x("INSERT INTO mindmaps (id, zone_id, title, mode, generated_by, created_at, updated_at)"
         " VALUES (?,?,?,?,?,?,?)", (legacy_id, zone_id, "旧图", "structure", "structure", db.now(), db.now()))
    db.x("INSERT INTO mindmap_nodes (id, mindmap_id, node_key, parent_key, title, note, origin, unverified, updated_at)"
         " VALUES (?,?,?,?,?,?,?,?,?)", (db.new_id(), legacy_id, "root", None, "旧根", "", "ai", 0, db.now()))
    legacy = mindmap.get_map(legacy_id)
    check("旧数据默认值", legacy["theme"] == "fresh" and legacy["layout"] == {"texts": [], "edges": [], "strokes": []}
          and legacy["nodes"][0]["shape"] == "roundrect", legacy)

    # PDF 导出接口（PNG → PDF，纯本地转换）
    import fitz  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    import app as appmod  # noqa: E402

    image_doc = fitz.open()
    image_page = image_doc.new_page(width=300, height=200)
    image_page.insert_text((20, 40), "mindmap export test", fontsize=14)
    png_bytes = image_page.get_pixmap(dpi=150).tobytes("png")
    image_doc.close()
    with TestClient(appmod.app) as client:
        resp = client.post("/api/mindmaps/export/pdf",
                           files={"file": ("map.png", png_bytes, "image/png")})
        bad = client.post("/api/mindmaps/export/pdf",
                          files={"file": ("map.txt", b"not-a-png", "text/plain")})
    check("导图 PDF 导出接口", resp.status_code == 200 and resp.content[:4] == b"%PDF",
          (resp.status_code, resp.content[:12]))
    check("PDF 导出拒绝非 PNG", bad.status_code == 400, (bad.status_code, bad.text[:120]))

    print(f"\n== 结果：{PASS} PASS / {len(FAILS)} FAIL")
    if FAILS:
        print("失败项：", FAILS)
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
