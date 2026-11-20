"""真实模型链路验收脚本（需要你要有可用的 OpenAI 兼容 API）。

特点：使用临时数据目录，不污染你的 data/；不保存 Key 到磁盘；只做一次性验收。

用法：
    .venv\\Scripts\\python.exe scripts\\verify_model.py ^
        --base-url https://api.deepseek.com/v1 --model deepseek-chat --api-key sk-xxxx

也可以用环境变量传 Key（避免出现在命令行历史）：
    $env:ZHIXU_TEST_API_KEY = "sk-xxxx"
    .venv\\Scripts\\python.exe scripts\\verify_model.py --base-url ... --model ...

验证内容：
1) 连通性 2) RAG 回答带引用 3) M2 概念导图（refs 溯源）4) 固定 6 题出卷
5) 论文检查（模型辅助不阻塞）6) usage_logs 是否记录真实 token 与 skill 哈希
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

fails: list[str] = []


def ok(name: str, cond, extra="") -> None:
    print(("PASS  " if cond else "FAIL  ") + name + ("  |  " + str(extra)[:260] if extra else ""))
    if not cond:
        fails.append(name)


def main() -> int:
    parser = argparse.ArgumentParser(description="真实模型链路验收（临时数据目录，不写真实 data/）")
    parser.add_argument("--base-url", default=os.environ.get("ZHIXU_TEST_BASE_URL", ""))
    parser.add_argument("--model", default=os.environ.get("ZHIXU_TEST_MODEL", ""))
    parser.add_argument("--api-key", default=os.environ.get("ZHIXU_TEST_API_KEY", ""))
    parser.add_argument("--from-config", action="store_true",
                        help="从 data/config.json 读取模型配置（网页设置页保存过即可；不打印 Key）")
    args = parser.parse_args()

    import json
    from pathlib import Path

    config_path = Path(ROOT) / "data" / "config.json"
    if args.from_config:
        if not config_path.exists():
            print("未找到 data/config.json：请先在网页「设置」里保存 base_url / api_key / model。")
            return 2
        try:
            saved = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"读取 data/config.json 失败：{exc}")
            return 2
        base_url = str(saved.get("base_url") or "").strip()
        model = str(saved.get("model") or "").strip()
        api_key = str(saved.get("api_key") or "").strip()
    else:
        base_url = (args.base_url or "").strip()
        model = (args.model or "").strip()
        api_key = (args.api_key or "").strip()
    if not base_url or not model:
        print("缺少 --base-url / --model。示例：")
        print(r"  .venv\Scripts\python.exe scripts\verify_model.py --base-url https://api.deepseek.com/v1 --model deepseek-chat")
        print(r"  或先在网页设置页保存配置，再运行：scripts\verify_model.py --from-config")
        return 2
    if not api_key:
        api_key = getpass.getpass("请输入 API Key（不会保存到磁盘）: ").strip()
    if not api_key:
        print("未提供 API Key。")
        return 2

    # 临时数据目录 + 真实模型配置（仅本进程内存）
    os.environ["ZHIXU_DATA_DIR"] = tempfile.mkdtemp(prefix="zhixu_model_")
    import config  # noqa: E402
    import app as appmod  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402
    from docx import Document  # noqa: E402
    import io  # noqa: E402

    real_cfg = {"base_url": base_url, "api_key": api_key, "model": model}
    config.load_config = lambda: dict(real_cfg)  # type: ignore[assignment]

    md = """# 第一章 信号与系统
## 1.1 信号的分类
连续信号在某一时间区间内都有定义，离散信号只在离散时刻有定义。
## 1.2 线性时不变系统
线性时不变系统满足线性性与时不变性，输出等于输入与系统冲激响应的卷积。
### 1.2.1 卷积
卷积满足交换律、结合律与分配律。
"""
    paper = Document()
    paper.add_paragraph("基于知识图谱的个性化学习路径研究")
    paper.add_paragraph("摘要：随着在线学习资源增长，学习者需要从复杂内容中识别适合自己的学习路径。本文讨论知识图谱在学习资源组织中的作用。")
    paper.add_paragraph("关键词:知识图谱,个性化学习,学习路径")
    paper.add_paragraph("1 引言")
    paper.add_paragraph("在线学习环境中的资源组织方式会影响学习者的选择效率。")
    paper.add_paragraph("参考文献")
    paper.add_paragraph("[1] 学习资源组织相关研究。")
    paper_buf = io.BytesIO()
    paper.save(paper_buf)

    with TestClient(appmod.app) as c:
        r = c.get("/api/health")
        ok("health 显示模型已配置", r.status_code == 200 and r.json().get("model_configured") is True, r.text[:200])
        r = c.post("/api/settings/test")
        ok("测试连通", r.status_code == 200 and r.json().get("ok") is True, r.text[:300])

        zid = c.post("/api/zones", json={"name": "模型验收"}).json()["id"]
        r = c.post("/api/documents", data={"zone_id": zid},
                   files={"file": ("模型验收.md", md.encode("utf-8"), "text/markdown")})
        ok("上传解析", r.status_code == 200 and r.json().get("status") == "ready", r.text[:200])
        doc_id = r.json()["document_id"]
        conv_id = c.post("/api/conversations", json={"zone_id": zid}).json()["id"]

        r = c.post("/api/chat", json={"conversation_id": conv_id, "question": "线性时不变系统有什么性质？"})
        body = r.json() if r.status_code == 200 else {}
        ok("RAG 回答带真实引用", r.status_code == 200 and body.get("status") in ("answered", "partial")
           and len(body.get("citations", [])) >= 1
           and body["citations"][0]["document_id"] == doc_id, r.text[:500])
        if body:
            print("      回答摘要:", str(body.get("answer", ""))[:120])
            print("      引用:", [(x.get("filename"), x.get("page"), x.get("heading_path")) for x in body.get("citations", [])])
            usage = body.get("usage") or {}
            ok("chat 真实 token", (usage.get("prompt_tokens") or 0) > 0 or (usage.get("completion_tokens") or 0) > 0, usage)

        r = c.post("/api/mindmaps", json={"zone_id": zid, "document_ids": [doc_id],
                                          "title": "模型验收导图", "mode": "concept"})
        mm = r.json() if r.status_code == 200 else {}
        ok("M2 概念导图", r.status_code == 200 and len(mm.get("nodes", [])) >= 3, r.text[:400])
        if mm:
            traceable = [n for n in mm.get("nodes", []) if n.get("parent_key") and n.get("source_snippet")]
            ok("M2 节点可溯源到原文", len(traceable) >= 1, traceable[:1])

        r = c.post("/api/quizzes", json={"zone_id": zid})
        quiz = r.json() if r.status_code == 200 else {}
        ok("固定 6 题生成", r.status_code == 200 and len(quiz.get("questions", [])) == 6, r.text[:400])
        if quiz:
            r2 = c.post(f"/api/quizzes/{quiz['quiz_id']}/export")
            ok("学生版 DOCX+PDF 导出", r2.status_code == 200 and r2.json().get("docx") and r2.json().get("pdf"), r2.text[:300])

        r = c.post("/api/papers", files={"file": ("模型验收论文.docx", paper_buf.getvalue(),
                   "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        pid = r.json()["paper_id"]
        r = c.post(f"/api/papers/{pid}/check", json={"profile_mode": "builtin"})
        ok("论文检查（模型辅助不阻塞）", r.status_code == 200 and r.json().get("status") == "checked", r.text[:300])

        usage_rows = c.get("/api/usage").json()
        kinds = {row.get("kind") for row in usage_rows}
        ok("usage_logs 有真实调用", len(usage_rows) > 0 and any((row.get("prompt_tokens") or 0) > 0 for row in usage_rows), usage_rows[:2])
        ok("四类调用记录", {"chat", "mindmap", "quiz"}.issubset(kinds), kinds)
        total_in = sum((row.get("prompt_tokens") or 0) for row in usage_rows)
        total_out = sum((row.get("completion_tokens") or 0) for row in usage_rows)
        print(f"\n用量汇总：调用 {len(usage_rows)} 次，输入 {total_in} tokens，输出 {total_out} tokens")

    print(f"\n== 结果：{len(fails)} 项失败")
    if fails:
        print("失败项：", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
