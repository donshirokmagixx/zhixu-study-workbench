"""课堂录音回忆摘要与笔记内嵌编辑的静态/接口测试。使用临时数据库和假模型。"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["ZHIXU_DATA_DIR"] = tempfile.mkdtemp(prefix="zhixu_note_recap_")

import config  # noqa: E402
import llm  # noqa: E402
import notes  # noqa: E402
import app as appmod  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

PASS = 0
FAILS: list[str] = []
BAD_OUTPUT = False
PROMPTS: list[str] = []


def ok(name, condition, extra=""):
    global PASS
    if condition:
        PASS += 1
        print("PASS ", name)
    else:
        FAILS.append(name)
        print("FAIL ", name, "|", str(extra)[:400])


USAGE = {"model": "recap-test", "prompt_tokens": 1, "completion_tokens": 1,
         "latency_ms": 1, "ok": 1, "error": None, "skill_name": "lecture_recap",
         "skill_sha256": "test"}


def fake_chat_json(skill_key, user_content, *, kind, system_extra="", max_tokens, temperature, timeout=60):
    assert skill_key == "lecture_recap" and kind == "lecture_recap"
    PROMPTS.append(user_content)
    if BAD_OUTPUT:
        return {"overview": "", "sections": "bad", "action_items": [], "questions": []}, dict(USAGE)
    return {
        "overview": "本节围绕卷积定理展开，老师先复习系统响应的定义，再说明交换律的图形含义，并用例题连接频谱与抽样。",
        "sections": [
            {
                "title": "卷积与系统响应",
                "summary": "老师从冲激响应出发说明零状态响应，强调卷积积分描述的是输入对系统响应的叠加，并复习了交换律的图形意义。",
                "key_points": ["卷积满足交换律", "零状态响应由输入与冲激响应卷积得到"],
            },
            {
                "title": "课堂例题与复习任务",
                "summary": "通过分段积分例题说明计算顺序，提醒学生先判断波形复杂度再决定翻转对象，并布置课后练习。",
                "key_points": ["优先将简单波形作为冲激响应", "注意分段区间和单位"],
            },
        ],
        "action_items": ["复习课件第 2 章例题", "完成课后第 5 题"],
        "questions": ["抽样频率与带宽的具体关系需要再确认"],
    }, dict(USAGE)


llm.chat_json = fake_chat_json


def main():
    global BAD_OUTPUT
    result = notes.summarize_transcript({
        "title": "卷积定理课堂",
        "transcript": "今天讲卷积定理。卷积满足交换律。做题时把简单波形当作冲激响应。课后完成第5题。",
    })
    ok("摘要接口返回结构", isinstance(result.get("overview"), str) and len(result["overview"]) > 10
       and len(result["sections"]) == 2 and len(result["sections"][0]["key_points"]) == 2
       and len(result["action_items"]) == 2)
    ok("分节标题与摘要", result["sections"][1]["title"] == "课堂例题与复习任务"
       and len(result["sections"][1]["summary"]) > 10)
    ok("待确认问题保留", len(result.get("questions", [])) == 1)
    ok("转写作为数据传入", PROMPTS and "今天讲卷积定理" in PROMPTS[-1])

    try:
        notes.summarize_transcript({"title": "超长", "transcript": "长" * (config.NOTE_TRANSCRIPT_MAX_CHARS + 1)})
    except llm.AppError as exc:
        ok("拒绝超长转写", exc.code == "invalid_request", exc)
    else:
        ok("拒绝超长转写", False)

    try:
        notes.summarize_transcript({"title": "短", "transcript": "太短"})
    except llm.AppError as exc:
        ok("拒绝过短转写", exc.code == "invalid_request", exc)
    else:
        ok("拒绝过短转写", False)

    BAD_OUTPUT = True
    try:
        notes.summarize_transcript({
            "title": "坏输出",
            "transcript": "这是一段足够长的课堂录音转写，用于验证模型输出校验功能。",
        })
    except llm.ModelOutputInvalid as exc:
        ok("拒绝非法摘要输出", "格式无效" in str(exc), exc)
    else:
        ok("拒绝非法摘要输出", False)
    BAD_OUTPUT = False

    with TestClient(appmod.app) as client:
        response = client.post("/api/notes/summarize", json={
            "title": "课堂摘要测试",
            "transcript": "老师讲解了系统函数、收敛域和稳定性判断。课后复习第二章。",
        })
        body = response.json() if response.status_code == 200 else {}
        ok("摘要 API 200", response.status_code == 200 and body.get("overview")
           and len(body.get("sections", [])) == 2, response.text[:300])
        response = client.post("/api/notes/summarize", json={"title": "短", "transcript": "短"})
        ok("摘要 API 参数校验", response.status_code == 400
           and response.json().get("error", {}).get("code") == "invalid_request", response.text[:300])

    frontend = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    ok("正文 contenteditable", 'data-scholar-body="true"' in frontend and "contenteditable" in frontend)
    ok("实时语音识别", "webkitSpeechRecognition" in frontend and "recognition.lang = 'zh-CN'" in frontend)
    ok("课堂摘要前端调用", "apiPost('/notes/summarize'" in frontend and "scholarInsertRecap" in frontend)
    ok("分节摘要渲染", "recap.sections" in frontend and "scholar-recap-section" in frontend)
    ok("摘要自动写入右侧笔记", "scholarInsertRecap(true)" in frontend and "整课回忆摘要" in frontend)
    ok("长笔记容量", 'maxlength="60000"' in frontend and config.NOTE_BODY_MAX_CHARS == 60000)
    ok("录音面板样式", ".scholar-live" in frontend and ".scholar-recap" in frontend)
    ok("摘要 Skill 可加载", len(llm.load_skill("lecture_recap")[1]) == 12)
    print(f"\n== {PASS} PASS / {len(FAILS)} FAIL")
    if FAILS:
        print("失败项：", FAILS)
        raise SystemExit(1)


if __name__ == "__main__":
    main()