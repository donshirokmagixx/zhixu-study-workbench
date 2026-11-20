"""检索、知识树、引用漂移与安全基线的无 HTTP 回归。

不调用真实模型，不使用正式 data/，也不依赖 TestClient 或本地端口。
用法：.venv\\Scripts\\python.exe scripts\\regression.py
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TMP_ROOT = ROOT / "data" / "tmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)
TMP = tempfile.mkdtemp(prefix="zhixu_regression_", dir=TMP_ROOT)
os.environ["ZHIXU_DATA_DIR"] = TMP
sys.path.insert(0, str(ROOT))

import app as appmod  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import quality  # noqa: E402
import rag  # noqa: E402
import tree  # noqa: E402

PASS = 0
FAILS: list[str] = []
CHAT_REQUESTS: list[tuple[list[dict], str]] = []


def check(name: str, condition: bool, detail="") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAILS.append(name)
        print(f"FAIL  {name}  |  {str(detail)[:300]}")


def fake_chat_json(skill_key, user_content, *, kind, system_extra="", max_tokens, temperature, timeout=60):
    evidence = json.loads(system_extra.rsplit("\n", 1)[-1])["evidence"]
    CHAT_REQUESTS.append((evidence, user_content))
    usage = {
        "model": "regression-fake", "prompt_tokens": 1, "completion_tokens": 1,
        "latency_ms": 1, "ok": 1, "error": None,
        "skill_name": skill_key, "skill_sha256": "regression",
    }
    return {
        "status": "answered",
        "answer": "这是基于当前证据的回答。[e1]" if evidence else "先看一个例子。[e1]",
        "citations": ["e1"],
        "missing_evidence": [],
        "stop_reason": "evidence_sufficient",
    }, usage


def upload(zone_id, node_id, filename, text):
    config.ensure_dirs()
    ext = Path(filename).suffix.lower()
    doc_id = db.new_id()
    saved = config.UPLOAD_DIR / f"{doc_id}{ext}"
    encoded = text.encode("utf-8")
    saved.write_bytes(encoded)
    db.x(
        "INSERT INTO documents (id, zone_id, node_id, filename, ext, size, sha256, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, zone_id, node_id, filename, ext, len(encoded),
         hashlib.sha256(encoded).hexdigest(), "parsing", db.now()),
    )
    result = rag.parse_and_index(doc_id)
    check("解析并索引 " + filename, result.get("status") == "ready" and result.get("chunk_count", 0) > 0, result)
    return doc_id


try:
    db.init_db()
    llm.chat_json = fake_chat_json

    # 安全防护纯函数
    check("本机 Host 放行", appmod._request_guard_error(
        "127.0.0.1:8788", "http://127.0.0.1:8788", "same-origin", "1024") is None)
    check("拒绝外部 Host", appmod._request_guard_error(
        "evil.example", None, None, None) == (403, "request_forbidden", "仅允许本机访问。"))
    check("拒绝跨站请求", appmod._request_guard_error(
        "127.0.0.1:8788", None, "cross-site", None) == (403, "request_forbidden", "跨站请求被拒绝。"))
    check("拒绝不同源 Origin", appmod._request_guard_error(
        "127.0.0.1:8788", "http://evil.example", None, None) == (403, "request_forbidden", "请求来源与当前服务不一致。"))
    check("拒绝超大请求体", appmod._request_guard_error(
        "127.0.0.1:8788", None, None, str(config.REQUEST_MAX_BYTES + 1)) == (413, "invalid_request", "请求体超过 25MB 上限。"))
    check("安全响应头已配置", appmod._SECURITY_HEADERS.get("X-Content-Type-Options") == "nosniff"
          and appmod._SECURITY_HEADERS.get("X-Frame-Options") == "DENY", appmod._SECURITY_HEADERS)

    zone_a = appmod.create_zone({"name": "高等数学"})
    zone_b = appmod.create_zone({"name": "数据结构"})
    root = appmod.create_node({"zone_id": zone_a["id"], "name": "第一章 导数"})
    child = appmod.create_node({"zone_id": zone_a["id"], "parent_id": root["id"], "name": "1.1 导数与微分"})
    other = appmod.create_node({"zone_id": zone_a["id"], "name": "第二章 练习"})
    check("创建多级知识树", root.get("id") and child.get("parent_id") == root["id"]
          and other.get("path") == "第二章 练习", [root, child, other])

    doc_derivative = upload(
        zone_a["id"], child["id"], "微积分笔记.md",
        "# 微积分笔记\n## 变化率与切线\n函数求导可以得到切线斜率。求导前需要判断函数在该点是否可导，可导必连续。",
    )
    doc_tree = upload(
        zone_a["id"], other["id"], "二叉树复习.md",
        "二叉树的中序遍历按照左子树、根节点、右子树的顺序访问。二叉搜索树中序遍历结果非递减。",
    )
    doc_title = upload(
        zone_a["id"], None, "卷积定理.md",
        "卷积定理说明时域卷积对应频域相乘。卷积满足交换律、结合律与分配律。",
    )
    doc_prose = upload(
        zone_a["id"], None, "课堂练习.md",
        "练习课讨论卷积的计算步骤和典型题型，主要记录课堂习题安排。",
    )
    doc_table = upload(
        zone_a["id"], None, "性质速查.md",
        "| 性质 | 条件 | 结论 |\n| --- | --- | --- |\n| 可导 | 极限存在 | 必连续 |\n| 连续 | 极限存在 | 不一定可导 |\n| 可微 | 可导 | 等价 |",
    )
    upload(zone_b["id"], None, "图与树.md", "二叉树是每个节点最多有两个子节点的树结构。图可以用邻接表存储。")

    synonym_results = rag.retrieve(zone_a["id"], "导数是什么")
    check("领域同义词召回", any("求导" in item["text"] and item["document_id"] == doc_derivative
                            for item in synonym_results), synonym_results)
    check("疑问词清洗", "什么" not in rag._clean_question("导数是什么"), rag._clean_question("导数是什么"))

    title_results = rag.retrieve(zone_a["id"], "卷积定理")
    check("标题命中优先", title_results and title_results[0]["document_id"] == doc_title, title_results[:3])
    check("标题命中标记", title_results and title_results[0]["title_hit"] is True, title_results[:1])

    check("表格识别", rag._is_tableish("| 性质 | 说明 |\n| --- | --- |\n| 可导 | 必连续 |\n| 连续 | 不一定可导 |"))
    table_results = rag.retrieve(zone_a["id"], "性质表")
    check("表格块参与重排", any(item["document_id"] == doc_table and item["table_hit"] for item in table_results), table_results)

    conv_node = appmod.create_conversation({"zone_id": zone_a["id"], "node_id": child["id"]})
    chat = appmod.chat({"conversation_id": conv_node["id"], "question": "变化率与切线有什么关系"})
    check("节点会话回答", chat.get("status") == "answered", chat)
    messages = appmod.list_messages(conv_node["id"])
    citation = messages[-1]["citations"][0]
    check("引用返回完整原文", citation.get("text") and "切线斜率" in citation["text"], citation)
    check("引用当前状态", citation.get("source_state") == "current", citation)

    upload_path = config.UPLOAD_DIR / f"{doc_derivative}.md"
    upload_path.write_text("变化率内容已经改写。当前索引原文不再包含原来的切线斜率表述。", encoding="utf-8")
    reindex = appmod.reindex_document(doc_derivative)
    check("重建索引", reindex.get("status") in ("ready", "ready_with_warning") and reindex.get("chunk_count", 0) > 0, reindex)
    drifted = appmod.list_messages(conv_node["id"])[-1]["citations"][0]
    check("引用漂移检测", drifted.get("source_state") == "drifted" and "已经改写" in (drifted.get("text") or ""), drifted)

    conv_scope = appmod.create_conversation({"zone_id": zone_a["id"], "node_id": root["id"]})
    scoped = appmod.chat({"conversation_id": conv_scope["id"], "question": "二叉树的中序遍历是什么"})
    check("节点范围隔离", scoped.get("retrieved") == 0 and not CHAT_REQUESTS[-1][0], scoped)
    check("零命中调用模型并标注通用讲解", scoped.get("status") == "general_knowledge"
          and scoped.get("usage", {}).get("completion_tokens") == 1
          and "以下为通用讲解" in scoped.get("answer", "")
          and "先看一个例子。" in scoped.get("answer", ""), scoped)
    check("通用讲解过滤虚构的课件引用", scoped.get("citations") == []
          and "[e1]" not in scoped.get("answer", ""), scoped)
    appmod.chat({"conversation_id": conv_scope["id"], "question": "能再举例吗？"})
    check("通用讲解可继续追问", "先看一个例子。" in CHAT_REQUESTS[-1][1], CHAT_REQUESTS[-1][1])

    conv_b = appmod.create_conversation({"zone_id": zone_b["id"]})
    zone_isolated = appmod.chat({"conversation_id": conv_b["id"], "question": "导数是什么"})
    check("知识区隔离", zone_isolated.get("status") == "general_knowledge"
          and zone_isolated.get("retrieved") == 0 and not CHAT_REQUESTS[-1][0], zone_isolated)
    tree_hit = appmod.chat({"conversation_id": conv_b["id"], "question": "二叉树是什么"})
    check("B 区可检索自身资料", tree_hit.get("status") == "answered", tree_hit)

    try:
        appmod.delete_node(child["id"])
        check("有资料节点拒绝删除", False, "未抛出 node_not_empty")
    except llm.AppError as exc:
        check("有资料节点拒绝删除", exc.code == "node_not_empty", exc.message)

    empty = appmod.create_node({"zone_id": zone_a["id"], "name": "空节点"})
    appmod.delete_node(empty["id"])
    check("空节点可删除", not db.q1("SELECT id FROM knowledge_nodes WHERE id = ?", (empty["id"],)))

    appmod.move_document_node(doc_prose, {"node_id": other["id"]})
    moved_docs = appmod.list_documents(zone_id=zone_a["id"], node_id=other["id"])
    check("资料移动到节点", {item["id"] for item in moved_docs} >= {doc_tree, doc_prose}, moved_docs)

    # DPAPI 与旧明文迁移
    plain_key = "sk-regression-key-123456"
    config.save_config({"base_url": "http://127.0.0.1:1/v1", "api_key": plain_key, "model": "fake"})
    cfg_text = (Path(TMP) / "config.json").read_text(encoding="utf-8")
    check("配置中不保存明文 Key", plain_key not in cfg_text and "api_key_enc" in cfg_text, cfg_text)
    check("DPAPI 可解密", config.load_config().get("api_key") == plain_key, config.load_config())

    legacy = {"base_url": "http://127.0.0.1:1/v1", "api_key": "sk-legacy-123456", "model": "fake"}
    (Path(TMP) / "config.json").write_text(json.dumps(legacy), encoding="utf-8")
    migrated = config.ensure_config_security()
    migrated_text = (Path(TMP) / "config.json").read_text(encoding="utf-8")
    check("旧明文配置自动迁移", migrated and "sk-legacy-123456" not in migrated_text
          and config.load_config().get("api_key") == "sk-legacy-123456", migrated_text)

    # v3.2 增量断言（追加，不替代以上用例）
    normal_text = "课程资料中的正常中文说明，包含定义、条件、步骤与结论。" * 10
    noisy_text = "\ufffd" * 800 + "!!!!!!!!!!!!!!!!" * 30
    normal_score, _ = quality.score_text(normal_text)
    noisy_score, noisy_note = quality.score_text(noisy_text)
    check("v3.2 质量评分正常/乱码", normal_score >= 0.85 and noisy_score < 0.6 and bool(noisy_note),
          (normal_score, noisy_score, noisy_note))
    docs_with_quality = appmod.list_documents(zone_id=zone_a["id"])
    check("v3.2 文档质量字段", docs_with_quality and all("quality" in item and "quality_note" in item
          for item in docs_with_quality), docs_with_quality[:1])
    check("v3.2 导图 token 上限", config.MINDMAP_MAX_TOKENS == 4000, config.MINDMAP_MAX_TOKENS)
    front_text = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    check("v3.2 导图前端导出与折叠静态检查",
          all(symbol in front_text for symbol in (
              "exportMapSvg", "exportMapPng", "toggleMapCollapse", "wrapMapTitle",
              "mapExportSvgBtn", "mapExportPngBtn", "mapCollapseAllBtn", "mapExpandAllBtn",
          )))
    check("v3.2 规则版提示文案", "规则版" in front_text and "AI 生成失败" in front_text)

    print(f"\n== {PASS} PASS / {len(FAILS)} FAIL")
    if FAILS:
        print("失败:", FAILS)
        sys.exit(1)
finally:
    shutil.rmtree(TMP, ignore_errors=True)
