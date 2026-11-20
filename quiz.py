"""出题成卷：教师命题 Skill、可配置题型题量、公式校验、学生版 DOCX + PDF。

对外函数见 docs/模块接口约定.md：generate / get_quiz / export_student。
"""
from __future__ import annotations

import html
import json
import re

import config
import db
import llm
from llm import AppError, ModelOutputInvalid

def _insufficient(total: int) -> str:
    return f"当前知识区的资料不足以生成 {total} 道题。"
_ALLOWED_MODEL_STATUS = {"ready", "partial", "insufficient_evidence", "blocked_scope"}
_TYPE_LABELS = {
    "single_choice": "单选题",
    "multiple_choice": "多选题",
    "true_false": "判断题",
    "fill_blank": "填空题",
    "short_answer": "简答题",
    "calculation": "计算题",
}
_OPTION_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


# ---------------------------------------------------------------------------
# 数据访问小工具
# ---------------------------------------------------------------------------

def _require_zone(zone_id: str):
    row = db.q1("SELECT id, name FROM zones WHERE id = ?", (zone_id,))
    if row is None:
        raise AppError("zone_not_found", "知识区不存在。")
    return row


def _require_mindmap(mindmap_id: str, zone_id: str):
    row = db.q1("SELECT id, zone_id, title FROM mindmaps WHERE id = ?", (mindmap_id,))
    if row is None:
        raise AppError("invalid_request", "导图不存在。")
    if row["zone_id"] != zone_id:
        raise AppError("invalid_request", "导图不属于该知识区。")
    return row


def _json_list(raw) -> list:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return data if isinstance(data, list) else []


def normalize_question_counts(raw) -> tuple[dict[str, int], list[str]]:
    """校验用户题量配置，返回 (各题型数量, 期望出题顺序)；缺省保持旧版固定 6 题。"""
    if raw is None:
        counts = {qtype: config.QUIZ_TYPES.count(qtype) for qtype in dict.fromkeys(config.QUIZ_TYPES)}
        return counts, list(config.QUIZ_TYPES)
    if not isinstance(raw, dict):
        raise AppError("invalid_request", "question_counts 必须是按题型填写数量的对象。")
    allowed = list(config.QUIZ_CONFIGURABLE_TYPES)
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise AppError("invalid_request", "不支持的题型：" + "、".join(unknown))
    counts: dict[str, int] = {}
    for qtype in allowed:
        value = raw.get(qtype, 0)
        if isinstance(value, bool) or not isinstance(value, int):
            raise AppError("invalid_request", f"{_TYPE_LABELS[qtype]}数量必须是整数。")
        if value < 0 or value > config.QUIZ_MAX_PER_TYPE:
            raise AppError(
                "invalid_request",
                f"{_TYPE_LABELS[qtype]}数量应在 0–{config.QUIZ_MAX_PER_TYPE} 之间。",
            )
        counts[qtype] = value
    total = sum(counts.values())
    if total < 1:
        raise AppError("invalid_request", "请至少选择一种题型并设置 1 道题。")
    if total > config.QUIZ_MAX_TOTAL:
        raise AppError("invalid_request", f"单次最多生成 {config.QUIZ_MAX_TOTAL} 道题，请减少题量。")
    expected_types = [qtype for qtype in allowed for _ in range(counts[qtype])]
    return counts, expected_types


def _normalize_math_wrappers(text: str) -> str:
    """统一模型常见的公式定界符，便于后续 MathML/OMML 转换。"""
    value = str(text or "").strip()
    value = re.sub(r"\$\s*\$([^$]+?)\$\s*\$", r"$\1$", value, flags=re.S)
    value = re.sub(r"\\\((.+?)\\\)", r"$\1$", value, flags=re.S)
    value = re.sub(r"\\\[(.+?)\\\]", r"$\1$", value, flags=re.S)
    return value


# ---------------------------------------------------------------------------
# 模型调用与校验
# ---------------------------------------------------------------------------

def _evidence_clue(mindmap_id: str | None) -> list[str]:
    if not mindmap_id:
        return []
    rows = db.q(
        "SELECT title, unverified, source_chunk_ids FROM mindmap_nodes"
        " WHERE mindmap_id = ? ORDER BY rowid",
        (mindmap_id,),
    )
    titles = []
    for row in rows:
        if int(row["unverified"] or 0) == 0 and _json_list(row["source_chunk_ids"]):
            titles.append(str(row["title"]))
    return titles


def _citation_from(cid: str, mapping: dict) -> dict:
    import rag
    item = mapping[cid]
    return {
        "document_id": item.get("document_id", ""),
        "filename": item.get("filename", ""),
        "page": item.get("page"),
        "heading_path": item.get("heading_path") or "",
        "snippet": rag.snippet(item.get("text") or "", 200),
    }


def _require_text(value, field: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise ModelOutputInvalid("model_output_invalid", f"题目字段 {field} 不能为空。")


def _normalize_options(raw) -> tuple[list[str], list[str]] | None:
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    ids: list[str] = []
    texts: list[str] = []
    for index, option in enumerate(raw):
        if isinstance(option, dict):
            text = option.get("text")
            if not isinstance(text, str) or not text.strip():
                return None
            texts.append(text.strip())
            option_id = option.get("id")
            ids.append(str(option_id).strip() if option_id is not None else _OPTION_LETTERS[index])
        elif isinstance(option, str) and option.strip():
            texts.append(option.strip())
            ids.append(_OPTION_LETTERS[index])
        else:
            return None
    if len(set(ids)) != len(ids):
        return None
    return ids, texts


def _normalize_choice_answer(raw_answer, ids: list[str], texts: list[str]) -> list[int] | None:
    raw = raw_answer
    if isinstance(raw, dict):
        for key in ("correct_option_ids", "correct", "option_ids", "ids", "answers"):
            if key in raw:
                raw = raw[key]
                break
        else:
            if "text" in raw:
                raw = raw["text"]
            else:
                return None
    if isinstance(raw, (str, int)) and not isinstance(raw, bool):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None
    upper_ids = [item.upper() for item in ids]
    picked: list[int] = []
    for item in raw:
        found = None
        if isinstance(item, bool):
            return None
        if isinstance(item, int):
            if 0 <= item < len(texts):
                found = item
        else:
            text = str(item).strip()
            if text in ids:
                found = ids.index(text)
            elif text.upper() in upper_ids:
                found = upper_ids.index(text.upper())
            elif text in texts:
                found = texts.index(text)
        if found is None:
            return None
        if found not in picked:
            picked.append(found)
    return sorted(picked)


def _normalize_true_false_answer(raw_answer) -> str | None:
    raw = raw_answer
    if isinstance(raw, dict):
        for key in ("text", "value", "answer"):
            if key in raw:
                raw = raw[key]
                break
        else:
            return None
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, str):
        text = raw.strip().lower()
        if text in ("true", "false"):
            return text
    return None


def _normalize_text_answer(raw_answer) -> str:
    if isinstance(raw_answer, bool):
        return ""
    if isinstance(raw_answer, str):
        return raw_answer.strip()
    if isinstance(raw_answer, (int, float)):
        return str(raw_answer)
    if isinstance(raw_answer, list):
        parts = [_normalize_text_answer(item) for item in raw_answer]
        return "；".join(part for part in parts if part)
    if isinstance(raw_answer, dict):
        for key in ("text", "value", "answer", "answers", "texts"):
            if key in raw_answer:
                text = _normalize_text_answer(raw_answer[key])
                if text:
                    return text
    return ""


def _latex_to_mathml(latex: str) -> str:
    """把 LaTeX 转成可嵌入页面的 MathML，并拦截非数学 HTML 内容。"""
    from latex2mathml.converter import convert as latex_to_mathml

    result = latex_to_mathml(latex)
    if isinstance(result, bytes):
        result = result.decode("utf-8")
    mathml = str(result or "").strip()
    if not mathml.startswith("<math"):
        raise ValueError("转换结果不是 MathML")
    if re.search(r"<\s*(?:script|iframe|foreignObject)|on\w+\s*=|javascript:", mathml, re.I):
        raise ValueError("MathML 含不安全内容")
    return mathml


def _validate_latex(latex: str, field: str) -> str:
    value = _normalize_math_wrappers(latex)
    if len(value) >= 2 and value.startswith("$") and value.endswith("$"):
        value = value[1:-1].strip()
    if not value:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 公式为空。")
    if len(value) > 2000:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 公式过长。")
    if "�" in value or "\x00" in value:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 公式含乱码或非法控制字符。")
    if "$" in value:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 公式定界符不完整。")
    depth = 0
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth < 0:
                break
    if depth != 0:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 公式花括号不配对。")
    environments = re.findall(r"\\(begin|end)\{([^}]+)\}", value)
    stack: list[str] = []
    for action, name in environments:
        if action == "begin":
            stack.append(name)
        elif not stack or stack.pop() != name:
            raise ModelOutputInvalid("model_output_invalid", f"{field} 公式环境不配对。")
    if stack:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 公式环境不配对。")
    try:
        _latex_to_mathml(value)
    except Exception as exc:
        raise ModelOutputInvalid(
            "model_output_invalid", f"{field} 公式无法转换为 MathML：{value[:120]}"
        ) from exc
    return value


def _validate_text_formulas(text: str, field: str) -> str:
    value = _normalize_math_wrappers(text)
    if "�" in value:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 含乱码替代字符。")
    if len(re.findall(r"(?<!\\)\$", value)) % 2:
        raise ModelOutputInvalid("model_output_invalid", f"{field} 的 $ 公式定界符不配对。")
    parts = re.split(r"(\$[^$]+\$)", value)
    for part in parts:
        if part.startswith("$") and part.endswith("$") and len(part) >= 3:
            _validate_latex(part[1:-1], field)
        elif re.search(r"\\(?:frac|sqrt|sum|int|alpha|beta|gamma|theta|lambda|pi|times|cdot)\b", part):
            raise ModelOutputInvalid("model_output_invalid", f"{field} 中的 LaTeX 命令必须使用 $...$ 包裹。")
    return value


def _validate_question_formulas(question: dict) -> dict:
    idx = question["idx"]
    for field in ("stem", "answer", "explanation"):
        question[field] = _validate_text_formulas(str(question[field]), f"第 {idx} 题 {field}")
    question["options"] = [
        _validate_text_formulas(str(option), f"第 {idx} 题选项 {chr(65 + i)}")
        for i, option in enumerate(question["options"])
    ]
    if question.get("latex"):
        question["latex"] = _validate_latex(question["latex"], f"第 {idx} 题 latex")
    return question


def _safe_mathml(latex) -> str:
    if not latex:
        return ""
    try:
        return _latex_to_mathml(str(latex))
    except Exception:
        return ""


def _rich_text_html(text) -> str:
    """服务端生成安全的文本 + MathML；普通文本始终转义，旧数据公式失败时保留源码。"""
    value = _normalize_math_wrappers(str(text or ""))
    output = []
    for part in re.split(r"(\$[^$]+\$)", value):
        if not part:
            continue
        if part.startswith("$") and part.endswith("$") and len(part) >= 3:
            mathml = _safe_mathml(part[1:-1])
            output.append('<span class="math-inline">' + mathml + "</span>" if mathml else html.escape(part))
        else:
            output.append(html.escape(part))
    return "".join(output)


def _extract_latex(question: dict) -> str | None:
    latex = question.get("latex")
    if latex is None:
        formulas = question.get("formulas")
        if isinstance(formulas, list) and formulas:
            if len(formulas) > 1:
                raise ModelOutputInvalid("model_output_invalid", "每题最多提供一个主公式，其余公式写入行内文本。")
            first = formulas[0]
            if isinstance(first, dict):
                latex = first.get("latex")
    if latex is None:
        return None
    if not isinstance(latex, str):
        raise ModelOutputInvalid("model_output_invalid", "题目 latex 字段不是字符串。")
    latex = _normalize_math_wrappers(latex)
    if len(latex) >= 2 and latex.startswith("$") and latex.endswith("$"):
        latex = latex[1:-1].strip()
    return latex or None


def _validate_question(question: dict, expected_type: str, mapping: dict, idx: int) -> dict:
    if not isinstance(question, dict):
        raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题不是对象。")
    qtype = question.get("type")
    if qtype != expected_type:
        raise ModelOutputInvalid(
            "model_output_invalid", f"第 {idx} 题题型应为 {expected_type}，实际 {qtype!r}。"
        )
    stem = _require_text(question.get("stem"), "stem")
    explanation = _require_text(question.get("explanation"), "explanation")

    raw_citations = question.get("citations")
    if not isinstance(raw_citations, list) or not raw_citations:
        raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题 citations 不能为空。")
    citations = []
    seen = set()
    for cid in raw_citations:
        if not isinstance(cid, str) or cid not in mapping:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题引用了无效 evidence_id：{cid!r}。")
        if cid in seen:
            continue
        seen.add(cid)
        citations.append(_citation_from(cid, mapping))
    if not citations:
        raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题没有有效引用。")

    if qtype in ("single_choice", "multiple_choice"):
        normalized_options = _normalize_options(question.get("options"))
        if normalized_options is None:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题选项非法。")
        ids, texts = normalized_options
        picked = _normalize_choice_answer(question.get("answer"), ids, texts)
        if not picked:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题答案与选项不匹配。")
        if qtype == "single_choice" and len(picked) != 1:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题单选必须恰有 1 个正确选项。")
        if qtype == "multiple_choice" and len(picked) < 2:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题多选至少 2 个正确选项。")
        answer = "；".join(texts[index] for index in picked)
        options = texts
    elif qtype == "true_false":
        answer = _normalize_true_false_answer(question.get("answer"))
        if answer is None:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题判断题答案必须是 true/false。")
        options = []
    elif qtype == "fill_blank":
        answer = _normalize_text_answer(question.get("answer"))
        if not answer:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题填空答案为空。")
        options = []
    else:  # short_answer / calculation
        answer = _normalize_text_answer(question.get("answer"))
        if not answer:
            raise ModelOutputInvalid("model_output_invalid", f"第 {idx} 题答案为空。")
        options = []

    normalized = {
        "idx": idx,
        "type": qtype,
        "stem": stem,
        "options": options,
        "answer": answer,
        "explanation": explanation,
        "citations": citations,
        "latex": _extract_latex(question),
    }
    return _validate_question_formulas(normalized)


def _validate_quiz_output(obj: dict, mapping: dict, expected_types: list[str]) -> list[dict]:
    if not isinstance(obj, dict):
        raise ModelOutputInvalid("model_output_invalid", "模型输出不是 JSON 对象。")
    status = obj.get("status")
    if status not in _ALLOWED_MODEL_STATUS:
        raise ModelOutputInvalid("model_output_invalid", f"模型输出 status 非法：{status!r}。")
    total = len(expected_types)
    if status in ("insufficient_evidence", "blocked_scope", "partial"):
        raise AppError("insufficient_evidence", _insufficient(total))
    if status != "ready":
        raise ModelOutputInvalid("model_output_invalid", f"模型输出状态未就绪：{status!r}。")
    if obj.get("requested_count") != total:
        raise ModelOutputInvalid("model_output_invalid", "requested_count 与用户题量不一致。")
    if obj.get("generated_count") != total:
        raise ModelOutputInvalid("model_output_invalid", "generated_count 与用户题量不一致。")
    questions = obj.get("questions")
    if not isinstance(questions, list) or len(questions) != total:
        raise ModelOutputInvalid("model_output_invalid", "questions 数量不等于用户题量。")
    normalized = []
    for idx, expected_type in enumerate(expected_types, start=1):
        normalized.append(_validate_question(questions[idx - 1], expected_type, mapping, idx))
    return normalized


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def generate(zone_id: str, mindmap_id: str | None, question_counts=None) -> dict:
    counts, expected_types = normalize_question_counts(question_counts)
    total = len(expected_types)
    zone = _require_zone(zone_id)
    if mindmap_id:
        _require_mindmap(mindmap_id, zone_id)
    clue_titles = _evidence_clue(mindmap_id)
    query = " ".join(clue_titles) if clue_titles else str(zone["name"])

    import rag

    items = rag.retrieve(zone_id, query)
    if not items:
        raise AppError("insufficient_evidence", _insufficient(total))
    evidence_pack, mapping = rag.build_evidence(zone_id, "", items)
    if not mapping:
        raise AppError("insufficient_evidence", _insufficient(total))

    teacher_skill, teacher_sha = llm.load_skill("teacher_examiner")
    style_skill, style_sha = llm.load_skill("exam_style")
    constraints = {
        "question_count": total,
        "question_counts": counts,
        "types": expected_types,
        "difficulty_progression": "基础理解 → 条件辨析 → 计算应用",
        "must_cite_evidence": True,
        "math_policy": "所有数学片段用 $...$；每题最多一个 latex 主公式；必须通过 MathML 转换校验",
    }
    system_extra = (
        teacher_skill
        + "\n\n"
        + style_skill
        + "\n\n课程证据包：\n"
        + json.dumps(evidence_pack, ensure_ascii=False)
        + "\n\n用户试卷约束（必须逐项满足）：\n"
        + json.dumps(constraints, ensure_ascii=False)
    )
    user_content = f"请以经验丰富教师的方式，严格按题型和数量生成 {total} 道题，并完成公式与答案终审。"
    if clue_titles:
        user_content += "\n导图考点线索（仅作考点线索，命题必须回到证据）：" + "、".join(clue_titles)

    max_tokens = min(8000, max(config.QUIZ_MAX_TOKENS, total * 600))

    def call(content: str):
        return llm.chat_json(
            "quiz",
            content,
            kind="quiz",
            system_extra=system_extra,
            max_tokens=max_tokens,
            temperature=config.CHAT_TEMPERATURE,
        )

    obj, _usage = call(user_content)
    try:
        questions = _validate_quiz_output(obj, mapping, expected_types)
    except ModelOutputInvalid as exc:
        correction = (
            "\n\n上一次输出未通过校验："
            f"{exc.message}"
            "。请严格按用户试卷约束重新输出一个完整 JSON 对象：status=ready、"
            f"requested_count={total}、generated_count={total}、"
            f"questions 顺序={expected_types}，每题必须带非空且有效的 citations；"
            "所有数学片段使用配对的 $...$，主公式必须能转换为 MathML。"
        )
        obj2, _usage2 = call(user_content + correction)
        questions = _validate_quiz_output(obj2, mapping, expected_types)

    quiz_id = db.new_id()
    created_at = db.now()
    db.x(
        "INSERT INTO quizzes (id, zone_id, mindmap_id, created_at) VALUES (?,?,?,?)",
        (quiz_id, zone_id, mindmap_id, created_at),
    )
    seq = [
        (
            db.new_id(),
            quiz_id,
            question["idx"],
            question["type"],
            question["stem"],
            json.dumps(question["options"], ensure_ascii=False),
            question["answer"],
            question["explanation"],
            json.dumps(question["citations"], ensure_ascii=False),
            question["latex"],
        )
        for question in questions
    ]
    db.xmany(
        "INSERT INTO quiz_questions (id, quiz_id, idx, type, stem, options, answer,"
        " explanation, citations, latex) VALUES (?,?,?,?,?,?,?,?,?,?)",
        seq,
    )
    return {
        "quiz_id": quiz_id,
        "question_counts": counts,
        "teacher_skill": {"name": "experienced-teacher-examiner", "sha256": teacher_sha},
        "style_skill": {"name": "bupt-exam-style", "sha256": style_sha},
        "formula_check": "passed",
        "questions": get_quiz(quiz_id)["questions"],
    }


def get_quiz(quiz_id: str) -> dict:
    quiz = db.q1("SELECT id, zone_id, created_at FROM quizzes WHERE id = ?", (quiz_id,))
    if quiz is None:
        raise AppError("invalid_request", "试卷不存在。")
    rows = db.q(
        "SELECT id, idx, type, stem, options, answer, explanation, citations, latex"
        " FROM quiz_questions WHERE quiz_id = ? ORDER BY idx",
        (quiz_id,),
    )
    questions = []
    for row in rows:
        try:
            options = json.loads(row["options"] or "[]")
        except (TypeError, ValueError):
            options = []
        try:
            citations = json.loads(row["citations"] or "[]")
        except (TypeError, ValueError):
            citations = []
        questions.append({
            "id": row["id"],
            "idx": int(row["idx"]),
            "type": row["type"],
            "stem": row["stem"],
            "stem_html": _rich_text_html(row["stem"]),
            "options": options if isinstance(options, list) else [],
            "option_html": [_rich_text_html(option) for option in options] if isinstance(options, list) else [],
            "answer": row["answer"],
            "answer_html": _rich_text_html(row["answer"]),
            "explanation": row["explanation"],
            "explanation_html": _rich_text_html(row["explanation"]),
            "citations": citations if isinstance(citations, list) else [],
            "latex": row["latex"],
            "mathml": _safe_mathml(row["latex"]),
        })
    return {
        "id": quiz["id"],
        "zone_id": quiz["zone_id"],
        "created_at": quiz["created_at"],
        "question_counts": {
            qtype: sum(1 for question in questions if question["type"] == qtype)
            for qtype in dict.fromkeys(question["type"] for question in questions)
        },
        "questions": questions,
    }


# ---------------------------------------------------------------------------
# 学生版 DOCX / PDF 导出
# ---------------------------------------------------------------------------

_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _set_run_font(run, *, east="宋体", latin="Times New Roman", size_pt=None, bold=None):
    from docx.oxml.ns import qn
    from docx.shared import Pt

    run.font.name = latin
    if size_pt is not None:
        run.font.size = Pt(size_pt)
    if bold is not None:
        run.font.bold = bold
    rpr = run._element.rPr
    if rpr is not None:
        rfonts = rpr.find(qn("w:rFonts"))
        if rfonts is not None:
            rfonts.set(qn("w:eastAsia"), east)


def _latex_to_omml(latex: str):
    """LaTeX → OMML 元素；转换失败返回 None（调用方降级为 LaTeX 源码文本）。"""
    try:
        from mathml2omml import convert as mathml_to_omml
        from docx.oxml import parse_xml

        mathml = _latex_to_mathml(latex)
        omml = mathml_to_omml(mathml)
        if isinstance(omml, bytes):
            omml = omml.decode("utf-8")
        xml = str(omml or "").strip()
        if not xml:
            return None
        if "xmlns:m=" not in xml:
            if not xml.startswith("<m:oMath"):
                return None
            xml = xml.replace("<m:oMath", f'<m:oMath xmlns:m="{_MATH_NS}"', 1)
        if "w:" in xml and "xmlns:w=" not in xml:
            xml = xml.replace("<m:oMath", f'<m:oMath xmlns:w="{_WORD_NS}"', 1)
        return parse_xml(xml)
    except Exception:  # noqa: BLE001 - 按规格降级为源码文本
        return None


def _append_math(paragraph, latex: str) -> bool:
    element = _latex_to_omml(latex)
    if element is None:
        return False
    try:
        paragraph._p.append(element)
        return True
    except Exception:  # noqa: BLE001
        return False


def _add_text_with_inline_math(paragraph, text: str) -> None:
    for part in re.split(r"(\$[^$]+\$)", text):
        if not part:
            continue
        if len(part) >= 3 and part.startswith("$") and part.endswith("$"):
            if not _append_math(paragraph, part[1:-1]):
                raise AppError("formula_render_failed", f"行内公式无法转换为 Word 公式：{part}")
        else:
            paragraph.add_run(part)


def _append_field(paragraph, instruction: str, placeholder: str = ""):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    runs = []

    def _fld_char(kind: str):
        run = paragraph.add_run()
        element = OxmlElement("w:fldChar")
        element.set(qn("w:fldCharType"), kind)
        run._r.append(element)
        runs.append(run)
        return run

    _fld_char("begin")
    instr_run = paragraph.add_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    instr_run._r.append(instr)
    runs.append(instr_run)
    _fld_char("separate")
    if placeholder:
        runs.append(paragraph.add_run(placeholder))
    _fld_char("end")
    return runs


def _add_footer_page_number(section) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    footer = section.footer
    try:
        footer.is_linked_to_previous = False
    except Exception:  # noqa: BLE001
        pass
    paragraph = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    for run in list(paragraph.runs):
        run._r.getparent().remove(run._r)
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    runs = _append_field(paragraph, " PAGE ", "1")
    for run in runs:
        _set_run_font(run, size_pt=10.5)


def _build_student_docx(zone_name: str, questions: list[dict], docx_path: str) -> None:
    try:
        for question in questions:
            _validate_question_formulas(dict(question))
    except ModelOutputInvalid as exc:
        raise AppError("formula_render_failed", "公式导出校验失败：" + exc.message) from exc

    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    document = Document()
    title_paragraph = document.add_paragraph()
    title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_paragraph.add_run(f"《{zone_name}》练习卷（学生版）")
    _set_run_font(title_run, east="黑体", size_pt=18, bold=True)

    for question in questions:
        stem_paragraph = document.add_paragraph()
        stem_paragraph.paragraph_format.space_before = Pt(6)
        prefix_run = stem_paragraph.add_run(f"{question['idx']}. ")
        _set_run_font(prefix_run)
        type_run = stem_paragraph.add_run(f"（{_TYPE_LABELS.get(question['type'], question['type'])}）")
        _set_run_font(type_run)
        _add_text_with_inline_math(stem_paragraph, question["stem"])

        latex = question.get("latex")
        if latex:
            math_paragraph = document.add_paragraph()
            math_paragraph.paragraph_format.left_indent = Pt(18)
            if not _append_math(math_paragraph, latex):
                raise AppError(
                    "formula_render_failed",
                    f"第 {question['idx']} 题主公式无法转换为 Word 原生公式，已停止导出。",
                )

        if question["options"]:
            for index, option in enumerate(question["options"]):
                option_paragraph = document.add_paragraph()
                option_paragraph.paragraph_format.left_indent = Pt(18)
                option_text = f"{_OPTION_LETTERS[index]}. "
                option_run = option_paragraph.add_run(option_text)
                _set_run_font(option_run)
                _add_text_with_inline_math(option_paragraph, str(option))

        answer_paragraph = document.add_paragraph()
        answer_paragraph.paragraph_format.left_indent = Pt(18)
        if question["type"] in ("single_choice", "multiple_choice"):
            answer_text = "答：" + "＿＿＿＿＿＿" * (2 if question["type"] == "multiple_choice" else 1)
        elif question["type"] == "true_false":
            answer_text = "答：（　　　　）"
        elif question["type"] == "fill_blank":
            answer_text = "答：＿＿＿＿＿＿＿＿＿＿＿＿＿＿"
        else:
            answer_text = "答："
        answer_run = answer_paragraph.add_run(answer_text)
        _set_run_font(answer_run)
        if question["type"] in ("short_answer", "calculation"):
            for _ in range(3):
                document.add_paragraph()

    for section in document.sections:
        _add_footer_page_number(section)
    document.save(docx_path)


def export_student(quiz_id: str) -> dict:
    quiz = get_quiz(quiz_id)
    zone = _require_zone(quiz["zone_id"])
    config.ensure_dirs()
    docx_path = config.export_dir() / f"{quiz_id}_student.docx"
    pdf_path = config.export_dir() / f"{quiz_id}_student.pdf"
    _build_student_docx(str(zone["name"]), quiz["questions"], str(docx_path))

    import officedoc

    officedoc.docx_to_pdf(str(docx_path), str(pdf_path))
    return {"docx": str(docx_path.resolve()), "pdf": str(pdf_path.resolve())}
