"""v3.2 功能 B 验收：截断检测与 M2 失败回退（不调真实模型）。"""
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
TMP = Path(tempfile.mkdtemp(prefix="zhixu_fallback_", dir=TMP_ROOT))
os.environ["ZHIXU_DATA_DIR"] = str(TMP)
sys.path.insert(0, str(ROOT))

import app  # noqa: E402
import config  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import mindmap  # noqa: E402
import rag  # noqa: E402

PASS = 0
FAILS: list[str] = []


def check(name: str, condition: bool, detail="") -> None:
    global PASS
    if condition:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAILS.append(name)
        print(f"FAIL  {name}  |  {str(detail)[:300]}")


def response(content: str, finish: str, tokens_in: int = 10, tokens_out: int = 10):
    return {
        "id": "fake",
        "model": "fake-model",
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }


def clear_usage():
    db.x("DELETE FROM usage_logs")


def upload_markdown(zone_id: str, filename: str, text: str):
    config.ensure_dirs()
    doc_id = db.new_id()
    blob = text.encode("utf-8")
    path = config.UPLOAD_DIR / f"{doc_id}.md"
    path.write_bytes(blob)
    db.x(
        "INSERT INTO documents (id, zone_id, node_id, filename, ext, size, sha256, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, zone_id, None, filename, ".md", len(blob),
         hashlib.sha256(blob).hexdigest(), "parsing", db.now()),
    )
    result = rag.parse_and_index(doc_id)
    return doc_id, result


try:
    db.init_db()
    check("配置 MINDMAP_MAX_TOKENS=4000", config.MINDMAP_MAX_TOKENS == 4000, config.MINDMAP_MAX_TOKENS)

    # B1：首次 length，第二次成功
    llm.get_model_config = lambda: {"base_url": "http://fake", "api_key": "x", "model": "fake-model"}
    calls = []
    queue = [
        response("{\"status\": \"rea", "length", 11, 20),
        response("{\"status\": \"ready\", \"ok\": true}", "stop", 12, 21),
    ]

    def fake_post(messages, *, max_tokens, temperature, timeout=60):
        calls.append(max_tokens)
        return queue.pop(0)

    llm._post = fake_post
    clear_usage()
    obj, usage = llm.chat_json("mindmap", "测试", kind="mindmap", max_tokens=1500, temperature=0.3)
    logs = [dict(row) for row in db.q("SELECT ok, error, prompt_tokens, completion_tokens FROM usage_logs ORDER BY rowid")]
    check("B1 截断自动加倍", calls == [1500, 3000] and obj.get("status") == "ready", calls)
    check("B1 用量记录 1 失败 + 1 成功", len(logs) == 2 and logs[0]["ok"] == 0
          and logs[0]["error"] == "truncated" and logs[1]["ok"] == 1, logs)

    # B2：两次均 length
    calls.clear()
    queue = [response("{", "length", 1, 20), response("{", "length", 1, 20)]
    clear_usage()
    try:
        llm.chat_json("mindmap", "测试", kind="mindmap", max_tokens=1500, temperature=0.3)
        check("B2 两次截断抛错", False, "未抛错")
    except llm.ModelOutputInvalid as exc:
        logs = [dict(row) for row in db.q("SELECT ok, error FROM usage_logs ORDER BY rowid")]
        check("B2 两次截断抛错", "被截断" in exc.message and calls == [1500, 3000], (exc.message, calls))
        check("B2 两次截断用量", len(logs) == 2 and all(row["ok"] == 0 and row["error"] == "truncated" for row in logs), logs)

    # B3：正常 JSON 行为不变
    calls.clear()
    queue = [response("{\"status\": \"ready\"}", "stop")]
    clear_usage()
    obj, usage = llm.chat_json("mindmap", "测试", kind="mindmap", max_tokens=1500, temperature=0.3)
    logs = [dict(row) for row in db.q("SELECT ok, error FROM usage_logs ORDER BY rowid")]
    check("B3 正常 JSON 无回归", calls == [1500] and obj.get("status") == "ready"
          and len(logs) == 1 and logs[0]["ok"] == 1, (calls, logs))

    # 准备可用于结构回退的真实文档与片段
    zone = app.create_zone({"name": "导图回退测试"})
    markdown = (
        "# 第一章 信号分析\n"
        "## 1.1 线性时不变系统\n线性时不变系统满足叠加性与时不变性，可以用卷积描述。\n"
        "## 1.2 卷积性质\n卷积满足交换律、结合律和分配律。\n"
        "## 1.3 系统分类\n系统可以分为线性与非线性、时变与时不变系统。\n"
        "## 1.4 稳定性\n稳定系统对有界输入产生有界输出。\n"
        "## 1.5 因果性\n因果系统的输出不依赖未来输入。\n"
    )
    doc_id, parsed = upload_markdown(zone["id"], "回退资料.md", markdown)
    check("准备可回退资料", parsed.get("status") == "ready", parsed)

    def generate_with_error(exc):
        original = llm.chat_json
        llm.chat_json = lambda *a, **k: (_ for _ in ()).throw(exc)
        try:
            return mindmap.generate(zone["id"], [doc_id], "失败回退导图", "concept"), original
        finally:
            llm.chat_json = original

    # B4：ModelOutputInvalid 回退 rule
    result, _ = generate_with_error(llm.ModelOutputInvalid("model_output_invalid", "输出非法"))
    map_row = db.q1("SELECT generated_by FROM mindmaps WHERE id = ?", (result["mindmap_id"],))
    check("B4 模型输出非法自动回退", result.get("method") == "rule"
          and len(result.get("nodes", [])) > 0 and result.get("warning"), result)
    check("B4 generated_by=rule", map_row["generated_by"] == "rule", dict(map_row) if map_row else None)
    concept_map_id = result["mindmap_id"]

    # B5：请求失败与未配置模型都回退
    result_req, _ = generate_with_error(llm.ModelRequestFailed("model_request_failed", "网络失败"))
    check("B5 模型请求失败自动回退", result_req.get("method") == "rule"
          and len(result_req.get("nodes", [])) > 0 and result_req.get("warning"), result_req)
    result_nc, _ = generate_with_error(llm.ModelNotConfigured("model_not_configured", "未配置模型"))
    check("B5 未配置模型自动回退", result_nc.get("method") == "rule"
          and len(result_nc.get("nodes", [])) > 0 and result_nc.get("warning"), result_nc)

    # B6：insufficient_evidence 不回退
    try:
        generate_with_error(llm.AppError("insufficient_evidence", "资料不足"))
        check("B6 资料不足不回退", False, "未抛错")
    except llm.AppError as exc:
        check("B6 资料不足不回退", exc.code == "insufficient_evidence", exc.message)

    # B8：重新生成失败回退，保留手改节点
    nodes = mindmap.get_map(concept_map_id)["nodes"]
    child = next((node for node in nodes if node.get("parent_key")), None)
    if child:
        child["title"] = "用户手改节点"
    mindmap.save_nodes(concept_map_id, nodes)
    original = llm.chat_json
    llm.chat_json = lambda *a, **k: (_ for _ in ()).throw(llm.ModelOutputInvalid("model_output_invalid", "再次失败"))
    try:
        regenerated = mindmap.regenerate(concept_map_id)
    finally:
        llm.chat_json = original
    map_row = db.q1("SELECT generated_by FROM mindmaps WHERE id = ?", (concept_map_id,))
    check("B8 重新生成失败回退规则版", regenerated.get("method") == "rule"
          and regenerated.get("warning") and map_row["generated_by"] == "rule", regenerated.get("warning"))
    check("B8 保留手改节点", regenerated.get("kept_user_nodes", 0) >= 1, regenerated.get("kept_user_nodes"))

    print(f"\n== {PASS} PASS / {len(FAILS)} FAIL")
    if FAILS:
        print("失败:", FAILS)
        sys.exit(1)
finally:
    shutil.rmtree(TMP, ignore_errors=True)