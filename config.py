"""集中配置与常量。所有路径、默认参数只允许在这里定义。"""
from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
_DATA_OVERRIDE = os.environ.get("ZHIXU_DATA_DIR")
DATA_DIR = Path(_DATA_OVERRIDE) if _DATA_OVERRIDE else BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
EXPORT_DIR = DATA_DIR / "exports"
TMP_DIR = DATA_DIR / "tmp"
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "app.db"
WEB_DIR = BASE_DIR / "web"
SKILLS_DIR = BASE_DIR / "skills"
_store_root: ContextVar[Path | None] = ContextVar("store_root", default=None)


@contextmanager
def use_store(root: Path | None):
    token = _store_root.set(root)
    try:
        yield
    finally:
        _store_root.reset(token)


def db_path() -> Path:
    root = _store_root.get()
    return root / DB_PATH.name if root is not None else DB_PATH


def upload_dir() -> Path:
    root = _store_root.get()
    return root / UPLOAD_DIR.name if root is not None else UPLOAD_DIR


def export_dir() -> Path:
    root = _store_root.get()
    return root / EXPORT_DIR.name if root is not None else EXPORT_DIR


def tmp_dir() -> Path:
    root = _store_root.get()
    return root / TMP_DIR.name if root is not None else TMP_DIR


def account_dir(user_id: str) -> Path:
    return DATA_DIR.resolve() / "accounts" / user_id


def legacy_backup_dir() -> Path:
    return DATA_DIR.resolve() / "auth-backups" / "legacy-v1"

# 本机 8765 被 Windows 系统排除（WSAEACCES），改用空闲端口 8788
PORT = 8788

# 分块
CHUNK_MIN = 600
CHUNK_MAX = 800
CHUNK_OVERLAP = 100

# 检索
RETRIEVE_TOP = 40
RETRIEVE_FINAL = 5
CONTEXT_MAX_CHARS = 4500
MAX_EVIDENCE_TOKENS = 3000

# 模型
CHAT_TEMPERATURE = 0.2
CHAT_MAX_TOKENS = 1200
# 多轮答疑：最近对话只用于理解指代与延续讲解，不作为课件事实来源
CHAT_HISTORY_MAX_MESSAGES = 6
CHAT_HISTORY_MAX_CHARS = 400
CHAT_HISTORY_QUERY_EXTRA_CHARS = 240
# 出题/论文方案输出较长，单独给更大上限（RAG 回答仍用 CHAT_MAX_TOKENS）
QUIZ_MAX_TOKENS = 4000
PAPER_MAX_TOKENS = 4000
MINDMAP_TEMPERATURE = 0.3
MINDMAP_MAX_TOKENS = 4000
NOTE_TITLE_MAX_CHARS = 160
NOTE_BODY_MAX_CHARS = 60000
NOTE_DRAFT_MAX_TOKENS = 3000
NOTE_TRANSCRIPT_MAX_CHARS = 40000
NOTE_RECAP_MAX_TOKENS = 8000

# 解析质量
QUALITY_LOW_SCORE = 0.6          # 低于该分视为低质量
QUALITY_RETRIEVE_FLOOR = 0.5     # 低质量资料检索分最低降到 50%

# 导图
MINDMAP_MAX_NODES = 120
MINDMAP_MAX_DEPTH = 5
MINDMAP_EVIDENCE_PER_DOC = 12
MINDMAP_EVIDENCE_MAX_CHUNKS = 40
MINDMAP_EVIDENCE_MAX_CHARS = 12000
MINDMAP_EDIT_MAX_NODES = 300

# 上传
UPLOAD_MAX_BYTES = 20 * 1024 * 1024
OCR_MAX_PAGES = 30
REQUEST_MAX_BYTES = 25 * 1024 * 1024
API_KEY_ENTROPY = b"zhixu-study-workbench-api-key-v1"

# 出题
QUIZ_COUNT = 6
QUIZ_TYPES = ["single_choice", "multiple_choice", "true_false", "fill_blank", "short_answer", "calculation"]
QUIZ_DIFFICULTIES = ["basic", "basic", "basic", "advanced", "advanced", "comprehensive"]
QUIZ_CONFIGURABLE_TYPES = ["single_choice", "multiple_choice", "true_false", "calculation"]
QUIZ_MAX_PER_TYPE = 10
QUIZ_MAX_TOTAL = 20

# 论文
PAPER_MAX_BYTES = 20 * 1024 * 1024
PAPER_KEYWORDS_MIN = 3
PAPER_KEYWORDS_MAX = 5
PAPER_BUILTIN_PROFILE = SKILLS_DIR / "paper-format-corrector" / "assets" / "generic-cn-course-paper-v1.json"

SUPPORTED_DOC_EXTS = {".pdf", ".docx", ".pptx", ".md", ".txt", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
OUTLINE_DOC_EXTS = {".docx", ".md", ".pptx"}

SKILL_FILES = {
    "rag": SKILLS_DIR / "rag-evidence-reader" / "SKILL.md",
    "quiz": SKILLS_DIR / "grounded-quiz-generator" / "SKILL.md",
    "paper": SKILLS_DIR / "paper-format-corrector" / "SKILL.md",
    "mindmap": SKILLS_DIR / "mindmap-builder" / "SKILL.md",
    "notebook_draft": SKILLS_DIR / "notebook-draft" / "SKILL.md",
    "lecture_recap": SKILLS_DIR / "lecture-recap" / "SKILL.md",
    "teacher_examiner": SKILLS_DIR / "experienced-teacher-examiner" / "SKILL.md",
    "exam_style": SKILLS_DIR / "bupt-exam-style" / "SKILL.md",
}


def ensure_dirs() -> None:
    for d in (upload_dir(), export_dir(), tmp_dir()):
        d.mkdir(parents=True, exist_ok=True)


def _protect_api_key(plain: str) -> str:
    if not plain:
        return ""
    if os.name != "nt":
        raise RuntimeError("当前系统不支持 Windows DPAPI，无法安全保存 API Key。")
    try:
        import win32crypt
        blob = win32crypt.CryptProtectData(
            plain.encode("utf-8"), "知序 API Key", API_KEY_ENTROPY, None, None, 0
        )
        return base64.b64encode(blob).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"API Key 加密失败：{exc}") from exc


def _unprotect_api_key(encoded: str) -> str:
    if not encoded:
        return ""
    if os.name != "nt":
        return ""
    try:
        import win32crypt
        blob = base64.b64decode(encoded.encode("ascii"))
        _description, plain = win32crypt.CryptUnprotectData(blob, API_KEY_ENTROPY, None, None, 0)
        return plain.decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


def _write_config(data: dict) -> None:
    tmp = CONFIG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG_PATH)


def ensure_config_security() -> bool:
    """把旧版明文 api_key 迁移成 DPAPI 密文；失败时保留原文件不破坏配置。"""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        return False
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(raw, dict):
        return False
    plain = raw.get("api_key")
    encoded = raw.get("api_key_enc")
    if isinstance(encoded, str) and encoded:
        if "api_key" in raw:
            raw.pop("api_key", None)
            raw["api_key_enc"] = encoded
            try:
                _write_config(raw)
            except OSError:
                pass
        return True
    if not isinstance(plain, str) or not plain:
        return False
    try:
        protected = _protect_api_key(plain)
    except RuntimeError:
        return False
    next_data = {
        "base_url": raw.get("base_url", "") if isinstance(raw.get("base_url"), str) else "",
        "api_key_enc": protected,
        "model": raw.get("model", "") if isinstance(raw.get("model"), str) else "",
    }
    try:
        _write_config(next_data)
        return True
    except OSError:
        return False


def load_config() -> dict:
    ensure_dirs()
    result = {"base_url": "", "api_key": "", "model": ""}
    if not CONFIG_PATH.exists():
        return result
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if not isinstance(data, dict):
        return result
    for key in ("base_url", "model"):
        if isinstance(data.get(key), str):
            result[key] = data[key]
    if isinstance(data.get("api_key_enc"), str) and data["api_key_enc"]:
        result["api_key"] = _unprotect_api_key(data["api_key_enc"])
    elif isinstance(data.get("api_key"), str):
        # 兼容尚未迁移的旧配置；应用启动时会执行加密迁移
        result["api_key"] = data["api_key"]
    return result


def save_config(cfg: dict) -> None:
    ensure_dirs()
    current = load_config()
    next_data = {
        "base_url": current.get("base_url", ""),
        "api_key_enc": "",
        "model": current.get("model", ""),
    }
    for key in ("base_url", "api_key", "model"):
        if key in cfg and isinstance(cfg[key], str):
            if key == "api_key":
                if cfg[key].strip():
                    next_data["api_key_enc"] = _protect_api_key(cfg[key].strip())
            else:
                next_data[key] = cfg[key]
    if not next_data["api_key_enc"]:
        current_key = current.get("api_key", "")
        if current_key:
            next_data["api_key_enc"] = _protect_api_key(current_key)
        elif CONFIG_PATH.exists():
            try:
                old = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                if isinstance(old, dict) and isinstance(old.get("api_key_enc"), str):
                    next_data["api_key_enc"] = old["api_key_enc"]
            except (OSError, json.JSONDecodeError):
                pass
    _write_config(next_data)


def masked_key() -> str:
    key = load_config().get("api_key", "")
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return key[:3] + "****" + key[-4:]
