"""论文格式检查与 DOCX 修正（含检查报告 DOCX/PDF）。

对外函数见 docs/模块接口约定.md：create_job / check / execute。
规格：第 8.3、12、13 节。检查阶段只读原稿；执行阶段只做 6 种白名单操作并另存修正版。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, Twips

import config
import db
import llm
from llm import AppError, ModelOutputInvalid

_CHECK_CODES = (
    "TITLE_STYLE",
    "ABSTRACT_STYLE",
    "KEYWORDS_COUNT",
    "HEADING_STYLE",
    "BODY_STYLE",
    "PAGE_LAYOUT",
    "CAPTION_STYLE",
    "FOOTER_PAGENUM",
    "TOC_FIELD",
    "REFERENCES_ORDER",
    "WHITESPACE",
)
_ALIGN_TO_NAME = {
    WD_ALIGN_PARAGRAPH.LEFT: "left",
    WD_ALIGN_PARAGRAPH.CENTER: "center",
    WD_ALIGN_PARAGRAPH.RIGHT: "right",
    WD_ALIGN_PARAGRAPH.JUSTIFY: "justify",
}
_NAME_TO_ALIGN = {
    "left": WD_ALIGN_PARAGRAPH.LEFT,
    "center": WD_ALIGN_PARAGRAPH.CENTER,
    "right": WD_ALIGN_PARAGRAPH.RIGHT,
    "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
}
_BUILTIN_STYLE_NAMES = {
    "heading1": "Heading 1",
    "heading2": "Heading 2",
    "heading3": "Heading 3",
}
_MODEL_SUMMARY_MAX_PARAGRAPHS = 240
_KNOWN_LIMITATIONS = [
    "模板未定义页眉内容，页眉保持原样。",
    "参考文献只检查、不修改。",
    "关键词数量只检查、不修改正文内容。",
    "首行缩进按字符属性（w:firstLineChars）写入，同时写入等宽近似值。",
    "格式检查基于 DOCX 结构解析，未做页面渲染视觉复核。",
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _close_num(actual, expected, tolerance: float) -> bool:
    if actual is None:
        return False
    try:
        return abs(float(actual) - float(expected)) <= tolerance
    except (TypeError, ValueError):
        return False


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:g}"
    if value is None:
        return "未设置"
    return str(value)


def _issue(code: str, target: str, prop: str, expected, actual, message: str) -> dict:
    return {
        "code": code,
        "target": target,
        "property": prop,
        "expected": _fmt(expected),
        "actual": _fmt(actual),
        "message": message,
    }


def _require_job(paper_id: str):
    row = db.q1("SELECT * FROM paper_jobs WHERE id = ?", (paper_id,))
    if row is None:
        raise AppError("invalid_request", "论文任务不存在。")
    return row


def _update_job(paper_id: str, **fields) -> None:
    columns = ", ".join(f"{key} = ?" for key in fields)
    db.x(f"UPDATE paper_jobs SET {columns} WHERE id = ?", (*fields.values(), paper_id))


# ---------------------------------------------------------------------------
# 模板加载与合并
# ---------------------------------------------------------------------------

def _load_builtin_profile() -> dict:
    path = Path(config.PAPER_BUILTIN_PROFILE)
    if not path.is_file():
        raise AppError("internal_error", "内置论文模板文件缺失。")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AppError("internal_error", f"内置论文模板无法解析：{exc}") from exc
    if not isinstance(data, dict):
        raise AppError("internal_error", "内置论文模板不是 JSON 对象。")
    return data


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key not in merged:
            continue  # 未知顶层键忽略
        if isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        elif isinstance(merged[key], dict) and not isinstance(value, dict):
            continue  # 类型不符的覆盖由下方校验拒绝
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _validate_profile_shape(profile: dict) -> None:
    styles = profile.get("styles")
    page = profile.get("page")
    if not isinstance(styles, dict) or not isinstance(page, dict):
        raise AppError("invalid_request", "模板 JSON 字段类型不正确：缺少 page/styles 对象。")
    for key in ("title", "body", "heading1", "heading2", "heading3", "caption"):
        style = styles.get(key)
        if not isinstance(style, dict):
            raise AppError("invalid_request", f"模板 JSON 字段类型不正确：styles.{key} 必须是对象。")
        if not isinstance(style.get("font"), dict):
            raise AppError("invalid_request", f"模板 JSON 字段类型不正确：styles.{key}.font 必须是对象。")
        if not isinstance(style.get("font_size_pt"), (int, float)):
            raise AppError("invalid_request", f"模板 JSON 字段类型不正确：styles.{key}.font_size_pt 必须是数字。")
    margins = page.get("margins_mm")
    if not isinstance(margins, dict):
        raise AppError("invalid_request", "模板 JSON 字段类型不正确：page.margins_mm 必须是对象。")
    for key in ("top", "bottom", "left", "right"):
        if not isinstance(margins.get(key), (int, float)):
            raise AppError("invalid_request", f"模板 JSON 字段类型不正确：page.margins_mm.{key} 必须是数字。")


def _effective_profile(profile_mode: str, profile_json) -> tuple[dict, str]:
    if profile_mode not in ("builtin", "upload"):
        raise AppError("invalid_request", "无效的模板选择。")
    builtin = _load_builtin_profile()
    if profile_mode == "builtin":
        _validate_profile_shape(builtin)
        return builtin, "builtin"
    if not isinstance(profile_json, dict):
        raise AppError("invalid_request", "上传模板必须是 JSON 对象。")
    if "profile_id" in profile_json and not isinstance(profile_json["profile_id"], str):
        raise AppError("invalid_request", "模板 JSON 的 profile_id 必须是字符串。")
    if "page" in profile_json and not isinstance(profile_json["page"], dict):
        raise AppError("invalid_request", "模板 JSON 的 page 必须是对象。")
    if "styles" in profile_json and not isinstance(profile_json["styles"], dict):
        raise AppError("invalid_request", "模板 JSON 的 styles 必须是对象。")
    merged = _deep_merge(builtin, profile_json)
    _validate_profile_shape(merged)
    return merged, "upload"


def _profile_digest(profile: dict) -> str:
    canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# DOCX 属性读取
# ---------------------------------------------------------------------------

def _element_rpr_east_asia(element) -> str | None:
    rpr = element.find(qn("w:rPr"))
    if rpr is None:
        return None
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        return None
    return rfonts.get(qn("w:eastAsia"))


def _run_east_asia(run) -> str | None:
    return _element_rpr_east_asia(run._element)


def _style_east_asia(style) -> str | None:
    try:
        return _element_rpr_east_asia(style.element)
    except Exception:  # noqa: BLE001
        return None


def _paragraph_east_asia(paragraph) -> str | None:
    for run in paragraph.runs:
        if (run.text or "").strip():
            value = _run_east_asia(run)
            if value:
                return value
    style = paragraph.style
    guard = 0
    while style is not None and guard < 10:
        value = _style_east_asia(style)
        if value:
            return value
        style = style.base_style
        guard += 1
    return None


def _paragraph_size_pt(paragraph) -> float | None:
    for run in paragraph.runs:
        if (run.text or "").strip() and run.font.size is not None:
            return float(run.font.size.pt)
    style = paragraph.style
    guard = 0
    while style is not None and guard < 10:
        if style.font.size is not None:
            return float(style.font.size.pt)
        style = style.base_style
        guard += 1
    return None


def _paragraph_bold(paragraph) -> bool:
    for run in paragraph.runs:
        if (run.text or "").strip() and run.font.bold is not None:
            return bool(run.font.bold)
    style = paragraph.style
    guard = 0
    while style is not None and guard < 10:
        if style.font.bold is not None:
            return bool(style.font.bold)
        style = style.base_style
        guard += 1
    return False


def _paragraph_alignment_name(paragraph) -> str:
    alignment = paragraph.alignment
    if alignment is None:
        style = paragraph.style
        guard = 0
        while style is not None and guard < 10:
            if style.paragraph_format.alignment is not None:
                alignment = style.paragraph_format.alignment
                break
            style = style.base_style
            guard += 1
    if alignment is None:
        return "left"  # Word 默认左对齐
    return _ALIGN_TO_NAME.get(alignment, str(alignment))


def _paragraph_line_spacing(paragraph) -> float:
    value = paragraph.paragraph_format.line_spacing
    if value is None:
        style = paragraph.style
        guard = 0
        while style is not None and guard < 10:
            value = style.paragraph_format.line_spacing
            if value is not None:
                break
            style = style.base_style
            guard += 1
    if value is None:
        return 1.0
    if isinstance(value, (int, float)):
        return float(value)
    # 固定磅值行距：规格要求倍数行距，按不等价处理（返回负数表示固定磅值）
    try:
        return -float(value.pt)
    except Exception:  # noqa: BLE001
        return -1.0


def _paragraph_indent_chars(paragraph) -> float:
    ppr = paragraph._p.pPr
    if ppr is not None:
        ind = ppr.find(qn("w:ind"))
        if ind is not None:
            first_chars = ind.get(qn("w:firstLineChars"))
            if first_chars is not None:
                try:
                    return int(first_chars) / 100.0
                except ValueError:
                    pass
            first = ind.get(qn("w:firstLine"))
            if first is not None:
                try:
                    twips = int(first)
                except ValueError:
                    twips = None
                if twips is not None:
                    size = _paragraph_size_pt(paragraph) or 12.0
                    return (twips / 20.0) / size
    style = paragraph.style
    guard = 0
    while style is not None and guard < 10:
        try:
            style_ppr = style.element.find(qn("w:pPr"))
        except Exception:  # noqa: BLE001
            style_ppr = None
        if style_ppr is not None:
            ind = style_ppr.find(qn("w:ind"))
            if ind is not None:
                first_chars = ind.get(qn("w:firstLineChars"))
                if first_chars is not None:
                    try:
                        return int(first_chars) / 100.0
                    except ValueError:
                        pass
                first = ind.get(qn("w:firstLine"))
                if first is not None:
                    try:
                        return (int(first) / 20.0) / (_paragraph_size_pt(paragraph) or 12.0)
                    except ValueError:
                        pass
        style = style.base_style
        guard += 1
    return 0.0


def _iter_field_elements(element):
    for child in element.iter():
        yield child


def _element_has_field(element, keyword: str) -> bool:
    for child in _iter_field_elements(element):
        if child.tag == qn("w:instrText") and child.text and keyword in child.text:
            return True
        if child.tag == qn("w:fldSimple"):
            instruction = child.get(qn("w:instr")) or ""
            if keyword in instruction:
                return True
    return False


def _paragraph_has_field(paragraph, keyword: str | None = None) -> bool:
    for child in paragraph._p.iter():
        if child.tag == qn("w:instrText") and child.text:
            if keyword is None or keyword in child.text:
                return True
        if child.tag == qn("w:fldSimple"):
            instruction = child.get(qn("w:instr")) or ""
            if keyword is None or keyword in instruction:
                return True
    return False


def _paragraph_has_sectpr(paragraph) -> bool:
    ppr = paragraph._p.pPr
    return ppr is not None and ppr.find(qn("w:sectPr")) is not None


# ---------------------------------------------------------------------------
# 检查（程序确定性检查 11 项）
# ---------------------------------------------------------------------------

def _heading_level(paragraph) -> int | None:
    name = ""
    try:
        if paragraph.style is not None and paragraph.style.name:
            name = paragraph.style.name
    except Exception:  # noqa: BLE001
        name = ""
    match = re.match(r"^\s*(?:heading|标题)\s*([1-9])", name or "", re.IGNORECASE)
    if match:
        return int(match.group(1))
    text = paragraph.text.strip()
    if re.match(r"^第[一二三四五六七八九十百]+[章篇]", text):
        return 1
    if re.match(r"^[一二三四五六七八九十]+[、．.]", text):
        return 1
    if re.match(r"^\d+\.\d+\.\d+", text):
        return 3
    if re.match(r"^\d+\.\d+", text):
        return 2
    if re.match(r"^\d+[、．.]\s*\S", text):
        return 1
    return None


def _is_caption(text: str) -> bool:
    return bool(re.match(r"^(图|表)\s*\d+", text))


def _is_blank_paragraph(paragraph) -> bool:
    if (paragraph.text or "").strip():
        return False
    if _paragraph_has_sectpr(paragraph):
        return False
    for child in paragraph._p.iter():
        if child.tag in (
            qn("w:drawing"), qn("w:pict"), qn("w:object"),
            qn("w:fldChar"), qn("w:instrText"), qn("w:fldSimple"),
        ):
            return False
        if child.tag == qn("w:br"):
            return False
    return True


def _style_issues(paragraph, style_cfg: dict, code: str, target: str, where: str,
                  include: tuple[str, ...]) -> list[dict]:
    issues: list[dict] = []
    font_cfg = style_cfg.get("font") or {}
    if "font" in include:
        expected_font = font_cfg.get("east_asian")
        if expected_font:
            actual_font = _paragraph_east_asia(paragraph)
            if actual_font != expected_font:
                issues.append(_issue(
                    code, target, "font_east_asian", expected_font,
                    actual_font or "未显式设置（继承样式）",
                    f"{where}字体应为 {expected_font}，实际 {actual_font or '未显式设置'}。",
                ))
    if "size" in include:
        expected_size = style_cfg.get("font_size_pt")
        if expected_size is not None:
            actual_size = _paragraph_size_pt(paragraph)
            if not _close_num(actual_size, expected_size, 0.1):
                issues.append(_issue(
                    code, target, "font_size_pt", f"{_fmt(float(expected_size))}pt",
                    f"{_fmt(actual_size)}pt" if actual_size is not None else "未显式设置",
                    f"{where}字号应为 {_fmt(float(expected_size))}pt，实际 {_fmt(actual_size)}pt。",
                ))
    if "bold" in include:
        expected_bold = style_cfg.get("bold")
        if isinstance(expected_bold, bool):
            actual_bold = _paragraph_bold(paragraph)
            if actual_bold != expected_bold:
                issues.append(_issue(
                    code, target, "bold", expected_bold, actual_bold,
                    f"{where}加粗应为 {'是' if expected_bold else '否'}，实际 {'是' if actual_bold else '否'}。",
                ))
    if "alignment" in include:
        expected_alignment = style_cfg.get("alignment")
        if expected_alignment:
            actual_alignment = _paragraph_alignment_name(paragraph)
            if actual_alignment != expected_alignment:
                issues.append(_issue(
                    code, target, "alignment", expected_alignment, actual_alignment,
                    f"{where}对齐方式应为 {expected_alignment}，实际 {actual_alignment}。",
                ))
    if "line_spacing" in include:
        spacing_cfg = style_cfg.get("line_spacing") or {}
        if spacing_cfg.get("type") == "multiple" and isinstance(spacing_cfg.get("value"), (int, float)):
            expected_spacing = float(spacing_cfg["value"])
            actual_spacing = _paragraph_line_spacing(paragraph)
            if actual_spacing < 0:
                issues.append(_issue(
                    code, target, "line_spacing", f"{_fmt(expected_spacing)} 倍行距",
                    f"固定值 {_fmt(-actual_spacing)}pt",
                    f"{where}行距应为 {_fmt(expected_spacing)} 倍，实际为固定磅值行距。",
                ))
            elif abs(actual_spacing - expected_spacing) > 0.01:
                issues.append(_issue(
                    code, target, "line_spacing", f"{_fmt(expected_spacing)} 倍行距",
                    f"{_fmt(actual_spacing)} 倍行距",
                    f"{where}行距应为 {_fmt(expected_spacing)} 倍，实际 {_fmt(actual_spacing)} 倍。",
                ))
    if "first_line_indent" in include:
        indent_cfg = style_cfg.get("first_line_indent") or {}
        if indent_cfg.get("unit") == "char" and isinstance(indent_cfg.get("value"), (int, float)):
            expected_chars = float(indent_cfg["value"])
            actual_chars = _paragraph_indent_chars(paragraph)
            if abs(actual_chars - expected_chars) > 0.25:
                issues.append(_issue(
                    code, target, "first_line_indent", f"首行缩进 {_fmt(expected_chars)} 字符",
                    f"首行缩进 {_fmt(actual_chars)} 字符",
                    f"{where}首行缩进应为 {_fmt(expected_chars)} 字符，实际 {_fmt(actual_chars)} 字符。",
                ))
    return issues


def _analyze_document(doc, profile: dict) -> dict:
    styles_cfg = profile["styles"]
    paragraphs = list(doc.paragraphs)
    issues: list[dict] = []
    checked = 0

    title_idx = None
    for index, paragraph in enumerate(paragraphs):
        if (paragraph.text or "").strip():
            title_idx = index
            break

    references_start = None
    for index, paragraph in enumerate(paragraphs):
        text = (paragraph.text or "").strip()
        if text in ("参考文献", "参考资料", "References", "REFERENCES"):
            references_start = index
            break

    abstract_idxs: list[int] = []
    keyword_idxs: list[int] = []
    heading_idxs: dict[int, int] = {}
    heading_other: set[int] = set()
    caption_idxs: list[int] = []
    body_idxs: list[int] = []
    toc_idx: int | None = None
    ref_entry_idxs: list[int] = []

    for index, paragraph in enumerate(paragraphs):
        stripped = (paragraph.text or "").strip()
        if not stripped:
            continue
        if index == title_idx:
            continue
        if references_start is not None and index == references_start:
            continue
        if references_start is not None and index > references_start:
            if re.match(r"^\[\d+\]", stripped):
                ref_entry_idxs.append(index)
            continue
        if stripped.startswith("摘要"):
            abstract_idxs.append(index)
            continue
        if stripped.startswith("关键词"):
            keyword_idxs.append(index)
            continue
        if stripped.startswith("目录"):
            if toc_idx is None:
                toc_idx = index
            continue
        level = _heading_level(paragraph)
        if level:
            if level <= 3:
                heading_idxs[index] = level
            else:
                heading_other.add(index)
            continue
        if _is_caption(stripped):
            caption_idxs.append(index)
            continue
        try:
            style_name = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
        except Exception:  # noqa: BLE001
            style_name = ""
        if "toc" in style_name or "目录" in style_name:
            continue
        if _paragraph_has_field(paragraph):
            continue
        body_idxs.append(index)

    # 1. TITLE_STYLE
    if title_idx is not None:
        checked += 1
        issues.extend(_style_issues(
            paragraphs[title_idx], styles_cfg["title"], "TITLE_STYLE",
            f"p{title_idx + 1}", f"标题（第 {title_idx + 1} 段）",
            ("font", "size", "bold", "alignment"),
        ))
    # 2. ABSTRACT_STYLE
    for index in abstract_idxs:
        checked += 1
        issues.extend(_style_issues(
            paragraphs[index], styles_cfg["body"], "ABSTRACT_STYLE",
            f"p{index + 1}", f"摘要（第 {index + 1} 段）",
            ("font", "size", "line_spacing"),
        ))
    # 3. KEYWORDS_COUNT
    for index in keyword_idxs:
        checked += 1
        text = paragraphs[index].text.strip()
        body_text = re.sub(r"^关键词\s*[:：]?\s*", "", text)
        keywords = [item for item in re.split(r"[；;，,、\s]+", body_text) if item.strip()]
        if not (config.PAPER_KEYWORDS_MIN <= len(keywords) <= config.PAPER_KEYWORDS_MAX):
            issues.append(_issue(
                "KEYWORDS_COUNT", f"p{index + 1}", "keywords_count",
                f"{config.PAPER_KEYWORDS_MIN}–{config.PAPER_KEYWORDS_MAX} 个",
                f"{len(keywords)} 个",
                f"第 {index + 1} 段关键词应为 {config.PAPER_KEYWORDS_MIN}–{config.PAPER_KEYWORDS_MAX} 个，实际 {len(keywords)} 个。",
            ))
    # 4. HEADING_STYLE
    for index, level in sorted(heading_idxs.items()):
        checked += 1
        issues.extend(_style_issues(
            paragraphs[index], styles_cfg.get(f"heading{level}", styles_cfg["heading1"]),
            "HEADING_STYLE", f"p{index + 1}", f"第 {index + 1} 段（{level} 级标题）",
            ("font", "size", "bold", "alignment"),
        ))
    # 5. BODY_STYLE
    for index in body_idxs:
        checked += 1
        issues.extend(_style_issues(
            paragraphs[index], styles_cfg["body"], "BODY_STYLE",
            f"p{index + 1}", f"正文（第 {index + 1} 段）",
            ("font", "size", "line_spacing", "first_line_indent", "alignment"),
        ))
    # 6. PAGE_LAYOUT
    page_cfg = profile["page"]
    margins_cfg = page_cfg.get("margins_mm") or {}
    for index, section in enumerate(doc.sections):
        checked += 1
        target = f"section:{index + 1}"
        if str(page_cfg.get("size", "A4")).upper() == "A4":
            try:
                width_mm = section.page_width.mm if section.page_width is not None else None
                height_mm = section.page_height.mm if section.page_height is not None else None
            except Exception:  # noqa: BLE001
                width_mm = height_mm = None
            landscape = str(page_cfg.get("orientation", "portrait")).lower() == "landscape"
            expected_w, expected_h = (297.0, 210.0) if landscape else (210.0, 297.0)
            if not (_close_num(width_mm, expected_w, 2) and _close_num(height_mm, expected_h, 2)):
                issues.append(_issue(
                    "PAGE_LAYOUT", target, "page_size",
                    f"A4（{expected_w:g}×{expected_h:g} mm）",
                    f"{_fmt(width_mm)}×{_fmt(height_mm)} mm" if width_mm else "未设置",
                    f"第 {index + 1} 节纸张应为 A4，实际 {_fmt(width_mm)}×{_fmt(height_mm)} mm。",
                ))
        for key, attr in (("top", "top_margin"), ("bottom", "bottom_margin"),
                          ("left", "left_margin"), ("right", "right_margin")):
            expected = margins_cfg.get(key)
            if expected is None:
                continue
            try:
                actual = getattr(section, attr)
                actual_mm = actual.mm if actual is not None else None
            except Exception:  # noqa: BLE001
                actual_mm = None
            if not _close_num(actual_mm, expected, 1.0):
                issues.append(_issue(
                    "PAGE_LAYOUT", target, f"margin_{key}",
                    f"{_fmt(float(expected))} mm", f"{_fmt(actual_mm)} mm",
                    f"第 {index + 1} 节{key}页边距应为 {_fmt(float(expected))} mm，实际 {_fmt(actual_mm)} mm。",
                ))
    # 7. CAPTION_STYLE
    for index in caption_idxs:
        checked += 1
        issues.extend(_style_issues(
            paragraphs[index], styles_cfg["caption"], "CAPTION_STYLE",
            f"p{index + 1}", f"题注（第 {index + 1} 段）",
            ("font", "size", "alignment"),
        ))
    # 8. FOOTER_PAGENUM
    checked += 1
    previous_has_page = False
    for index, section in enumerate(doc.sections):
        has_page = False
        try:
            footer = section.footer
            if footer.is_linked_to_previous:
                has_page = previous_has_page
            else:
                has_page = _element_has_field(footer._element, "PAGE")
        except Exception:  # noqa: BLE001
            has_page = False
        if not has_page:
            issues.append(_issue(
                "FOOTER_PAGENUM", f"section:{index + 1}", "page_field",
                "页脚含 PAGE 域", "无 PAGE 域",
                f"第 {index + 1} 节页脚未检测到 PAGE 域。",
            ))
        previous_has_page = has_page
    # 9. TOC_FIELD
    if toc_idx is not None:
        checked += 1
        has_toc = any(
            _paragraph_has_field(paragraph, "TOC")
            for paragraph in paragraphs[toc_idx + 1:]
        )
        if not has_toc:
            issues.append(_issue(
                "TOC_FIELD", f"p{toc_idx + 1}", "toc_field",
                "目录标题后含 TOC 域", "无 TOC 域",
                f"第 {toc_idx + 1} 段为“目录”标题，但其后未检测到 TOC 域。",
            ))
    # 10. REFERENCES_ORDER
    checked += 1
    entry_numbers: list[int] = []
    for index in ref_entry_idxs:
        match = re.match(r"^\[(\d+)\]", paragraphs[index].text.strip())
        if match:
            entry_numbers.append(int(match.group(1)))
    if entry_numbers:
        expected_numbers = list(range(1, len(entry_numbers) + 1))
        if entry_numbers != expected_numbers:
            issues.append(_issue(
                "REFERENCES_ORDER", "references", "entry_numbering",
                "以 [1] 起连续递增", ",".join(str(number) for number in entry_numbers),
                "参考文献条目编号不连续：" + "、".join(f"[{number}]" for number in entry_numbers) + "。",
            ))
    citation_source = paragraphs[: references_start] if references_start is not None else paragraphs
    citation_numbers: list[int] = []
    for paragraph in citation_source:
        for match in re.finditer(r"\[(\d+)\]", paragraph.text or ""):
            citation_numbers.append(int(match.group(1)))
    if citation_numbers and (not entry_numbers or max(citation_numbers) > len(entry_numbers)):
        issues.append(_issue(
            "REFERENCES_ORDER", "references", "citation_range",
            f"正文引用编号 ≤ 条目数（{len(entry_numbers)}）",
            f"最大引用编号 {max(citation_numbers)}",
            f"正文引用编号超出参考文献条目数：条目 {len(entry_numbers)} 条，最大引用编号 {max(citation_numbers)}。",
        ))
    # 11. WHITESPACE
    checked += 1
    trailing_positions = []
    for index, paragraph in enumerate(paragraphs):
        text = paragraph.text or ""
        if not text:
            continue
        for line in text.split("\n"):
            if line != line.rstrip(" \t\u3000"):
                trailing_positions.append(index + 1)
                break
    if trailing_positions:
        preview = "、".join(str(pos) for pos in trailing_positions[:5])
        issues.append(_issue(
            "WHITESPACE", "document", "line_trailing_spaces",
            "无行尾空格", f"{len(trailing_positions)} 处",
            f"检测到 {len(trailing_positions)} 处行尾空格（第 {preview} 段"
            + ("等" if len(trailing_positions) > 5 else "") + "）。",
        ))
    blank_runs: list[tuple[int, int]] = []
    run_start = None
    run_length = 0
    for index, paragraph in enumerate(paragraphs):
        if _is_blank_paragraph(paragraph):
            if run_start is None:
                run_start = index
                run_length = 0
            run_length += 1
        else:
            if run_start is not None and run_length >= 3:
                blank_runs.append((run_start, run_length))
            run_start = None
            run_length = 0
    if run_start is not None and run_length >= 3:
        blank_runs.append((run_start, run_length))
    if blank_runs:
        preview = "、".join(f"第 {start + 1} 段起 {length} 个空段" for start, length in blank_runs[:5])
        issues.append(_issue(
            "WHITESPACE", "document", "consecutive_blank_lines",
            "连续空行 < 3", f"{len(blank_runs)} 处",
            f"检测到 {len(blank_runs)} 处连续 ≥3 个空段落：{preview}。",
        ))

    # 结构化摘要（供模型角色判定与执行计划）
    paragraph_summaries = []
    section_index = 1
    for index, paragraph in enumerate(paragraphs):
        try:
            style_name = paragraph.style.name if paragraph.style is not None else ""
        except Exception:  # noqa: BLE001
            style_name = ""
        paragraph_summaries.append({
            "id": f"p{index + 1}",
            "text": (paragraph.text or "").strip()[:80],
            "style": style_name,
            "is_heading": index in heading_idxs,
            "heading_level": heading_idxs.get(index),
            "is_caption": index in caption_idxs,
            "section": section_index,
        })
        if _paragraph_has_sectpr(paragraph):
            section_index += 1
    section_summaries = []
    for index, section in enumerate(doc.sections):
        try:
            section_summaries.append({
                "id": f"section:{index + 1}",
                "page_width_mm": round(section.page_width.mm, 2) if section.page_width else None,
                "page_height_mm": round(section.page_height.mm, 2) if section.page_height else None,
                "top_margin_mm": round(section.top_margin.mm, 2) if section.top_margin else None,
                "bottom_margin_mm": round(section.bottom_margin.mm, 2) if section.bottom_margin else None,
                "left_margin_mm": round(section.left_margin.mm, 2) if section.left_margin else None,
                "right_margin_mm": round(section.right_margin.mm, 2) if section.right_margin else None,
            })
        except Exception:  # noqa: BLE001
            section_summaries.append({"id": f"section:{index + 1}"})

    return {
        "issues": issues,
        "checked_count": max(checked, len(paragraphs) + len(doc.sections)),
        "paragraph_count": len(paragraphs),
        "section_count": len(doc.sections),
        "title_idx": title_idx,
        "abstract_idxs": abstract_idxs,
        "keyword_idxs": keyword_idxs,
        "heading_idxs": heading_idxs,
        "caption_idxs": caption_idxs,
        "body_idxs": body_idxs,
        "toc_idx": toc_idx,
        "paragraph_summaries": paragraph_summaries[:_MODEL_SUMMARY_MAX_PARAGRAPHS],
        "paragraph_summaries_truncated": len(paragraphs) > _MODEL_SUMMARY_MAX_PARAGRAPHS,
        "section_summaries": section_summaries,
        "has_references": references_start is not None,
    }


# ---------------------------------------------------------------------------
# 模型辅助角色判定（检查阶段最多 1 次；失败不阻塞）
# ---------------------------------------------------------------------------

def _log_paper_usage_failed(error: str) -> None:
    skill_sha = ""
    try:
        _, skill_sha = llm.load_skill("paper")
    except Exception:  # noqa: BLE001
        skill_sha = ""
    try:
        db.x(
            "INSERT INTO usage_logs (id, created_at, kind, model, prompt_tokens, completion_tokens,"
            " latency_ms, ok, error, skill_name, skill_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (db.new_id(), db.now(), "paper_role", None, None, None, None, 0,
             (error or "模型未参与角色判定")[:500], "paper", skill_sha),
        )
    except Exception:  # noqa: BLE001
        pass


def _validate_model_output(obj, paragraph_ids: set[str], section_ids: set[str],
                           profile: dict) -> tuple[list[dict], list[dict], list, str]:
    if not isinstance(obj, dict):
        raise ModelOutputInvalid("model_output_invalid", "模型输出不是 JSON 对象。")
    status = obj.get("status")
    if status not in ("plan_ready", "no_changes", "partial", "needs_profile", "blocked_input"):
        raise ModelOutputInvalid("model_output_invalid", f"模型输出 status 非法：{status!r}。")

    model_issues: list[dict] = []
    raw_issues = obj.get("issues")
    if raw_issues is not None and not isinstance(raw_issues, list):
        raise ModelOutputInvalid("model_output_invalid", "模型输出 issues 不是数组。")
    for item in raw_issues or []:
        if not isinstance(item, dict):
            continue
        target_id = item.get("target_id")
        prop = item.get("property")
        expected = item.get("expected")
        reason = item.get("reason")
        if not isinstance(target_id, str) or target_id not in paragraph_ids:
            continue
        if not isinstance(prop, str) or not prop.strip():
            continue
        if not isinstance(expected, (str, int, float, bool)):
            continue
        if not isinstance(reason, str) or not reason.strip():
            continue
        model_issues.append(_issue(
            "MODEL_ROLE", target_id, prop.strip(), expected, "未程序验证", reason.strip(),
        ))

    model_operations: list[dict] = []
    raw_operations = obj.get("operations")
    if raw_operations is not None and not isinstance(raw_operations, list):
        raise ModelOutputInvalid("model_output_invalid", "模型输出 operations 不是数组。")
    for op in raw_operations or []:
        if not isinstance(op, dict):
            continue
        op_type = op.get("op")
        target = op.get("target_id")
        if op_type not in ("apply_paragraph_style", "normalize_run_font", "set_section_layout"):
            continue
        if not isinstance(target, str):
            continue
        if op_type == "set_section_layout":
            if target in section_ids and op.get("profile_key") == "page":
                model_operations.append({"op": op_type, "target_id": target, "profile_key": "page"})
        else:
            style_key = op.get("style_key")
            if target in paragraph_ids and isinstance(style_key, str) and style_key in profile.get("styles", {}):
                model_operations.append({"op": op_type, "target_id": target, "style_key": style_key})

    needs_review = obj.get("needs_review")
    if not isinstance(needs_review, list):
        needs_review = []
    needs_review = [item for item in needs_review if isinstance(item, (dict, str))]
    return model_issues, model_operations, needs_review, str(status)


def _model_review(analysis: dict, profile: dict, digest: str, job) -> tuple[list[dict], list[dict], list, bool, str]:
    paragraph_ids = {item["id"] for item in analysis["paragraph_summaries"]}
    section_ids = {item["id"] for item in analysis["section_summaries"]}
    user_obj = {
        "document_id": job["id"],
        "document_version": job["sha256"],
        "profile_digest": digest,
        "paragraphs": analysis["paragraph_summaries"],
        "sections": analysis["section_summaries"],
        "issues_program": [
            {key: issue[key] for key in ("code", "target", "property", "expected", "actual", "message")}
            for issue in analysis["issues"]
        ],
    }
    try:
        obj, _usage = llm.chat_json(
            "paper",
            json.dumps(user_obj, ensure_ascii=False),
            kind="paper_role",
            system_extra=json.dumps(profile, ensure_ascii=False),
            max_tokens=config.PAPER_MAX_TOKENS,
            temperature=config.CHAT_TEMPERATURE,
        )
    except AppError as exc:
        _log_paper_usage_failed(f"{exc.code}: {exc.message}")
        return [], [], [], False, f"模型未参与角色判定（{exc.message}）"
    except Exception as exc:  # noqa: BLE001
        _log_paper_usage_failed(f"{exc.__class__.__name__}: {exc}")
        return [], [], [], False, "模型未参与角色判定（调用异常）。"
    try:
        model_issues, model_operations, needs_review, _status = _validate_model_output(
            obj, paragraph_ids, section_ids, profile
        )
    except ModelOutputInvalid as exc:
        _log_paper_usage_failed(exc.message)
        return [], [], [], False, f"模型未参与角色判定（输出非法：{exc.message}）"
    note = "模型已参与角色判定"
    if analysis["paragraph_summaries_truncated"]:
        note += f"（文档较长，仅发送前 {_MODEL_SUMMARY_MAX_PARAGRAPHS} 段摘要）"
    return model_issues, model_operations, needs_review, True, note


def _merge_issues(program_issues: list[dict], model_issues: list[dict]) -> list[dict]:
    merged = list(program_issues)
    seen = {(issue["target"], issue["property"]) for issue in program_issues}
    for issue in model_issues:
        key = (issue["target"], issue["property"])
        if key in seen:
            continue
        seen.add(key)
        merged.append(issue)
    return merged


# ---------------------------------------------------------------------------
# 执行计划（仅 6 种白名单操作）
# ---------------------------------------------------------------------------

def _build_plan(analysis: dict) -> list[dict]:
    plan: list[dict] = []
    if analysis["title_idx"] is not None:
        plan.append({"op": "apply_paragraph_style", "target": f"p{analysis['title_idx'] + 1}", "style_key": "title"})
    for index in analysis["abstract_idxs"]:
        plan.append({"op": "apply_paragraph_style", "target": f"p{index + 1}", "style_key": "body"})
    for index in analysis["keyword_idxs"]:
        plan.append({"op": "apply_paragraph_style", "target": f"p{index + 1}", "style_key": "body"})
    for index, level in sorted(analysis["heading_idxs"].items()):
        plan.append({"op": "apply_paragraph_style", "target": f"p{index + 1}", "style_key": f"heading{min(level, 3)}"})
    for index in analysis["body_idxs"]:
        plan.append({"op": "apply_paragraph_style", "target": f"p{index + 1}", "style_key": "body"})
    for index in analysis["caption_idxs"]:
        plan.append({"op": "apply_paragraph_style", "target": f"p{index + 1}", "style_key": "caption"})
    for index in analysis["body_idxs"]:
        plan.append({"op": "normalize_run_font", "target": f"p{index + 1}", "style_key": "body"})
    for index in range(analysis["section_count"]):
        plan.append({"op": "set_section_layout", "target": f"section:{index + 1}", "profile_key": "page"})
    for index in range(analysis["section_count"]):
        plan.append({"op": "insert_page_number_field", "target": f"section:{index + 1}"})
    if analysis["toc_idx"] is not None:
        plan.append({"op": "replace_toc_field", "target": f"p{analysis['toc_idx'] + 1}"})
    plan.append({"op": "cleanup_whitespace", "target": "document"})
    return plan


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def create_job(filename: str, saved_path: str, sha256: str) -> dict:
    if not isinstance(filename, str) or not filename.strip():
        raise AppError("invalid_request", "文件名不能为空。")
    filename = filename.strip()
    if not filename.lower().endswith(".docx"):
        raise AppError("invalid_request", "仅支持 .docx 格式的论文文件。")
    path = Path(saved_path).resolve()
    if not path.is_file():
        raise AppError("invalid_request", "论文文件不存在。")
    if path.stat().st_size > config.PAPER_MAX_BYTES:
        raise AppError("invalid_request", "论文文件超过 20MB 上限。")
    actual_sha = _sha256_file(path)
    paper_id = db.new_id()
    db.x(
        "INSERT INTO paper_jobs (id, filename, original_path, sha256, profile_digest, status,"
        " issues, operations, checks, corrected_docx, corrected_pdf, report_pdf, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (paper_id, filename, str(path), actual_sha, None, "uploaded",
         None, None, None, None, None, None, db.now()),
    )
    return {"paper_id": paper_id, "filename": filename, "sha256": actual_sha}


def check(paper_id: str, profile_mode: str, profile_json: dict | None) -> dict:
    job = _require_job(paper_id)
    path = Path(job["original_path"])
    if not path.is_file():
        raise AppError("invalid_request", "论文原稿不存在。")
    if _sha256_file(path) != job["sha256"]:
        raise AppError("paper_version_mismatch", "论文原稿已变更，请重新上传后再检查。")
    profile, profile_source = _effective_profile(profile_mode, profile_json)
    digest = _profile_digest(profile)
    try:
        document = Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise AppError("invalid_request", f"无法解析 DOCX 文件：{exc}") from exc

    analysis = _analyze_document(document, profile)
    plan = _build_plan(analysis)
    model_issues, model_operations, needs_review, model_participated, model_note = _model_review(
        analysis, profile, digest, job
    )
    merged_issues = _merge_issues(analysis["issues"], model_issues)
    checked_at = db.now()
    operations_payload = {
        "profile": profile,
        "profile_mode": profile_mode,
        "profile_source": profile_source,
        "plan": plan,
        "model_operations": model_operations,
        "needs_review": needs_review,
        "model_participated": model_participated,
        "model_note": model_note,
        "checked_at": checked_at,
        "checked_count": analysis["checked_count"],
        "paragraph_count": analysis["paragraph_count"],
        "section_count": analysis["section_count"],
        "has_references": analysis["has_references"],
        "has_toc_heading": analysis["toc_idx"] is not None,
        "paragraph_summaries_truncated": analysis["paragraph_summaries_truncated"],
    }
    _update_job(
        paper_id,
        profile_digest=digest,
        status="checked",
        issues=json.dumps(merged_issues, ensure_ascii=False),
        operations=json.dumps(operations_payload, ensure_ascii=False),
        checks=None,
        corrected_docx=None,
        corrected_pdf=None,
        report_pdf=None,
    )
    return {
        "status": "checked",
        "profile_digest": digest,
        "issues": merged_issues,
        "checked_count": analysis["checked_count"],
    }


# ---------------------------------------------------------------------------
# 修正执行：格式应用
# ---------------------------------------------------------------------------

def _get_or_add_rfonts(rpr):
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is not None:
        return rfonts
    try:
        return rpr.get_or_add_rFonts()
    except AttributeError:
        rfonts = OxmlElement("w:rFonts")
        rpr.insert(0, rfonts)
        return rfonts


def _apply_run_font(run, style_cfg: dict, *, with_bold: bool) -> None:
    font_cfg = style_cfg.get("font") or {}
    latin = font_cfg.get("latin")
    east_asian = font_cfg.get("east_asian")
    if latin:
        run.font.name = latin
    size = style_cfg.get("font_size_pt")
    if isinstance(size, (int, float)):
        run.font.size = Pt(float(size))
    if with_bold and isinstance(style_cfg.get("bold"), bool):
        run.font.bold = bool(style_cfg["bold"])
    rpr = run._element.rPr
    if rpr is not None and east_asian:
        rfonts = _get_or_add_rfonts(rpr)
        rfonts.set(qn("w:eastAsia"), str(east_asian))


def _set_first_line_chars(paragraph, chars: float, font_size_pt: float) -> None:
    paragraph_format = paragraph.paragraph_format
    paragraph_format.first_line_indent = Pt(max(chars, 0) * font_size_pt)
    ppr = paragraph._p.pPr
    if ppr is None:
        return
    ind = ppr.find(qn("w:ind"))
    if ind is None:
        return
    ind.set(qn("w:firstLineChars"), str(int(round(chars * 100))))
    ind.set(qn("w:firstLine"), str(int(round(max(chars, 0) * font_size_pt * 20))))


def _apply_paragraph_format(paragraph, style_cfg: dict) -> None:
    alignment = style_cfg.get("alignment")
    if alignment in _NAME_TO_ALIGN:
        paragraph.alignment = _NAME_TO_ALIGN[alignment]
    paragraph_format = paragraph.paragraph_format
    spacing = style_cfg.get("line_spacing") or {}
    if spacing.get("type") == "multiple" and isinstance(spacing.get("value"), (int, float)):
        paragraph_format.line_spacing = float(spacing["value"])
    elif spacing.get("type") == "exact" and isinstance(spacing.get("value"), (int, float)):
        paragraph_format.line_spacing = Pt(float(spacing["value"]))
    if isinstance(style_cfg.get("space_before_pt"), (int, float)):
        paragraph_format.space_before = Pt(float(style_cfg["space_before_pt"]))
    if isinstance(style_cfg.get("space_after_pt"), (int, float)):
        paragraph_format.space_after = Pt(float(style_cfg["space_after_pt"]))
    if isinstance(style_cfg.get("keep_with_next"), bool):
        paragraph_format.keep_with_next = style_cfg["keep_with_next"]
    indent_cfg = style_cfg.get("first_line_indent") or {}
    font_size = style_cfg.get("font_size_pt") if isinstance(style_cfg.get("font_size_pt"), (int, float)) else 12.0
    if indent_cfg.get("unit") == "char" and isinstance(indent_cfg.get("value"), (int, float)):
        _set_first_line_chars(paragraph, float(indent_cfg["value"]), float(font_size))
    for run in paragraph.runs:
        _apply_run_font(run, style_cfg, with_bold=True)


def _normalize_runs(paragraph, style_cfg: dict) -> None:
    for run in paragraph.runs:
        if not (run.text or "").strip():
            continue
        _apply_run_font(run, style_cfg, with_bold=False)


def _ensure_heading_style(paragraph, style_key: str) -> None:
    name = _BUILTIN_STYLE_NAMES.get(style_key)
    if not name:
        return
    try:
        paragraph.style = name
    except Exception:  # noqa: BLE001 - 文档缺少该内置样式时保留原样式，仅做直接格式
        pass


def _set_section_layout(section, page_cfg: dict) -> None:
    if str(page_cfg.get("size", "A4")).upper() == "A4":
        landscape = str(page_cfg.get("orientation", "portrait")).lower() == "landscape"
        section.page_width = Mm(297) if landscape else Mm(210)
        section.page_height = Mm(210) if landscape else Mm(297)
    margins = page_cfg.get("margins_mm") or {}
    for key, attr in (("top", "top_margin"), ("bottom", "bottom_margin"),
                      ("left", "left_margin"), ("right", "right_margin")):
        value = margins.get(key)
        if isinstance(value, (int, float)):
            setattr(section, attr, Mm(float(value)))


def _append_field(paragraph, instruction: str, placeholder: str = "") -> None:
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    run._r.append(begin)

    instr_run = paragraph.add_run()
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = instruction
    instr_run._r.append(instr)

    separate_run = paragraph.add_run()
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    separate_run._r.append(separate)

    if placeholder:
        paragraph.add_run(placeholder)
    end_run = paragraph.add_run()
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    end_run._r.append(end)


def _clear_footer(footer) -> None:
    for child in list(footer._element):
        footer._element.remove(child)


def _insert_page_number_field(section) -> None:
    footer = section.footer
    try:
        footer.is_linked_to_previous = False
    except Exception:  # noqa: BLE001
        pass
    _clear_footer(footer)
    paragraph = footer.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _append_field(paragraph, " PAGE ", "1")
    for run in paragraph.runs:
        _apply_run_font(run, {"font": {}, "font_size_pt": 10.5, "bold": False}, with_bold=False)
        run.font.name = "宋体"
        rpr = run._element.rPr
        if rpr is not None:
            rfonts = _get_or_add_rfonts(rpr)
            rfonts.set(qn("w:eastAsia"), "宋体")


def _looks_like_toc_entry(paragraph) -> bool:
    text = paragraph.text or ""
    if not text.strip():
        return False
    try:
        style_name = (paragraph.style.name or "").lower() if paragraph.style is not None else ""
    except Exception:  # noqa: BLE001
        style_name = ""
    if "toc" in style_name:
        return True
    stripped = text.strip()
    if len(stripped) > 120:
        return False
    if re.search(r"(\t|\.{2,}|…{2,}|·{3,})\s*\d+\s*$", text):
        return True
    return False


def _replace_toc_field(document, heading_paragraph) -> None:
    body = document.element.body
    current = heading_paragraph._p.getnext()
    while current is not None and current.tag == qn("w:p"):
        from docx.text.paragraph import Paragraph

        candidate = Paragraph(current, heading_paragraph._parent)
        if _paragraph_has_field(candidate, "TOC") or _looks_like_toc_entry(candidate):
            following = current.getnext()
            body.remove(current)
            current = following
            continue
        break
    new_paragraph = OxmlElement("w:p")
    heading_paragraph._p.addnext(new_paragraph)
    from docx.text.paragraph import Paragraph

    paragraph = Paragraph(new_paragraph, heading_paragraph._parent)
    _append_field(paragraph, ' TOC \\o "1-3" \\h \\z \\u ', "")


def _cleanup_whitespace(document) -> tuple[int, int]:
    trailing = 0
    for paragraph in document.paragraphs:
        runs = list(paragraph.runs)
        for run in reversed(runs):
            text = run.text or ""
            if not text:
                continue
            stripped = text.rstrip(" \t\u3000")
            if stripped != text:
                run.text = stripped
                trailing += 1
            if stripped:
                break
    removed_blank = 0
    paragraphs = list(document.paragraphs)
    run_start = None
    run_length = 0
    to_remove = []
    for index, paragraph in enumerate(paragraphs):
        if _is_blank_paragraph(paragraph):
            if run_start is None:
                run_start = index
                run_length = 0
            run_length += 1
        else:
            if run_start is not None:
                for offset in range(2, run_length):
                    to_remove.append(paragraphs[run_start + offset])
            run_start = None
            run_length = 0
    if run_start is not None:
        for offset in range(2, run_length):
            to_remove.append(paragraphs[run_start + offset])
    for paragraph in to_remove:
        element = paragraph._p
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
            removed_blank += 1
    return trailing, removed_blank


# ---------------------------------------------------------------------------
# 执行后校验与报告
# ---------------------------------------------------------------------------

def _normalized_text_sequence(document) -> list[str]:
    texts = []
    for paragraph in document.paragraphs:
        normalized = re.sub(r"\s+", "", paragraph.text or "")
        if normalized:
            texts.append(normalized)
    return texts


def _object_counts(document) -> dict:
    body = document.element.body
    return {
        "drawing": len(body.xpath(".//w:drawing")),
        "pict": len(body.xpath(".//w:pict")),
        "tables": len(body.xpath(".//w:tbl")),
        "omml": len(body.xpath(".//m:oMath")),
        "oMathPara": len(body.xpath(".//m:oMathPara")),
    }


def _describe_plan(plan: list[dict]) -> list[str]:
    counters: dict[tuple[str, str], int] = {}
    toc = 0
    for operation in plan:
        op = operation["op"]
        if op in ("apply_paragraph_style", "normalize_run_font"):
            key = (op, operation.get("style_key", ""))
            counters[key] = counters.get(key, 0) + 1
        elif op == "set_section_layout":
            counters[(op, "page")] = counters.get((op, "page"), 0) + 1
        elif op == "insert_page_number_field":
            counters[(op, "")] = counters.get((op, ""), 0) + 1
        elif op == "replace_toc_field":
            toc += 1
        elif op == "cleanup_whitespace":
            counters[(op, "")] = counters.get((op, ""), 0) + 1
    lines = []
    for (op, key), count in counters.items():
        label = "应用段落格式" if op == "apply_paragraph_style" else (
            "规范化正文字体" if op == "normalize_run_font" else (
                "设置节页面与页边距" if op == "set_section_layout" else (
                    "插入居中页码域" if op == "insert_page_number_field" else "清理空白")))
        suffix = f"（{key}）" if key and op != "set_section_layout" else ""
        lines.append(f"{label}{suffix}：{count} 处")
    if toc:
        lines.append("替换目录 TOC 域：1 处（需在 Word/WPS 中按 F9 更新目录）")
    return lines


def _add_heading(document, text: str) -> None:
    paragraph = document.add_paragraph()
    run = paragraph.add_run(text)
    run.font.bold = True
    run.font.size = Pt(14)
    rpr = run._element.rPr
    if rpr is not None:
        rfonts = _get_or_add_rfonts(rpr)
        rfonts.set(qn("w:eastAsia"), "黑体")


def _build_report_docx(job, operations_payload: dict, issues: list[dict], checks: list[str],
                       applied_descriptions: list[str], report_path: Path) -> None:
    document = Document()
    title_paragraph = document.add_paragraph()
    title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title_run = title_paragraph.add_run("论文格式检查报告")
    title_run.font.bold = True
    title_run.font.size = Pt(18)
    rpr = title_run._element.rPr
    if rpr is not None:
        rfonts = _get_or_add_rfonts(rpr)
        rfonts.set(qn("w:eastAsia"), "黑体")

    profile = operations_payload.get("profile") or {}
    profile_source = operations_payload.get("profile_source", "builtin")
    source_label = "内置通用模板" if profile_source == "builtin" else "上传模板（与内置模板合并）"
    source_label += f" / {profile.get('display_name', profile.get('profile_id', ''))}"

    _add_heading(document, "一、基本信息")
    basic_lines = [
        f"原文件名：{job['filename']}",
        f"SHA256：{job['sha256']}",
        f"模板来源：{source_label}",
        f"profile_digest：{job['profile_digest']}",
        f"检查时间：{operations_payload.get('checked_at', '')}",
        f"执行时间：{db.now()}",
        f"模型角色判定：{operations_payload.get('model_note', '')}",
    ]
    for line in basic_lines:
        document.add_paragraph(line)

    _add_heading(document, f"二、问题清单（共 {len(issues)} 项）")
    if issues:
        table = document.add_table(rows=1, cols=7)
        try:
            table.style = "Table Grid"
        except Exception:  # noqa: BLE001
            pass
        headers = ["序号", "code", "位置", "属性", "期望", "实际", "说明"]
        for index, header in enumerate(headers):
            cell = table.rows[0].cells[index]
            cell.text = header
            for paragraph in cell.paragraphs:
                for run in paragraph.runs:
                    run.font.bold = True
        for number, issue in enumerate(issues, start=1):
            cells = table.add_row().cells
            values = [
                str(number), issue.get("code", ""), issue.get("target", ""),
                issue.get("property", ""), issue.get("expected", ""),
                issue.get("actual", ""), issue.get("message", ""),
            ]
            for index, value in enumerate(values):
                cells[index].text = str(value)
    else:
        document.add_paragraph("未发现问题。")

    _add_heading(document, "三、已执行操作")
    if applied_descriptions:
        for line in applied_descriptions:
            document.add_paragraph(line)
    else:
        document.add_paragraph("无。")

    _add_heading(document, "四、执行后校验结果")
    for line in checks:
        document.add_paragraph(line)

    _add_heading(document, "五、已知限制")
    limitations = list(_KNOWN_LIMITATIONS)
    if operations_payload.get("has_toc_heading"):
        limitations.append("目录域已写入，需在 Word/WPS 中按 F9 更新目录内容。")
    if not operations_payload.get("model_participated"):
        limitations.append("模型未参与角色判定，问题清单仅含程序确定性检查结果。")
    if operations_payload.get("paragraph_summaries_truncated"):
        limitations.append(f"文档较长，模型角色判定仅覆盖前 {_MODEL_SUMMARY_MAX_PARAGRAPHS} 段。")
    if operations_payload.get("needs_review"):
        limitations.append("模型给出的待确认项：" + json.dumps(
            operations_payload["needs_review"], ensure_ascii=False)[:1000])
    for line in limitations:
        document.add_paragraph(line)

    document.save(str(report_path))


# ---------------------------------------------------------------------------
# 对外接口：execute
# ---------------------------------------------------------------------------

def _apply_plan(document, plan: list[dict], profile: dict) -> tuple[list[str], int, int]:
    styles_cfg = profile["styles"]
    page_cfg = profile["page"]
    paragraphs = list(document.paragraphs)
    sections = list(document.sections)
    applied: list[str] = []
    trailing_cleaned = 0
    blank_removed = 0
    for operation in plan:
        op = operation.get("op")
        target = operation.get("target", "")
        if op == "apply_paragraph_style":
            index = _target_paragraph_index(target)
            style_key = operation.get("style_key", "")
            if index is None or not (0 <= index < len(paragraphs)) or style_key not in styles_cfg:
                continue
            _apply_paragraph_format(paragraphs[index], styles_cfg[style_key])
            if style_key.startswith("heading"):
                _ensure_heading_style(paragraphs[index], style_key)
            applied.append(f"apply_paragraph_style:{target}:{style_key}")
        elif op == "normalize_run_font":
            index = _target_paragraph_index(target)
            style_key = operation.get("style_key", "")
            if index is None or not (0 <= index < len(paragraphs)) or style_key not in styles_cfg:
                continue
            _normalize_runs(paragraphs[index], styles_cfg[style_key])
            applied.append(f"normalize_run_font:{target}:{style_key}")
        elif op == "set_section_layout":
            index = _target_section_index(target)
            if index is None or not (0 <= index < len(sections)):
                continue
            _set_section_layout(sections[index], page_cfg)
            applied.append(f"set_section_layout:{target}")
        elif op == "insert_page_number_field":
            index = _target_section_index(target)
            if index is None or not (0 <= index < len(sections)):
                continue
            _insert_page_number_field(sections[index])
            applied.append(f"insert_page_number_field:{target}")
        elif op == "replace_toc_field":
            index = _target_paragraph_index(target)
            if index is None or not (0 <= index < len(paragraphs)):
                continue
            _replace_toc_field(document, paragraphs[index])
            applied.append(f"replace_toc_field:{target}")
        elif op == "cleanup_whitespace":
            trailing_cleaned, blank_removed = _cleanup_whitespace(document)
            applied.append("cleanup_whitespace")
    return applied, trailing_cleaned, blank_removed


def _target_paragraph_index(target: str) -> int | None:
    match = re.fullmatch(r"p(\d+)", target or "")
    if not match:
        return None
    return int(match.group(1)) - 1


def _target_section_index(target: str) -> int | None:
    match = re.fullmatch(r"section:(\d+)", target or "")
    if not match:
        return None
    return int(match.group(1)) - 1


def execute(paper_id: str) -> dict:
    job = _require_job(paper_id)
    if job["status"] == "uploaded" or not job["operations"] or not job["issues"]:
        raise AppError("paper_check_required", "请先完成格式检查再执行修正。")
    path = Path(job["original_path"])
    if not path.is_file():
        raise AppError("invalid_request", "论文原稿不存在。")
    if _sha256_file(path) != job["sha256"]:
        raise AppError("paper_version_mismatch", "论文原稿已变更，请重新上传并检查。")
    try:
        operations_payload = json.loads(job["operations"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise AppError("paper_check_required", "检查结果损坏，请重新检查。") from exc
    profile = operations_payload.get("profile")
    plan = operations_payload.get("plan")
    if not isinstance(profile, dict) or not isinstance(plan, list):
        raise AppError("paper_check_required", "检查结果损坏，请重新检查。")
    issues = []
    try:
        loaded_issues = json.loads(job["issues"] or "[]")
        if isinstance(loaded_issues, list):
            issues = loaded_issues
    except (TypeError, json.JSONDecodeError):
        issues = []

    try:
        original = Document(str(path))
    except Exception as exc:  # noqa: BLE001
        raise AppError("invalid_request", f"无法解析 DOCX 文件：{exc}") from exc
    baseline_texts = _normalized_text_sequence(original)
    baseline_objects = _object_counts(original)
    baseline_sections = len(original.sections)

    applied, trailing_cleaned, blank_removed = _apply_plan(original, plan, profile)

    config.ensure_dirs()
    corrected_docx = config.export_dir() / f"{paper_id}_corrected.docx"
    corrected_pdf = config.export_dir() / f"{paper_id}_corrected.pdf"
    report_pdf = config.export_dir() / f"{paper_id}_report.pdf"
    report_docx = config.tmp_dir() / f"{paper_id}_report.docx"
    original.save(str(corrected_docx))

    # 执行后校验：段落文本序列（去空白）、对象数量、节数
    checks: list[str] = []
    try:
        corrected = Document(str(corrected_docx))
    except Exception as exc:  # noqa: BLE001
        checks.append(f"重新打开修正版失败：{exc}")
        _update_job(paper_id, status="failed", checks=json.dumps(checks, ensure_ascii=False),
                    corrected_docx=None, corrected_pdf=None, report_pdf=None)
        return {"status": "failed", "checks": checks}
    corrected_texts = _normalized_text_sequence(corrected)
    corrected_objects = _object_counts(corrected)
    corrected_sections = len(corrected.sections)
    texts_ok = corrected_texts == baseline_texts
    objects_ok = corrected_objects == baseline_objects
    sections_ok = corrected_sections == baseline_sections
    checks.append(
        "段落文本序列（去空白）与原稿一致：" + ("通过" if texts_ok else "不一致")
        + f"（原稿 {len(baseline_texts)} 段 / 修正版 {len(corrected_texts)} 段）"
    )
    checks.append(
        "图片/表格/内联公式对象数量与原稿一致：" + ("通过" if objects_ok else "不一致")
        + f"（原稿 {baseline_objects} / 修正版 {corrected_objects}）"
    )
    checks.append(
        "节数与原稿一致：" + ("通过" if sections_ok else "不一致")
        + f"（原稿 {baseline_sections} 节 / 修正版 {corrected_sections} 节）"
    )
    checks.append(f"空白清理：行尾空格 {trailing_cleaned} 处；删除连续空段落 {blank_removed} 个。")
    if not (texts_ok and objects_ok and sections_ok):
        try:
            corrected_docx.unlink()
        except OSError:
            pass
        _update_job(paper_id, status="failed", checks=json.dumps(checks, ensure_ascii=False),
                    corrected_docx=None, corrected_pdf=None, report_pdf=None)
        return {"status": "failed", "checks": checks}

    import officedoc

    applied_descriptions = _describe_plan(plan)
    try:
        officedoc.docx_to_pdf(str(corrected_docx), str(corrected_pdf))
    except AppError:
        _update_job(paper_id, status="failed", checks=json.dumps(
            checks + ["修正版 PDF 转换失败"], ensure_ascii=False))
        raise

    operations_payload["applied"] = applied
    operations_payload["applied_descriptions"] = applied_descriptions
    operations_payload["executed_at"] = db.now()
    operations_payload["checks"] = checks
    _build_report_docx(job, operations_payload, issues, checks, applied_descriptions, report_docx)
    try:
        officedoc.docx_to_pdf(str(report_docx), str(report_pdf))
    except AppError:
        _update_job(paper_id, status="failed", checks=json.dumps(
            checks + ["检查报告 PDF 转换失败"], ensure_ascii=False))
        raise
    finally:
        try:
            if report_docx.exists():
                report_docx.unlink()
        except OSError:
            pass

    _update_job(
        paper_id,
        status="corrected",
        operations=json.dumps(operations_payload, ensure_ascii=False),
        checks=json.dumps(checks, ensure_ascii=False),
        corrected_docx=str(corrected_docx.resolve()),
        corrected_pdf=str(corrected_pdf.resolve()),
        report_pdf=str(report_pdf.resolve()),
    )
    return {
        "status": "corrected",
        "corrected_docx": str(corrected_docx.resolve()),
        "corrected_pdf": str(corrected_pdf.resolve()),
        "report_pdf": str(report_pdf.resolve()),
        "checks": checks,
    }
