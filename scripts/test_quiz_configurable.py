"""可配置出题、教师 Skill 与公式安全测试。使用临时数据库和假模型，不调用真实 API。"""
from __future__ import annotations

import os
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ZHIXU_DATA_DIR"] = tempfile.mkdtemp(prefix="zhixu_quiz_config_")

import config  # noqa: E402
import db  # noqa: E402
import llm  # noqa: E402
import quiz  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS = 0
FAILS: list[str] = []


def ok(name: str, condition, extra=""):
    global PASS
    if condition:
        PASS += 1
        print("PASS ", name)
    else:
        FAILS.append(name)
        print("FAIL ", name, "|", str(extra)[:400])


USAGE = {"model": "quiz-test", "prompt_tokens": 1, "completion_tokens": 1,
         "latency_ms": 1, "ok": 1, "error": None, "skill_name": "quiz", "skill_sha256": "test"}
CALLS = []
PROMPTS = []
OUTPUT_TYPES = list(config.QUIZ_TYPES)
INVALID_FIRST = False


def make_output(types, invalid=False):
    questions = []
    for index, qtype in enumerate(types, 1):
        if qtype in ("single_choice", "multiple_choice"):
            options = [{"id": "A", "text": "结果 $x^2$"}, {"id": "B", "text": "结果 $x^3$"}]
            answer = {"correct_option_ids": ["A"] if qtype == "single_choice" else ["A", "B"]}
        elif qtype == "true_false":
            options, answer = [], {"value": True}
        else:
            options, answer = [], {"text": "$a_6=13$"}
        questions.append({
            "type": qtype,
            "stem": f"第{index}题：代入 $x^2+1$，并说明 <b>条件</b>。",
            "options": options,
            "answer": answer,
            "explanation": "使用 $a_n=a_1+(n-1)d$ 复核，结果一致。",
            "citations": ["e1"],
            "latex": ("\\frac{a}{b" if invalid else "a_n=a_1+(n-1)d") if qtype == "calculation" else None,
        })
    return {"status": "ready", "requested_count": len(types), "generated_count": len(types),
            "questions": questions, "missing_evidence": [], "warnings": []}


def fake_chat_json(skill_key, user_content, *, kind, system_extra="", max_tokens, temperature, timeout=60):
    if skill_key != "quiz":
        raise AssertionError(skill_key)
    CALLS.append(1)
    PROMPTS.append(system_extra)
    invalid = INVALID_FIRST and len(CALLS) == 1
    return make_output(OUTPUT_TYPES, invalid=invalid), dict(USAGE)


llm.chat_json = fake_chat_json


def expect_invalid(name, payload, contains=""):
    try:
        quiz.normalize_question_counts(payload)
    except Exception as exc:
        ok(name, getattr(exc, "code", "") == "invalid_request" and contains in str(exc), exc)
    else:
        ok(name, False, "未拒绝非法配置")


def main():
    global OUTPUT_TYPES, INVALID_FIRST
    counts, default_types = quiz.normalize_question_counts(None)
    ok("缺省保持旧版 6 题", default_types == config.QUIZ_TYPES and sum(counts.values()) == 6)
    counts, custom_types = quiz.normalize_question_counts(
        {"single_choice": 2, "multiple_choice": 0, "true_false": 1, "calculation": 2}
    )
    ok("自定义题型顺序", custom_types == ["single_choice", "single_choice", "true_false", "calculation", "calculation"])
    expect_invalid("拒绝 0 题", {"single_choice": 0}, "至少")
    expect_invalid("拒绝未知题型", {"short_answer": 1}, "不支持")
    expect_invalid("拒绝负数", {"calculation": -1}, "0–10")
    expect_invalid("拒绝布尔数量", {"calculation": True}, "整数")
    expect_invalid("拒绝超过总题量", {t: 10 for t in config.QUIZ_CONFIGURABLE_TYPES}, "最多")

    OUTPUT_TYPES = custom_types
    with TestClient(appmod.app) as client:
        zone = client.post("/api/zones", json={"name": "等差数列"}).json()
        md = ("# 等差数列\n等差数列通项公式为 a_n=a_1+(n-1)d，其中 a_1 为首项，d 为公差，n 为正整数。\n"
              "求指定项时，将首项、公差和项数代入公式并复核计算。\n")
        uploaded = client.post(
            "/api/documents", data={"zone_id": zone["id"]},
            files={"file": ("等差数列.md", md.encode("utf-8"), "text/markdown")},
        )
        ok("准备真实证据", uploaded.status_code == 200, uploaded.text[:200])
        result = client.post("/api/quizzes", json={
            "zone_id": zone["id"],
            "question_counts": {"single_choice": 2, "multiple_choice": 0, "true_false": 1, "calculation": 2},
        })
        ok("自定义题量 API", result.status_code == 200 and len(result.json().get("questions", [])) == 5,
           result.text[:400])
        body = result.json()
        ok("响应题量配置", body.get("question_counts", {}).get("calculation") == 2
           and body["question_counts"]["multiple_choice"] == 0)
        ok("教师 Skill 元数据", body.get("teacher_skill", {}).get("name") == "experienced-teacher-examiner")
        ok("风格 Skill 元数据", body.get("style_skill", {}).get("name") == "bupt-exam-style"
           and len(body.get("style_skill", {}).get("sha256", "")) == 12)
        ok("教师 Skill 进入提示词", PROMPTS and "经验丰富教师命题" in PROMPTS[-1])
        ok("风格 Skill 进入提示词", PROMPTS and "北邮本科期中/期末命题风格" in PROMPTS[-1])
        ok("公式校验状态", body.get("formula_check") == "passed")
        ok("题目类型严格匹配", [q["type"] for q in body["questions"]] == custom_types)
        ok("主公式 MathML 预览", any(q.get("mathml", "").startswith("<math") for q in body["questions"]))
        ok("行内公式 MathML 预览", any("<math" in q.get("stem_html", "") for q in body["questions"]))
        ok("普通 HTML 被转义", all("<b>条件</b>" not in q.get("stem_html", "") for q in body["questions"])
           and any("&lt;b&gt;条件&lt;/b&gt;" in q.get("stem_html", "") for q in body["questions"]))

        quiz_id = body["quiz_id"]
        docx_path = Path(os.environ["ZHIXU_DATA_DIR"]) / "formula-test.docx"
        quiz._build_student_docx("高等数学", body["questions"], str(docx_path))
        with zipfile.ZipFile(docx_path) as archive:
            xml = archive.read("word/document.xml").decode("utf-8")
        ok("DOCX 写入原生 OMML", "m:oMath" in xml)
        ok("DOCX 无公式失败降级文本", "公式无法转换" not in xml and "LaTeX 源码" not in xml)

        OUTPUT_TYPES = ["calculation"]
        INVALID_FIRST = True
        CALLS.clear()
        retry = client.post("/api/quizzes", json={
            "zone_id": zone["id"], "question_counts": {"calculation": 1},
        })
        ok("错误公式自动修正", retry.status_code == 200 and len(CALLS) == 2, retry.text[:400])
        ok("修正后公式有效", retry.json().get("formula_check") == "passed")

        INVALID_FIRST = False
        try:
            quiz._validate_latex("\\frac{a}{b", "测试")
        except Exception as exc:
            ok("拒绝不配对花括号", getattr(exc, "code", "") == "model_output_invalid", exc)
        else:
            ok("拒绝不配对花括号", False)
        try:
            quiz._validate_text_formulas("结果 $x^2", "测试")
        except Exception as exc:
            ok("拒绝不配对定界符", getattr(exc, "code", "") == "model_output_invalid", exc)
        else:
            ok("拒绝不配对定界符", False)

    frontend = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    ok("前端题量控件", 'data-quiz-count="' in frontend and "question_counts" in frontend)
    ok("前端公式预览", "formula-display" in frontend and "question.mathml" in frontend)
    print(f"\n== {PASS} PASS / {len(FAILS)} FAIL")
    if FAILS:
        print("失败项：", FAILS)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
