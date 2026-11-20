"""解析文本质量评分（纯标准库）。

评分只用于暴露明显损坏的解析结果，不改变资料格式支持范围。
"""
from __future__ import annotations

import re

import config

_CJK_RANGES = (
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xF900, 0xFAFF),
    (0x20000, 0x2FA1F),
)
_JUNK_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}
_MEANINGFUL_PUNCT = set(
    "，。！？；：、“”‘’（）《》【】〈〉…—·"
    ",.!?;:'\"()[]{}<>-_/@#%&*+=~`\\|"
)
_REPEAT_RE = re.compile(r"([^\s])\1{5,}")


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def _is_junk(ch: str) -> bool:
    code = ord(ch)
    if ch == "\ufffd" or ch in _JUNK_ZERO_WIDTH:
        return True
    if code < 32 and ch not in "\n\r\t":
        return True
    return False


def _is_meaningful(ch: str) -> bool:
    if ch.isspace():
        return True
    if _is_cjk(ch) or ch.isalnum():
        return True
    return ch in _MEANINGFUL_PUNCT


def score_text(text: str) -> tuple[float, str]:
    """返回 (0~1 分, 原因说明)。空文本直接返回 0.0 / 空文本。"""
    value = text or ""
    length = len(value)
    if length == 0:
        return 0.0, "空文本"

    junk_count = sum(1 for ch in value if _is_junk(ch))
    junk_ratio = junk_count / length

    garbage_count = sum(len(match.group(0)) for match in _REPEAT_RE.finditer(value))
    garbage_ratio = garbage_count / length

    meaningful_count = sum(1 for ch in value if _is_meaningful(ch))
    meaningful = meaningful_count / length

    text_count = sum(1 for ch in value if _is_cjk(ch) or ch.isalnum())
    text_ratio = text_count / length

    score = 1.0
    score -= min(0.60, junk_ratio * 40)
    score -= min(0.30, garbage_ratio * 3)
    if meaningful < 1.0:
        score -= min(0.35, (1.0 - meaningful) * 0.5)
    if length > 120 and meaningful < 0.35:
        score -= 0.20
    if length > 80 and text_ratio < 0.12:
        score -= 0.25
    score = round(max(0.0, min(1.0, score)), 3)

    if junk_ratio > 0.005:
        note = f"含 {junk_ratio * 100:.1f}% 无法识别字符"
    elif garbage_ratio > 0.10:
        note = "含大量重复符号（可能是表格线或乱码）"
    elif length > 80 and text_ratio < 0.12:
        note = "几乎不含文字（可能是表格线或乱码）"
    elif meaningful < 0.5:
        note = "可读字符比例偏低"
    elif score < 0.85:
        note = "文本噪声偏多"
    else:
        note = ""
    return score, note


def score_chunks(chunks: list[dict]) -> tuple[float, str]:
    """按 chunk 字符数加权平均，并去重汇总最多 3 条说明。"""
    total_weight = 0
    weighted_sum = 0.0
    notes: list[str] = []
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("text") or "")
        if not text:
            continue
        weight = len(text)
        score, note = score_text(text)
        total_weight += weight
        weighted_sum += score * weight
        if note and note not in notes:
            notes.append(note)
    if total_weight == 0:
        return 0.0, "空文本"
    return round(weighted_sum / total_weight, 3), "；".join(notes[:3])


def is_low(score: float, threshold: float | None = None) -> bool:
    limit = config.QUALITY_LOW_SCORE if threshold is None else threshold
    return float(score) < float(limit)