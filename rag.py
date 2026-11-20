"""RAG 管线：资料解析（PDF/DOCX/PPTX/MD/TXT/图片 OCR）、切块、FTS5 bigram 索引、检索、证据包。

对外接口见 docs/模块接口约定.md，解析/切块/检索参数见规格第 7 节：
- parse_and_index(document_id) / reindex(document_id) / delete_document_data(document_id)
- retrieve(zone_id, query, top_k=None, final=None)
- build_evidence(zone_id, conversation_id, items) -> (evidence_pack, mapping)
- corpus_revision(zone_id) / snippet(text, max_len=200)

约定：
- 上传文件落盘路径：``config.UPLOAD_DIR / f"{document_id}{ext}"``（app.py 负责落盘）。
- ``heading_path`` 用 ``" > "`` 连接祖先标题（与 docs/architecture.md 示例一致），
  DOCX 识别 Heading 1..3 / 标题 1..3，MD 识别 ``#``，PPTX 取每页标题，PDF/TXT 为 None。
- 数据库只经 db.q/q1/x/xmany 访问；错误使用 llm.AppError；不调用模型。
"""
from __future__ import annotations

import hashlib
import logging
import re
import sqlite3
import tempfile
from pathlib import Path
from threading import Lock

import config
import db
import llm
import quality

# 规格固定的 warning 文案（不得改写）
WARNING_OCR_USED = "OCR 已用于扫描页"
WARNING_OCR_PAGE_LIMIT = "扫描件超过 30 页，仅索引前 30 页"

_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
PAGE_PREVIEW_EXTS = {".pdf", ".pptx"}
PAGE_INDEX_VERSION = 1
PDF_LOCK = Lock()
_MAX_ERROR_CHARS = 500
_TRUNCATE_MARK = "[...]"

_WS_RE = re.compile(r"\s+")
_ASCII_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_MD_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")
_MD_FENCE_RE = re.compile(r"^(```|~~~)")
_HEADING_STYLE_RE = re.compile(r"^(?:heading|标题)\s*([1-6])$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF        # CJK 扩展 A
        or 0x4E00 <= code <= 0x9FFF     # CJK 基本区
        or 0xF900 <= code <= 0xFAFF     # CJK 兼容区
        or 0x20000 <= code <= 0x2FA1F   # CJK 扩展 B+
    )


def _cjk_bigrams(run: str) -> list[str]:
    if len(run) <= 1:
        return [run]
    return [run[i:i + 2] for i in range(len(run) - 1)]


def _bigram_tokens(text: str) -> list[str]:
    """连续 CJK 段 → 相邻两字 bigram；非 CJK 段原样保留并转小写。"""
    tokens: list[str] = []
    cjk: list[str] = []
    other: list[str] = []

    def flush_cjk() -> None:
        if cjk:
            tokens.extend(_cjk_bigrams("".join(cjk)))
            cjk.clear()

    def flush_other() -> None:
        if other:
            tokens.append("".join(other).lower())
            other.clear()

    for ch in text:
        if _is_cjk(ch):
            flush_other()
            cjk.append(ch)
        else:
            flush_cjk()
            other.append(ch)
    flush_cjk()
    flush_other()
    return tokens


def _bigram_text(text: str) -> str:
    return " ".join(_bigram_tokens(text))


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()
    for token in _bigram_tokens(query or ""):
        token = token.strip()
        if not token:
            continue
        if _is_cjk(token[0]):
            candidates = [token]
        else:
            candidates = [w.lower() for w in _ASCII_WORD_RE.findall(token)]
        for term in candidates:
            term = term.strip()
            if term and term not in seen:
                seen.add(term)
                terms.append(term)
    return terms


def _fts_query(terms: list[str]) -> str:
    quoted = []
    for term in terms:
        quoted.append('"' + term.replace('"', '""') + '"')
    return " OR ".join(quoted)


# 疑问词/口语套话不参与检索，避免“什么/怎么/请帮我”把无关页面顶上来。
_SEARCH_STOP_PHRASES = sorted({
    "请帮我", "请", "帮我", "告诉我", "我想知道", "想问", "是什么", "为什么", "为何",
    "怎么样", "怎么", "怎样", "如何", "有哪些", "有什么", "什么样", "什么",
    "哪些", "哪个", "多少", "是否", "能否", "可以", "一下", "讲一下", "介绍一下",
    "解释", "说明", "描述", "讲", "的", "了", "吗", "呢", "吧", "啊", "呀",
}, key=len, reverse=True)

# 课程领域同义词：只放指向明确的术语，避免“性质/关系”等泛词过度扩张。
_SEARCH_SYNONYMS = {
    "导数": ["求导", "微分", "变化率"],
    "求导": ["导数", "微分"],
    "微分": ["导数", "求导"],
    "切线": ["切线斜率", "导数几何意义"],
    "积分": ["定积分", "原函数"],
    "定积分": ["积分", "原函数", "牛顿莱布尼茨"],
    "链式法则": ["复合函数求导"],
    "极限": ["趋近", "lim"],
    "连续": ["连续性"],
    "可导": ["可微", "导数存在"],
    "二叉树": ["树", "节点", "子树"],
    "二叉搜索树": ["二叉排序树", "BST"],
    "中序遍历": ["中序", "左根右"],
    "前序遍历": ["前序", "根左右"],
    "后序遍历": ["后序", "左右根"],
    "遍历": ["前序", "中序", "后序"],
    "时间复杂度": ["复杂度", "大O", "增长量级"],
    "复杂度": ["时间复杂度", "空间复杂度", "大O"],
    "二分查找": ["折半查找", "logn"],
    "线性时不变": ["LTI", "时不变系统"],
    "卷积": ["卷积积分", "冲激响应"],
    "傅里叶": ["傅氏", "频谱"],
    "傅里叶变换": ["傅氏变换", "频谱"],
    "拉普拉斯": ["拉氏", "s域"],
    "z变换": ["Z变换", "z域"],
    "采样": ["抽样", "采样定理"],
    "混叠": ["混叠失真", "奈奎斯特"],
    "奈奎斯特": ["采样定理", "2ωm"],
    "因果": ["因果系统", "causal"],
    "稳定": ["稳定性", "稳定系统"],
    "收敛域": ["ROC"],
    "方波": ["矩形脉冲", "rect"],
    "矩形脉冲": ["rect", "方波"],
    "正弦": ["sin"],
    "余弦": ["cos"],
    "单位冲激": ["δ(t)", "冲激函数"],
    "单位阶跃": ["u(t)", "阶跃函数"],
    "变换对": ["傅里叶变换对", "拉普拉斯变换对"],
}

_GENERIC_SEARCH_TERMS = {
    "定义", "证明", "性质", "例子", "举例", "含义", "概念", "特点", "作用",
    "方法", "步骤", "条件", "应用", "分类", "总结", "对比", "区别", "比较",
    "介绍", "说明", "公式", "推导", "计算", "题目", "例题", "要点",
}
_TABLE_QUERY_WORDS = ("性质", "定理", "公式", "对比", "区别", "关系", "表", "清单", "总结", "变换对", "分类", "有哪些")
_TERM_SPLIT_RE = re.compile(r"[\s,，。？！、；：\"'（）()《》<>\[\]{}]+")


def _clean_question(query: str) -> str:
    text = (query or "").strip()
    for phrase in _SEARCH_STOP_PHRASES:
        text = text.replace(phrase, " ")
    fragments = [part.strip() for part in _TERM_SPLIT_RE.split(text) if part.strip()]
    fragments = [part for part in fragments if len(part) >= 2 or _ASCII_WORD_RE.fullmatch(part)]
    if not fragments:
        return (query or "").strip()
    return " ".join(fragments[:8])


def _expand_terms(terms: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        candidates = [term]
        candidates.extend(_SEARCH_SYNONYMS.get(term, []))
        for key, synonyms in _SEARCH_SYNONYMS.items():
            if key != term and key in term:
                candidates.extend(synonyms)
                for synonym in synonyms:
                    if key in term:
                        candidates.append(term.replace(key, synonym))
        for candidate in candidates:
            candidate = candidate.strip()
            if candidate and candidate.lower() not in seen:
                seen.add(candidate.lower())
                out.append(candidate)
    return out[:18]


def _search_terms(query: str) -> tuple[str, list[str], list[str]]:
    cleaned = _clean_question(query)
    base_terms = [part for part in cleaned.split() if part]
    expanded = _expand_terms(base_terms or [cleaned])
    return cleaned, base_terms, expanded


def _fts_terms(cleaned: str, expanded: list[str]) -> list[str]:
    source_parts = [cleaned] + expanded
    terms: list[str] = []
    seen: set[str] = set()
    for part in source_parts:
        for term in _query_terms(part):
            term = term.strip()
            if not term:
                continue
            if not _is_cjk(term[0]) and len(term) < 2:
                continue
            if term not in seen:
                seen.add(term)
                terms.append(term)
    return terms[:120]


def _wants_table(query: str) -> bool:
    return any(word in (query or "") for word in _TABLE_QUERY_WORDS)


def _is_tableish(text: str) -> bool:
    if not text:
        return False
    lines = text.splitlines()
    if sum(1 for line in lines if "|" in line) >= 3:
        return True
    if text.count(" | ") >= 3:
        return True
    return bool(re.search(r"(时域|频域|性质|定理)[^\n]{0,20}(→|:|：)", text))


def _matched_title_terms(title: str, heading: str, terms: list[str]) -> int:
    haystack = ((title or "") + " " + (heading or "")).lower()
    return sum(1 for term in terms if term and term.lower() in haystack)


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    haystack = (text or "").lower()
    return [term for term in terms if term and term.lower() in haystack]


def _normalize_ext(ext: str | None) -> str:
    value = (ext or "").strip().lower()
    if value and not value.startswith("."):
        value = "." + value
    return value


def _error_summary(exc: BaseException) -> str:
    detail = str(exc).strip()
    text = f"{exc.__class__.__name__}: {detail}" if detail else exc.__class__.__name__
    return _WS_RE.sub(" ", text).strip()[:_MAX_ERROR_CHARS]


def _read_upload_text(path: Path) -> str:
    """UTF-8 读取，失败试 GBK，再失败抛 document_parse_failed。"""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise llm.AppError("document_parse_failed", f"读取文件失败：{exc}") from exc
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding).lstrip("\ufeff")
        except UnicodeDecodeError:
            continue
    raise llm.AppError("document_parse_failed", "文本编码无法识别（已尝试 UTF-8 / GBK）。")


def _resolve_upload_path(document_id: str, ext: str) -> Path:
    """上传文件落盘约定（app.py 落盘）：``UPLOAD_DIR / f"{document_id}{ext}"``。"""
    return config.upload_dir() / f"{document_id}{ext}"


# ---------------------------------------------------------------------------
# OCR（rapidocr-onnxruntime，延迟导入：避免未安装依赖时模块导入失败）
# ---------------------------------------------------------------------------

_ocr_engine = None


def _get_ocr_engine():
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError as exc:  # pragma: no cover - 依赖缺失时由 parse 失败兜底
            raise llm.AppError("document_parse_failed", f"OCR 组件不可用：{exc}") from exc
        try:
            _ocr_engine = RapidOCR()
        except Exception as exc:  # pragma: no cover
            raise llm.AppError("document_parse_failed", f"OCR 初始化失败：{exc}") from exc
    return _ocr_engine


def _ocr_result_to_text(result) -> str:
    """把 rapidocr 结果（[[box, text, score], ...]）拼成文本。"""
    if not result:
        return ""
    lines: list[str] = []
    for item in result:
        text = ""
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            text = item[1]
        elif isinstance(item, dict):
            text = item.get("txt") or item.get("text") or ""
        text = str(text or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines).strip()


def _ocr_image_file(image_path: Path) -> str:
    """对图片文件做 OCR；结果为空返回空串。"""
    engine = _get_ocr_engine()
    try:
        output = engine(str(image_path))
    except Exception as exc:
        raise llm.AppError("document_parse_failed", f"OCR 识别失败：{exc}") from exc
    result = output[0] if isinstance(output, tuple) else output
    return _ocr_result_to_text(result)


def _pixmap_png_bytes(page) -> bytes:
    """PDF 页按 200dpi 渲染为 PNG 字节。"""
    try:
        pix = page.get_pixmap(dpi=200)
    except TypeError:  # 旧版 PyMuPDF 无 dpi 参数
        pix = page.get_pixmap(matrix=_fitz().Matrix(200 / 72, 200 / 72))
    return pix.tobytes("png")


def _ocr_pdf_page(page) -> str:
    """渲染 PDF 页到临时 PNG → OCR → 清理临时文件。"""
    config.ensure_dirs()
    fd, tmp_name = tempfile.mkstemp(prefix="ocr_", suffix=".png", dir=config.tmp_dir())
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "wb") as handle:
            handle.write(_pixmap_png_bytes(page))
        return _ocr_image_file(tmp_path)
    finally:
        try:
            tmp_path.unlink()
        except OSError:
            pass


def _fitz():
    """延迟导入 PyMuPDF；新旧包名兼容。"""
    try:
        import pymupdf as fitz  # PyMuPDF >= 1.24 推荐
    except ImportError:  # pragma: no cover
        import fitz  # type: ignore
    return fitz


def pdf_page_preview(path: Path, page_number: int) -> bytes:
    fitz = _fitz()
    with PDF_LOCK, fitz.open(str(path)) as document:
        if not 1 <= page_number <= document.page_count:
            raise llm.AppError("page_not_found", "原文件中没有这页。")
        page = document[page_number - 1]
        zoom = min(2.5, 1440 / max(page.rect.width, page.rect.height))
        return page.get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")


# ---------------------------------------------------------------------------
# 各格式解析：返回 {"sections": list[dict], "page_count": int|None, "warning": str|None}
# section: {"heading_path": str|None, "page": int|None, "text": str}
# ---------------------------------------------------------------------------

def _paragraph_heading_level(paragraph) -> int:
    """DOCX 标题段落：style 名匹配 Heading 1..3 / 标题 1..3（规格只识别 1..3）。"""
    try:
        style = paragraph.style
    except Exception:  # pragma: no cover - 样式损坏时按正文处理
        return 0
    name = (getattr(style, "name", "") or "").strip()
    match = _HEADING_STYLE_RE.match(name)
    if not match:
        return 0
    level = int(match.group(1))
    return level if 1 <= level <= 3 else 0


def _parse_docx(path: Path) -> dict:
    try:
        from docx import Document
    except ImportError as exc:  # pragma: no cover
        raise llm.AppError("document_parse_failed", f"DOCX 解析组件不可用：{exc}") from exc
    try:
        document = Document(str(path))
    except Exception as exc:
        raise llm.AppError("document_parse_failed", f"DOCX 打开失败：{exc}") from exc

    sections: list[dict] = []
    stack: list[tuple[int, str]] = []      # [(level, title)]
    lines: list[str] = []
    heading: str | None = None
    body_count = 0

    def flush() -> None:
        nonlocal lines, heading, body_count
        if lines and body_count > 0:
            text = "\n".join(lines).strip()
            if text:
                sections.append({"heading_path": heading, "page": None, "text": text})
        lines = []
        heading = None
        body_count = 0

    for paragraph in document.paragraphs:
        text = (paragraph.text or "").strip()
        if not text:
            continue
        level = _paragraph_heading_level(paragraph)
        if level:
            flush()
            stack = [item for item in stack if item[0] < level]
            stack.append((level, text))
            heading = " > ".join(title for _, title in stack)
            lines.append(text)
        else:
            lines.append(text)
            body_count += 1
    flush()
    return {"sections": sections, "page_count": None, "warning": None}


def _md_heading(line: str) -> tuple[int, str] | None:
    match = _MD_HEADING_RE.match(line)
    if not match:
        return None
    title = match.group(2).strip()
    title = re.sub(r"[ \t]+#+[ \t]*$", "", title).strip()
    if not title:
        return None
    return len(match.group(1)), title


def _parse_markdown(text: str) -> dict:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    sections: list[dict] = []
    stack: list[tuple[int, str]] = []
    current: list[str] = []
    heading: str | None = None
    body_count = 0
    in_fence = False

    def flush() -> None:
        nonlocal current, heading, body_count
        if current and body_count > 0:
            body = "\n".join(current).strip()
            if body:
                sections.append({"heading_path": heading, "page": None, "text": body})
        current = []
        heading = None
        body_count = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        stripped = line.strip()
        if _MD_FENCE_RE.match(stripped):
            in_fence = not in_fence
            current.append(line)
            if stripped:
                body_count += 1
            continue
        if not in_fence:
            parsed = _md_heading(line)
            if parsed is not None:
                flush()
                level, title = parsed
                stack = [item for item in stack if item[0] < level]
                stack.append((level, title))
                heading = " > ".join(title for _, title in stack)
                current.append(line)
                continue
        current.append(line)
        if stripped:
            body_count += 1
    flush()
    return {"sections": sections, "page_count": None, "warning": None}


def _parse_pptx(path: Path) -> dict:
    try:
        from pptx import Presentation
    except ImportError as exc:  # pragma: no cover
        raise llm.AppError("document_parse_failed", f"PPTX 解析组件不可用：{exc}") from exc
    try:
        presentation = Presentation(str(path))
    except Exception as exc:
        raise llm.AppError("document_parse_failed", f"PPTX 打开失败：{exc}") from exc

    sections: list[dict] = []
    page_count = 0
    for index, slide in enumerate(presentation.slides, start=1):
        page_count = index
        title = ""
        title_shape = None
        try:
            title_shape = slide.shapes.title
        except Exception:  # pragma: no cover
            title_shape = None
        if title_shape is not None:
            try:
                title = (title_shape.text or "").strip()
            except Exception:  # pragma: no cover
                title = ""
        title_id = getattr(title_shape, "shape_id", None) if title_shape is not None else None

        parts: list[str] = []
        for shape in slide.shapes:
            if title_id is not None and getattr(shape, "shape_id", None) == title_id:
                continue
            try:
                has_text = bool(shape.has_text_frame)
            except Exception:  # pragma: no cover
                has_text = False
            if not has_text:
                continue
            try:
                shape_text = (shape.text_frame.text or "").strip()
            except Exception:  # pragma: no cover
                shape_text = ""
            if shape_text:
                parts.append(shape_text)

        notes = ""
        try:
            if slide.has_notes_slide:
                notes_frame = slide.notes_slide.notes_text_frame
                if notes_frame is not None:
                    notes = (notes_frame.text or "").strip()
        except Exception:  # pragma: no cover - 备注缺失/损坏不影响正文
            notes = ""
        if notes:
            parts.append(notes)

        if not parts and not title:
            continue
        body = "\n".join(parts).strip()
        text = (title + "\n" + body).strip() if title else body
        sections.append({"heading_path": title or None, "page": index, "text": text})
    return {"sections": sections, "page_count": page_count, "warning": None}


def _parse_pdf(path: Path) -> dict:
    with PDF_LOCK:
        return _read_pdf(path)


def _read_pdf(path: Path) -> dict:
    fitz = _fitz()
    try:
        document = fitz.open(str(path))
    except Exception as exc:
        raise llm.AppError("document_parse_failed", f"PDF 打开失败：{exc}") from exc
    try:
        page_count = int(document.page_count or 0)
        pages: list[str] = []
        total_chars = 0
        for page in document:
            text = page.get_text("text") or ""
            pages.append(text)
            total_chars += len(text.strip())

        warning: str | None = None
        if total_chars < 20 and page_count > 0:
            # 扫描件：200dpi 渲染 + OCR，最多前 OCR_MAX_PAGES 页
            limit = min(page_count, int(config.OCR_MAX_PAGES))
            for index in range(limit):
                ocr_text = _ocr_pdf_page(document.load_page(index))
                pages[index] = ocr_text
                if not ocr_text.strip():
                    pages[index] = ""
            if page_count > limit:
                pages = pages[:limit]
                warning = WARNING_OCR_PAGE_LIMIT
            else:
                warning = WARNING_OCR_USED

        sections: list[dict] = []
        for index, text in enumerate(pages):
            text = (text or "").strip()
            if not text:
                continue
            sections.append({"heading_path": None, "page": index + 1, "text": text})
        return {"sections": sections, "page_count": page_count, "warning": warning}
    finally:
        try:
            document.close()
        except Exception:  # pragma: no cover
            pass


def _parse_image(path: Path) -> dict:
    text = _ocr_image_file(path)
    sections: list[dict] = []
    if text.strip():
        sections.append({"heading_path": None, "page": 1, "text": text.strip()})
    return {"sections": sections, "page_count": 1, "warning": None}


def _parse_plain_text(path: Path) -> dict:
    text = _read_upload_text(path)
    text = text.strip()
    sections: list[dict] = []
    if text:
        # TXT 无标题层级：整篇作为一个 section，超长由切块阶段滑窗处理
        sections.append({"heading_path": None, "page": None, "text": text})
    return {"sections": sections, "page_count": None, "warning": None}


def _parse_document(path: Path, ext: str) -> dict:
    if ext == ".md":
        return _parse_markdown(_read_upload_text(path))
    if ext == ".txt":
        return _parse_plain_text(path)
    if ext == ".docx":
        return _parse_docx(path)
    if ext == ".pptx":
        return _parse_pptx(path)
    if ext == ".pdf":
        return _parse_pdf(path)
    if ext in _IMAGE_EXTS:
        return _parse_image(path)
    raise llm.AppError("document_unsupported", f"不支持的文件类型：{ext or '未知'}")


# ---------------------------------------------------------------------------
# 切块：按标题/页边界切 section → <600 合并（合并后不超过 800）→ >800 滑窗 100 overlap
# ---------------------------------------------------------------------------

def _build_chunks(sections: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for section in sections:
        text = str(section.get("text") or "").strip()
        if not text:
            continue
        heading = section.get("heading_path")
        page = section.get("page")
        if merged:
            previous = merged[-1]
            same_heading = (previous.get("heading_path") or "") == (heading or "")
            if (
                same_heading
                and previous.get("page") == page
                and len(previous["text"]) < config.CHUNK_MIN
                and len(previous["text"]) + 1 + len(text) <= config.CHUNK_MAX
            ):
                previous["text"] = previous["text"] + "\n" + text
                continue
        merged.append({"heading_path": heading, "page": page, "text": text})

    chunks: list[dict] = []
    for section in merged:
        text = section["text"]
        if len(text) <= config.CHUNK_MAX:
            chunks.append(section)
            continue
        step = max(1, int(config.CHUNK_MAX) - int(config.CHUNK_OVERLAP))
        start = 0
        while start < len(text):
            piece = text[start:start + int(config.CHUNK_MAX)].strip()
            if piece:
                chunks.append({
                    "heading_path": section.get("heading_path"),
                    "page": section.get("page"),
                    "text": piece,
                })
            if start + int(config.CHUNK_MAX) >= len(text):
                break
            start += step
    return chunks


# ---------------------------------------------------------------------------
# 索引写入 / 清理
# ---------------------------------------------------------------------------

def _delete_chunks(conn: sqlite3.Connection, document_id: str) -> None:
    conn.execute(
        "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)",
        (document_id,),
    )
    conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))


def _write_chunks(conn: sqlite3.Connection, document: dict, chunks: list[dict]) -> None:
    chunk_rows: list[tuple] = []
    fts_rows: list[tuple] = []
    for ordinal, chunk in enumerate(chunks):
        text = chunk["text"]
        chunk_id = db.new_id()
        chunk_rows.append((
            chunk_id,
            document["id"],
            document["zone_id"],
            ordinal,
            chunk.get("heading_path"),
            chunk.get("page"),
            text,
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
        ))
        # FTS 索引 chunk 正文 + 标题上下文（标题有助于“按章节名提问”的召回）
        heading = chunk.get("heading_path") or ""
        index_text = f"{heading}\n{text}" if heading else text
        fts_rows.append((chunk_id, _bigram_text(index_text)))
    conn.executemany(
        "INSERT INTO chunks (id, document_id, zone_id, ordinal, heading_path, page, text, text_sha256)"
        " VALUES (?,?,?,?,?,?,?,?)",
        chunk_rows,
    )
    conn.executemany("INSERT INTO chunks_fts (chunk_id, text_bigram) VALUES (?,?)", fts_rows)


def _mark_failed(document_id: str, error: str) -> dict:
    message = (error or "解析失败。").strip()[:_MAX_ERROR_CHARS]
    quality_note = ("解析失败：" + message)[:500]
    try:
        delete_document_data(document_id)
    except Exception:  # pragma: no cover - 清理失败不掩盖原始解析错误
        pass
    db.x(
        "UPDATE documents SET status = 'failed', error = ?, warning = NULL,"
        " page_count = NULL, chunk_count = 0, page_index_version = 0, quality = 0.0, quality_note = ? WHERE id = ?",
        (message, quality_note, document_id),
    )
    return {
        "status": "failed",
        "warning": None,
        "page_count": None,
        "chunk_count": 0,
        "error": message,
        "quality": 0.0,
        "quality_note": quality_note,
    }


# ---------------------------------------------------------------------------
# 对外接口：解析 / 重建 / 删除
# ---------------------------------------------------------------------------

def _index_parsed_document(document: dict, parsed: dict, *, preserve_sources: bool = False) -> dict:
    chunks = _build_chunks(parsed.get("sections") or [])
    if not chunks:
        raise ValueError("未解析出任何可用文本内容。")
    warning = parsed.get("warning")
    page_count = parsed.get("page_count")
    quality_score, quality_note = quality.score_chunks(chunks)
    status = "ready_with_warning" if warning else "ready"
    page_version = PAGE_INDEX_VERSION if document["ext"] in PAGE_PREVIEW_EXTS else 0
    with db.connect() as conn:
        if preserve_sources:
            conn.execute(
                "INSERT OR IGNORE INTO chunk_source_snapshots (id, document_id, heading_path, page, text, text_sha256)"
                " SELECT id, document_id, heading_path, page, text, text_sha256 FROM chunks WHERE document_id = ?",
                (document["id"],),
            )
        _delete_chunks(conn, document["id"])
        _write_chunks(conn, document, chunks)
        conn.execute(
            "UPDATE documents SET status = ?, error = NULL, warning = ?, page_count = ?, chunk_count = ?,"
            " page_index_version = ?, quality = ?, quality_note = ? WHERE id = ?",
            (status, warning, page_count, len(chunks), page_version, quality_score, quality_note, document["id"]),
        )
    return {
        "status": status, "warning": warning, "page_count": page_count, "chunk_count": len(chunks),
        "error": None, "quality": quality_score, "quality_note": quality_note,
    }


def refresh_page_indexes() -> None:
    """按页重建旧 PDF/PPTX 索引。读取失败时保留旧索引。"""
    documents = db.q(
        "SELECT * FROM documents WHERE ext IN ('.pdf', '.pptx') AND page_index_version < ?"
        " AND status IN ('ready', 'ready_with_warning')",
        (PAGE_INDEX_VERSION,),
    )
    for row in documents:
        document = dict(row)
        try:
            path = _resolve_upload_path(document["id"], document["ext"])
            _index_parsed_document(document, _parse_document(path, document["ext"]), preserve_sources=True)
        except Exception as exc:
            logging.getLogger(__name__).warning("资料 %s 的按页索引更新失败：%s", document["id"], _error_summary(exc))


def parse_and_index(document_id: str) -> dict:
    """读取 documents 行 → 解析 → 切块 → 写入 chunks + chunks_fts → 更新 documents。"""
    row = db.q1("SELECT * FROM documents WHERE id = ?", (document_id,))
    if row is None:
        raise llm.AppError("invalid_request", "文档不存在。")
    document = dict(row)

    ext = _normalize_ext(document.get("ext"))
    if ext not in config.SUPPORTED_DOC_EXTS:
        raise llm.AppError("document_unsupported", f"不支持的文件类型：{ext or '未知'}")

    path = _resolve_upload_path(document_id, ext)
    if not path.exists() or not path.is_file():
        return _mark_failed(document_id, "上传文件不存在，无法解析。")
    try:
        size = path.stat().st_size
    except OSError as exc:
        return _mark_failed(document_id, f"读取文件信息失败：{exc}")
    if size > config.UPLOAD_MAX_BYTES:
        raise llm.AppError("document_too_large", "文件超过 20MB 上限。")

    db.x("UPDATE documents SET status = 'parsing', error = NULL WHERE id = ?", (document_id,))
    try:
        return _index_parsed_document(document, _parse_document(path, ext))
    except Exception as exc:
        return _mark_failed(document_id, _error_summary(exc))


def reindex(document_id: str) -> dict:
    """删除旧 chunks + fts 后重新解析；返回结构与 parse_and_index 一致。"""
    row = db.q1("SELECT id FROM documents WHERE id = ?", (document_id,))
    if row is None:
        raise llm.AppError("invalid_request", "文档不存在。")
    return parse_and_index(document_id)


def delete_document_data(document_id: str) -> None:
    """删除 chunks + chunks_fts 行（上传文件由 app.py 删除）。"""
    with db.connect() as conn:
        _delete_chunks(conn, document_id)


# ---------------------------------------------------------------------------
# 检索
# ---------------------------------------------------------------------------

def retrieve(zone_id: str, query: str, top_k: int | None = None, final: int | None = None,
             document_ids: list[str] | None = None) -> list[dict]:
    """按知识区做 FTS5 bigram 检索，再做标题、表格和同义词感知重排。

    document_ids=None 表示整个知识区；[] 表示节点范围为空，直接返回无结果。
    """
    zone_id = (zone_id or "").strip()
    query = (query or "").strip()
    if not zone_id or not query:
        return []
    cleaned, base_terms, expanded_terms = _search_terms(query)
    fts_terms = _fts_terms(cleaned, expanded_terms)
    if not fts_terms:
        return []
    if document_ids is not None and not document_ids:
        return []

    candidate_limit = int(top_k) if top_k is not None else int(config.RETRIEVE_TOP)
    final_limit = int(final) if final is not None else int(config.RETRIEVE_FINAL)
    if candidate_limit <= 0 or final_limit <= 0:
        return []

    sql = (
        "SELECT chunks.id AS chunk_id, chunks.document_id AS document_id,"
        " chunks.heading_path AS heading_path, chunks.page AS page,"
        " chunks.text AS text, chunks.text_sha256 AS text_sha256,"
        " documents.filename AS filename, documents.sha256 AS document_sha256,"
        " documents.ext, documents.page_index_version,"
        " documents.quality AS doc_quality,"
        " bm25(chunks_fts) AS rank"
        " FROM chunks_fts"
        " JOIN chunks ON chunks.id = chunks_fts.chunk_id"
        " JOIN documents ON documents.id = chunks.document_id"
        " WHERE chunks_fts MATCH ? AND chunks.zone_id = ?"
    )
    params: list = [_fts_query(fts_terms), zone_id]
    if document_ids is not None:
        placeholders = ",".join("?" for _ in document_ids)
        sql += f" AND chunks.document_id IN ({placeholders})"
        params.extend(document_ids)
    sql += " ORDER BY rank ASC LIMIT ?"
    params.append(candidate_limit)

    try:
        rows = db.q(sql, tuple(params))
    except sqlite3.OperationalError:
        # 查询串异常时按“无结果”处理，不把检索错误伪装成命中
        return []

    wants_table = _wants_table(query)
    scoring_terms = [term for term in expanded_terms if term not in _GENERIC_SEARCH_TERMS]
    if not scoring_terms:
        scoring_terms = expanded_terms
    ranked: list[dict] = []
    for position, row in enumerate(rows):
        text = row["text"] or ""
        heading = row["heading_path"] or ""
        filename = row["filename"] or ""
        title_hits = _matched_title_terms(filename, heading, scoring_terms)
        base_hits = _matched_title_terms(filename, heading, base_terms)
        table_hit = bool(wants_table and _is_tableish(text))
        score = 1.0 / (position + 1)
        score += min(base_hits, 3) * 0.32
        score += min(title_hits, 5) * 0.10
        if table_hit:
            score += 0.28
        if wants_table and any(word in heading for word in _TABLE_QUERY_WORDS):
            score += 0.12
        doc_quality = float(row["doc_quality"] if row["doc_quality"] is not None else 1.0)
        if doc_quality < config.QUALITY_LOW_SCORE:
            score *= max(config.QUALITY_RETRIEVE_FLOOR, doc_quality / config.QUALITY_LOW_SCORE)
        ranked.append({
            "chunk_id": row["chunk_id"],
            "document_id": row["document_id"],
            "filename": filename,
            "ext": row["ext"],
            "page_index_version": row["page_index_version"],
            "page": row["page"],
            "heading_path": heading,
            "text": text,
            "text_sha256": row["text_sha256"] or "",
            "document_sha256": row["document_sha256"] or "",
            "quality": round(doc_quality, 3),
            "score": round(score, 6),
            "title_hit": bool(title_hits),
            "table_hit": table_hit,
            "matched_terms": _matched_terms(filename + " " + heading + " " + text, scoring_terms),
        })

    ranked.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    results: list[dict] = []
    seen: set[str] = set()
    for item in ranked:
        text_sha = item["text_sha256"] or item["chunk_id"]
        if text_sha in seen:
            continue
        seen.add(text_sha)
        results.append(item)
        if len(results) >= final_limit:
            break
    return results


# ---------------------------------------------------------------------------
# 证据包 / 版本 / 摘要
# ---------------------------------------------------------------------------

def corpus_revision(zone_id: str) -> str:
    """该知识区“文档数 + 最新 created_at”的 sha256 前 12 位。"""
    row = db.q1(
        "SELECT COUNT(*) AS doc_count, MAX(created_at) AS latest FROM documents WHERE zone_id = ?",
        (zone_id or "",),
    )
    count = int(row["doc_count"] or 0) if row is not None else 0
    latest = (row["latest"] or "") if row is not None else ""
    return hashlib.sha256(f"{count}|{latest}".encode("utf-8")).hexdigest()[:12]


def build_evidence(zone_id: str, conversation_id: str, items: list[dict]) -> tuple[dict, dict]:
    """组装规格第 7 节证据包；返回 (evidence_pack, {evidence_id: item})。"""
    item_list = [item for item in (items or []) if isinstance(item, dict)]

    doc_ids: list[str] = []
    for item in item_list:
        document_id = str(item.get("document_id") or "")
        if document_id and document_id not in doc_ids:
            doc_ids.append(document_id)
    doc_sha: dict[str, str] = {}
    doc_status: dict[str, str] = {}
    if doc_ids:
        placeholders = ",".join("?" for _ in doc_ids)
        for row in db.q(
            f"SELECT id, sha256, status FROM documents WHERE id IN ({placeholders})",
            tuple(doc_ids),
        ):
            doc_sha[row["id"]] = row["sha256"] or ""
            doc_status[row["id"]] = row["status"] or "ready"

    evidence: list[dict] = []
    mapping: dict[str, dict] = {}
    used_chars = 0
    max_chunks = int(config.RETRIEVE_FINAL)

    for item in item_list:
        if len(evidence) >= max_chunks:
            break
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        remaining = int(config.CONTEXT_MAX_CHARS) - used_chars
        if remaining <= 0:
            break
        truncated = False
        if len(text) > remaining:
            room = remaining - len(_TRUNCATE_MARK)
            if room <= 0:
                break
            text = text[:room].rstrip() + _TRUNCATE_MARK
            truncated = True

        evidence_id = f"e{len(evidence) + 1}"
        document_id = str(item.get("document_id") or "")
        evidence.append({
            "evidence_id": evidence_id,
            "document_id": document_id,
            "document_version": doc_sha.get(document_id) or str(item.get("document_sha256") or ""),
            "chunk_id": str(item.get("chunk_id") or ""),
            "content_hash": str(item.get("text_sha256") or ""),
            "locator": {
                "page": item.get("page"),
                "section": str(item.get("heading_path") or ""),
            },
            "text": text,
            "parse_status": doc_status.get(document_id) or "ready",
        })
        mapping[evidence_id] = item
        used_chars += len(text)
        if truncated:
            break

    evidence_pack = {
        "scope": {
            "workspace_id": "local",
            "knowledge_base_id": zone_id or "",
            "conversation_id": conversation_id or "",
            "corpus_revision": corpus_revision(zone_id),
        },
        "evidence": evidence,
        "read_ledger": [],
        "budget": {
            "remaining_search_calls": 0,
            "remaining_chunk_read_calls": 0,
            "max_evidence_chunks": max_chunks,
            "max_evidence_tokens": int(config.MAX_EVIDENCE_TOKENS),
        },
    }
    return evidence_pack, mapping


def snippet(text: str, max_len: int = 200) -> str:
    """压缩空白后截断为摘要文本（用于 citations.snippet）。"""
    compact = _WS_RE.sub(" ", str(text or "")).strip()
    if max_len is None or int(max_len) <= 0:
        return compact
    limit = int(max_len)
    if len(compact) <= limit:
        return compact
    return compact[:limit].rstrip() + "..."
