"""自动化冒烟验收（对应规格第 19 节可自动化的部分）。

用临时 DATA_DIR + TestClient + 假模型函数，不触碰真实 data/ 数据，也不调用真实模型 API。

用法：
    .venv\\Scripts\\python.exe scripts\\smoke.py
退出码 0 = 全部通过；非 0 = 有失败项。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ["ZHIXU_DATA_DIR"] = tempfile.mkdtemp(prefix="zhixu_smoke_")

import config  # noqa: E402
import llm  # noqa: E402
import officedoc  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from docx import Document  # noqa: E402

PASS = 0
FAILS: list[str] = []


def ok(name: str, cond, extra="") -> None:
    global PASS
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAILS.append(name)
        print(f"FAIL  {name}  |  {str(extra)[:300]}")


USAGE = {"model": "smoke-fake", "prompt_tokens": 11, "completion_tokens": 7, "latency_ms": 5,
         "ok": 1, "error": None, "skill_name": "x", "skill_sha256": "0123456789ab"}


def fake_chat_json(skill_key, user_content, *, kind, system_extra="", max_tokens, temperature, timeout=60):
    u = dict(USAGE)
    u["skill_name"] = skill_key
    if skill_key == "rag":
        if not json.loads(system_extra.rsplit("\n", 1)[-1])["evidence"]:
            return {"status": "general_knowledge", "answer": "导数可以理解为瞬时变化率。",
                    "citations": [], "missing_evidence": [], "stop_reason": "no_new_evidence"}, u
        return {"status": "answered", "answer": "线性时不变系统可以用卷积描述。[e1]",
                "citations": ["e1"], "missing_evidence": [], "stop_reason": "evidence_sufficient"}, u
    if skill_key == "mindmap":
        return {"status": "ready", "title": "概念导图", "nodes": [
            {"id": "n1", "parent_id": None, "title": "信号与系统", "note": "总览", "refs": [], "kind": "concept", "latex": None},
            {"id": "n2", "parent_id": "n1", "title": "线性时不变系统", "note": "卷积", "refs": ["e1"], "kind": "concept", "latex": None},
            {"id": "n3", "parent_id": "n1", "title": "卷积性质", "note": "交换律", "refs": ["e99"], "kind": "concept", "latex": None},
        ], "missing": []}, u
    if skill_key == "quiz":
        questions = []
        for i, t in enumerate(config.QUIZ_TYPES):
            if t in ("single_choice", "multiple_choice"):
                options = [{"id": "o1", "text": "选项甲"}, {"id": "o2", "text": "选项乙"}]
                answer = {"correct_option_ids": ["o1"]} if t == "single_choice" else {"correct_option_ids": ["o1", "o2"]}
            elif t == "true_false":
                options, answer = [], {"value": True}
            else:
                options, answer = [], {"text": "答案要点"}
            questions.append({"type": t, "stem": f"第{i + 1}题：线性时不变系统的性质是什么？",
                              "options": options, "answer": answer,
                              "explanation": "解析：参考资料中的卷积描述。", "citations": ["e1"],
                              "latex": "a_n=a_1+(n-1)d" if t == "calculation" else None})
        return {"status": "ready", "requested_count": 6, "generated_count": 6,
                "questions": questions, "missing_evidence": [], "warnings": []}, u
    if skill_key == "paper":
        return {"status": "no_changes", "operations": [], "issues": [], "needs_review": [],
                "unverified": [], "checked_ids": []}, u
    raise AssertionError(skill_key)


llm.chat_json = fake_chat_json
for _module_name in ("mindmap", "quiz", "paper", "app"):
    _module = sys.modules.get(_module_name)
    if _module is not None and hasattr(_module, "chat_json"):
        _module.chat_json = fake_chat_json

MD = """# 第一章 信号与系统
## 1.1 线性时不变系统
线性时不变系统可以用卷积描述，输出等于输入与系统冲激响应的卷积。
### 1.1.1 卷积性质
卷积满足交换律、结合律与分配律。
"""

PAPER = Document()
PAPER.add_paragraph("基于知识图谱的个性化学习路径研究")
PAPER.add_paragraph("摘要：随着在线学习资源增长，学习者需要从复杂内容中识别适合自己的学习路径。本文讨论知识图谱在学习资源组织中的作用。")
PAPER.add_paragraph("关键词:知识图谱,个性化学习,学习路径")
PAPER.add_paragraph("1 引言")
PAPER.add_paragraph("在线学习环境中的资源组织方式会影响学习者的选择效率。")
PAPER.add_paragraph("参考文献")
PAPER.add_paragraph("[1] 学习资源组织相关研究。")
PAPER_BYTES = io.BytesIO()
PAPER.save(PAPER_BYTES)

print("== 预热 PDF 引擎（首次会尝试 Word，超时后回退 WPS，可能耗时约 45s）")
_t = time.time()
_sel = officedoc.selftest()
print(f"   selftest -> {_sel.get('engine')} / ok={_sel.get('ok')} ({time.time() - _t:.1f}s)")
ok("PDF 引擎自检", _sel.get("ok") is True and _sel.get("engine") in ("word", "wps"), _sel)

with TestClient(appmod.app) as c:
    # 基础与空库
    r = c.get("/api/health")
    ok("health", r.status_code == 200 and r.json().get("ok") is True and r.json().get("fts5") is True, r.text[:200])
    r = c.get("/api/zones")
    ok("首次启动空知识区", r.status_code == 200 and r.json() == [], r.text[:200])
    r = c.get("/")
    ok("首页可访问", r.status_code == 200 and "知序" in r.text, len(r.text))

    # 知识区
    z = c.post("/api/zones", json={"name": "信号"})
    ok("创建知识区", z.status_code == 200, z.text[:200])
    zid = z.json()["id"]
    r = c.post("/api/zones", json={"name": "信号"})
    ok("重名 409", r.status_code == 409 and r.json()["error"]["code"] == "zone_duplicate", r.text[:200])

    # 上传与解析
    r = c.post("/api/documents", data={"zone_id": zid},
               files={"file": ("信号笔记.md", MD.encode("utf-8"), "text/markdown")})
    ok("上传解析", r.status_code == 200 and r.json().get("status") == "ready", r.text[:200])
    ok("上传质量字段", r.status_code == 200 and r.json().get("quality", 0) >= 0.85
       and "quality_note" in r.json(), r.text[:200])
    doc_id = r.json()["document_id"]
    r = c.get("/api/documents", params={"zone_id": zid})
    ok("文档列表 chunk_count>0", r.status_code == 200 and (r.json()[0].get("chunk_count") or 0) > 0, r.text[:200])
    ok("文档列表质量字段", r.status_code == 200 and "quality" in r.json()[0]
       and "quality_note" in r.json()[0], r.text[:200])

    # 会话与 RAG
    empty_zone = c.post("/api/zones", json={"name": "空知识区"}).json()["id"]
    empty_conv = c.post("/api/conversations", json={"zone_id": empty_zone}).json()["id"]
    r = c.post("/api/chat", json={"conversation_id": empty_conv, "question": "导数是什么？"})
    body = r.json()
    ok("空知识区可通用讲解", r.status_code == 200 and body.get("status") == "general_knowledge"
       and "以下为通用讲解" in body.get("answer", "") and "瞬时变化率" in body.get("answer", "")
       and body.get("retrieved") == 0 and body.get("citations") == [], r.text[:300])
    messages = c.get(f"/api/conversations/{empty_conv}/messages").json()
    ok("重新加载保留通用讲解", messages[-1]["status"] == "general_knowledge"
       and messages[-1]["content"] == body.get("answer") and messages[-1]["citations"] == [], messages[-1])
    conv_id = c.post("/api/conversations", json={"zone_id": zid}).json()["id"]
    r = c.post("/api/chat", json={"conversation_id": conv_id, "question": "zzzzqqqq"})
    ok("已有资料但零命中仍可讲解", r.status_code == 200 and r.json().get("status") == "general_knowledge"
       and r.json().get("retrieved") == 0 and r.json().get("citations") == [], r.text[:300])
    r = c.post("/api/chat", json={"conversation_id": conv_id, "question": "线性时不变系统有什么性质"})
    body = r.json() if r.status_code == 200 else {}
    ok("带引用回答", r.status_code == 200 and body.get("status") == "answered"
       and len(body.get("citations", [])) == 1 and body["citations"][0]["document_id"] == doc_id, r.text[:300])

    # 导图 M1 / M2
    r = c.post("/api/mindmaps", json={"zone_id": zid, "document_ids": [doc_id], "title": "结构导图", "mode": "structure"})
    ok("M1 结构导图", r.status_code == 200 and len(r.json().get("nodes", [])) >= 2, r.text[:300])
    m1 = r.json()
    r = c.post("/api/mindmaps", json={"zone_id": zid, "document_ids": [doc_id], "title": "概念导图", "mode": "concept"})
    ok("M2 概念导图", r.status_code == 200 and len(r.json().get("nodes", [])) >= 3, r.text[:300])
    m2 = r.json() if r.status_code == 200 else {"nodes": []}
    ok("M2 非法 refs 标 unverified", any(n.get("unverified") == 1 for n in m2.get("nodes", [])), m2.get("nodes"))
    r = c.get(f"/api/mindmaps/{m2['mindmap_id']}")
    ok("M2 节点带原文溯源", r.status_code == 200
       and any(n.get("source_snippet") for n in r.json().get("nodes", [])), r.text[:300])
    nodes = m2.get("nodes", [])
    child = [n for n in nodes if n.get("parent_key") is not None]
    if child:
        child[0]["title"] = "用户改过的节点"
        r = c.put(f"/api/mindmaps/{m2['mindmap_id']}", json={"nodes": nodes})
        ok("导图保存手改", r.status_code == 200, r.text[:200])
        r = c.post(f"/api/mindmaps/{m2['mindmap_id']}/regenerate")
        ok("重新生成保留手改", r.status_code == 200 and r.json().get("kept_user_nodes", 0) >= 1, r.text[:300])

    # 出题 + 导出
    r = c.post("/api/quizzes", json={"zone_id": zid})
    ok("固定 6 题", r.status_code == 200 and len(r.json().get("questions", [])) == 6, r.text[:300])
    quiz_id = r.json()["quiz_id"]
    ok("题型顺序", [q["type"] for q in r.json()["questions"]] == config.QUIZ_TYPES, r.json()["questions"])
    r = c.post(f"/api/quizzes/{quiz_id}/export")
    ok("学生版导出 DOCX+PDF", r.status_code == 200 and r.json().get("docx") and r.json().get("pdf"), r.text[:300])
    if r.status_code == 200:
        d = c.get(r.json()["docx"]); p = c.get(r.json()["pdf"])
        ok("导出文件可下载", d.status_code == 200 and p.status_code == 200 and p.content[:4] == b"%PDF", (len(d.content), p.content[:8]))
        student = Document(io.BytesIO(d.content))
        text = "\n".join(par.text for par in student.paragraphs)
        ok("学生版无答案解析来源", ("解析" not in text) and ("来源" not in text) and ("答案" not in text), text[:120])

    # 论文
    r = c.post("/api/papers", files={"file": ("论文.docx", PAPER_BYTES.getvalue(),
               "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    ok("论文上传", r.status_code == 200, r.text[:200])
    pid = r.json()["paper_id"]
    r = c.post(f"/api/papers/{pid}/check", json={"profile_mode": "builtin"})
    ok("论文检查出问题", r.status_code == 200 and r.json().get("status") == "checked"
       and len(r.json().get("issues", [])) > 0, r.text[:300])
    r = c.post(f"/api/papers/{pid}/execute")
    body = r.json() if r.status_code == 200 else {}
    ok("论文执行修正", r.status_code == 200 and body.get("status") == "corrected"
       and body.get("corrected_docx") and body.get("report_pdf"), r.text[:300])
    if body:
        d = c.get(body["corrected_docx"]); rep = c.get(body["report_pdf"])
        ok("修正版与报告可下载", d.status_code == 200 and rep.status_code == 200 and rep.content[:4] == b"%PDF",
           (d.status_code, rep.status_code))
        corrected = Document(io.BytesIO(d.content))
        original_text = "".join(p.text for p in PAPER.paragraphs).replace(" ", "")
        corrected_text = "".join(p.text for p in corrected.paragraphs).replace(" ", "")
        ok("修正版保留原文", original_text == corrected_text, (original_text[:60], corrected_text[:60]))
        ok("原稿保留", len(list(config.UPLOAD_DIR.glob("paper-*.docx"))) >= 1, "")

    # 用量与清空
    r = c.get("/api/usage")
    ok("用量接口", r.status_code == 200 and isinstance(r.json(), list), r.text[:120])
    r = c.post("/api/data/clear", json={"confirm": "CLEAR"})
    ok("清空数据", r.status_code == 200 and r.json().get("ok") is True, r.text[:200])
    r = c.get("/api/zones")
    ok("清空后为空", r.status_code == 200 and r.json() == [], r.text[:200])

print(f"\n== 结果：{PASS} PASS / {len(FAILS)} FAIL")
if FAILS:
    print("失败项：", FAILS)
sys.exit(1 if FAILS else 0)
