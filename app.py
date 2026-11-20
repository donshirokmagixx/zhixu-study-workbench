"""FastAPI 入口与全部 HTTP 路由（唯一注册处）。

只做：读请求 → 调模块 → 落库/转错误 → 返回 JSON。
解析、切块、检索、模型调用、DOCX/PDF 生成都在各自模块里。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import Body, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.datastructures import MutableHeaders

import auth
import config
import db
import llm
import mindmap
import notes
import officedoc
import paper
import quiz
import rag
import tree

ERROR_STATUS = {
    "invalid_request": 400,
    "zone_duplicate": 409,
    "zone_not_found": 404,
    "zone_not_empty": 409,
    "node_not_found": 404,
    "node_not_empty": 409,
    "document_unsupported": 415,
    "document_not_found": 404,
    "page_not_found": 404,
    "page_preview_unavailable": 503,
    "document_too_large": 413,
    "document_parse_failed": 422,
    "duplicate_document": 409,
    "conversation_not_found": 404,
    "note_not_found": 404,
    "model_not_configured": 400,
    "model_request_failed": 502,
    "model_output_invalid": 502,
    "insufficient_evidence": 422,
    "pdf_engine_unavailable": 500,
    "paper_version_mismatch": 409,
    "paper_check_required": 409,
    "export_not_ready": 409,
    "internal_error": 500,
}

GENERAL_KNOWLEDGE_NOTE = "本次未找到课件依据，以下为通用讲解。"
NO_CITE = "模型没有给出可核对的资料引用，本次回答不作为资料结论。"
QUIZ_SHORT = "当前知识区的资料不足以生成 6 道题。"
MINDMAP_SHORT = "当前资料不足以生成导图。"

_pdf_engine: dict = {"engine": "none", "message": ""}


def _pdf_engine_now() -> str:
    """只读取缓存。COM 自检只在 POST /api/settings/pdf-test 中执行，避免 GET 被 Word 自动化阻塞。"""
    return _pdf_engine["engine"]


def _url(path: str | None) -> str | None:
    if not path:
        return None
    return "/exports/" + Path(path).name


def _row(row) -> dict:
    return dict(row) if row is not None else {}


def _body(payload: dict | None) -> dict:
    return payload if isinstance(payload, dict) else {}


_FOLLOWUP_START = (
    "为什么", "怎么", "如何", "还有", "继续", "再", "然后", "所以",
    "那", "这", "它", "他", "她", "举例", "详细", "展开", "解释",
)
_FOLLOWUP_REFERENCE = re.compile(r"(它们|他们|她们|它|这个|那个|这些|那些|上述|前面|刚才|这里|其中|这种|上面|该)")


def _recent_chat_history(conversation_id: str) -> list[dict]:
    """读取同会话最近消息，仅供模型理解追问；事实依据仍来自本轮 evidence。"""
    rows = db.q(
        "SELECT role, content, status FROM messages"
        " WHERE conversation_id = ? AND role IN ('user', 'assistant')"
        " ORDER BY rowid DESC LIMIT ?",
        (conversation_id, int(config.CHAT_HISTORY_MAX_MESSAGES)),
    )
    history: list[dict] = []
    for row in reversed(rows):
        if row["role"] == "assistant" and row["status"] not in ("answered", "partial", "general_knowledge"):
            continue
        text = " ".join((row["content"] or "").split())
        if not text:
            continue
        if len(text) > config.CHAT_HISTORY_MAX_CHARS:
            text = text[:config.CHAT_HISTORY_MAX_CHARS].rstrip() + "…"
        history.append({"role": row["role"], "content": text})
    return history


def _history_user_content(question: str, history: list[dict]) -> str:
    if not history:
        return question
    lines = [
        "【同会话最近对话｜仅用于理解指代、条件和延续讲解；不是资料事实来源】",
    ]
    for message in history:
        label = "学生" if message["role"] == "user" else "助教"
        lines.append(f"{label}：{message['content']}")
    lines.extend(["【当前问题】", question])
    return "\n".join(lines)


def _needs_followup_context(question: str) -> bool:
    text = question.strip()
    if len(text) <= 12 or text.startswith(_FOLLOWUP_START):
        return True
    return len(text) <= 80 and bool(_FOLLOWUP_REFERENCE.search(text))


def _retrieval_query(question: str, history: list[dict]) -> str:
    """追问时补入上一轮学生问题作为检索主题，减少“它/这个”等指代丢失。"""
    if not _needs_followup_context(question):
        return question
    previous = ""
    for message in reversed(history):
        if message["role"] == "user":
            previous = message["content"].strip()
            break
    if not previous or previous == question.strip():
        return question
    previous = previous[:config.CHAT_HISTORY_QUERY_EXTRA_CHARS].strip()
    return f"{previous}\n{question}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    login = app.state.auth = auth.Auth()
    if login.configured:
        db.prepare_account_stores(login.primary, login.user_ids)
        for user_id in sorted(login.user_ids):
            with config.use_store(config.account_dir(user_id)):
                initialize_store()
    elif not login.required:
        initialize_store()
    if login.configured or not login.required:
        config.ensure_config_security()
    for key in config.SKILL_FILES:
        llm.load_skill(key)
    yield


def initialize_store() -> None:
    db.init_db()
    if not db.fts5_available():
        raise RuntimeError("SQLite FTS5 不可用：请使用带 FTS5 的 Python 3.11")
    rag.refresh_page_indexes()


app = FastAPI(title="知序", lifespan=lifespan)
app.state.auth = auth.Auth()

_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1", "testserver"}
_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
_PUBLIC_ROUTES = {
    ("GET", "/"), ("GET", "/api/auth/session"),
    ("POST", "/api/auth/login"), ("POST", "/api/auth/logout"),
}
_PRIMARY_ROUTES = {
    ("PUT", "/api/settings"), ("POST", "/api/settings/test"), ("POST", "/api/settings/pdf-test"),
}


def _host_name(value: str) -> str:
    host = (value or "").strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        return host.split("]", 1)[0].lstrip("[")
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _request_guard_error(host: str, origin: str | None, sec_fetch_site: str | None,
                         content_length: str | None) -> tuple[int, str, str] | None:
    if _host_name(host) not in _ALLOWED_HOSTS:
        return 403, "request_forbidden", "仅允许本机访问。"
    if (sec_fetch_site or "").lower() == "cross-site":
        return 403, "request_forbidden", "跨站请求被拒绝。"
    if origin:
        try:
            if urlparse(origin).netloc.lower() != host.lower():
                return 403, "request_forbidden", "请求来源与当前服务不一致。"
        except ValueError:
            return 403, "request_forbidden", "请求来源无效。"
    try:
        length = int(content_length or 0)
    except ValueError:
        length = 0
    if length > config.REQUEST_MAX_BYTES:
        return 413, "invalid_request", "请求体超过 25MB 上限。"
    return None


def _auth_error(exc: auth.AuthError) -> JSONResponse:
    return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)


class LocalRequestGuard:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        route = (request.method, request.url.path)

        async def send_response(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for key, value in _SECURITY_HEADERS.items():
                    headers.setdefault(key, value)
                headers["Cache-Control"] = "no-store"
            await send(message)

        blocked = _request_guard_error(
            request.headers.get("host", ""),
            request.headers.get("origin"),
            request.headers.get("sec-fetch-site"),
            request.headers.get("content-length"),
        )
        if blocked is not None:
            status, code, message = blocked
            await _auth_error(auth.AuthError(status, code, message))(scope, receive, send_response)
            return

        login = request.app.state.auth
        user = None
        try:
            if route == ("GET", "/api/auth/session"):
                user = await login.user_for(request.cookies.get(auth.COOKIE))
            elif login.required and route not in _PUBLIC_ROUTES:
                if not login.configured:
                    raise auth.not_configured()
                user = await login.user_for(request.cookies.get(auth.COOKIE))
                if user is None:
                    raise auth.AuthError(401, "auth_required", "请先登录知序。")
                if route in _PRIMARY_ROUTES and not user.is_primary:
                    raise auth.AuthError(403, "primary_required", "此设置由主账号管理。")
        except auth.AuthError as exc:
            await _auth_error(exc)(scope, receive, send_response)
            return
        request.state.auth_user = user
        root = config.account_dir(user.id) if user is not None else None
        # Keep the account bound until the ASGI response stream and file transfer finish.
        with config.use_store(root):
            await self.app(scope, receive, send_response)


app.add_middleware(LocalRequestGuard)


@app.exception_handler(llm.AppError)
async def _app_error(_: Request, exc: llm.AppError):
    return JSONResponse(
        {"error": {"code": exc.code, "message": exc.message}},
        status_code=ERROR_STATUS.get(exc.code, 500),
    )


@app.exception_handler(Exception)
async def _unhandled(_: Request, exc: Exception):
    return JSONResponse(
        {"error": {"code": "internal_error", "message": f"内部错误：{exc.__class__.__name__}"}},
        status_code=500,
    )


# ---------- 1-5 健康与设置 ----------

@app.get("/api/auth/session")
async def auth_session(request: Request):
    return request.app.state.auth.status(request.state.auth_user)


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    login = request.app.state.auth
    if not login.configured:
        return _auth_error(auth.not_configured())
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        return _auth_error(auth.AuthError(400, "invalid_request", "登录请求须使用 JSON 格式。"))
    try:
        data = _body(await request.json())
    except (ValueError, UnicodeError):
        data = {}
    email, password = data.get("email"), data.get("password")
    if (not isinstance(email, str) or not 0 < len(email.strip()) <= 254
            or not isinstance(password, str) or not 0 < len(password) <= 1024):
        return _auth_error(auth.AuthError(400, "invalid_request", "请输入有效的邮箱和密码。"))
    try:
        token, user = await login.sign_in(email.strip(), password, request.cookies.get(auth.COOKIE))
    except auth.AuthError as exc:
        return _auth_error(exc)
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_SECONDS, httponly=True,
                        samesite="strict", secure=request.url.scheme == "https")
    return login.status(user)


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    login = request.app.state.auth
    login.sign_out(request.cookies.get(auth.COOKIE))
    response.delete_cookie(auth.COOKIE, httponly=True, samesite="strict", secure=request.url.scheme == "https")
    return login.status()


@app.get("/api/health")
def health():
    try:
        llm.get_model_config()
        configured = True
    except llm.AppError:
        configured = False
    return {
        "ok": True,
        "model_configured": configured,
        "pdf_engine": _pdf_engine_now(),
        "fts5": True,
    }


@app.get("/api/settings")
def get_settings():
    cfg = config.load_config()
    usage = [_row(r) for r in db.q("SELECT * FROM usage_logs ORDER BY created_at DESC LIMIT 20")]
    return {
        "base_url": cfg["base_url"],
        "model": cfg["model"],
        "api_key_masked": config.masked_key(),
        "pdf_engine": _pdf_engine_now(),
        "usage": usage,
    }


@app.put("/api/settings")
def put_settings(payload: dict | None = Body(default=None)):
    data = _body(payload)
    base_url = data.get("base_url")
    model = data.get("model")
    api_key = data.get("api_key")
    if not isinstance(base_url, str) or not isinstance(model, str):
        raise llm.AppError("invalid_request", "base_url 与 model 必须是字符串。")
    cfg = {"base_url": base_url.strip(), "model": model.strip()}
    if isinstance(api_key, str) and api_key.strip():
        cfg["api_key"] = api_key.strip()
    try:
        config.save_config(cfg)
    except RuntimeError as exc:
        raise llm.AppError("internal_error", str(exc)) from exc
    return {"ok": True}


@app.post("/api/settings/test")
def settings_test():
    ok, message = llm.test_connection()
    return {"ok": ok, "message": message}


@app.post("/api/settings/pdf-test")
def settings_pdf_test():
    try:
        result = officedoc.selftest()
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "engine": "none", "message": f"{exc.__class__.__name__}: {exc}"}
    _pdf_engine["engine"] = result.get("engine") or "none"
    _pdf_engine["message"] = result.get("message") or ""
    return result


# ---------- 6-9 知识区 ----------

def _zone_name(raw) -> str:
    if not isinstance(raw, str):
        raise llm.AppError("invalid_request", "知识区名称必须是字符串。")
    name = raw.strip()
    if not (1 <= len(name) <= 20):
        raise llm.AppError("invalid_request", "知识区名称需为 1–20 个字符。")
    return name


@app.get("/api/zones")
def list_zones():
    rows = db.q(
        "SELECT z.id, z.name, z.created_at, COUNT(d.id) AS doc_count "
        "FROM zones z LEFT JOIN documents d ON d.zone_id = z.id GROUP BY z.id ORDER BY z.created_at ASC"
    )
    return [dict(r) for r in rows]


@app.post("/api/zones")
def create_zone(payload: dict | None = Body(default=None)):
    name = _zone_name(_body(payload).get("name"))
    if db.q1("SELECT id FROM zones WHERE name = ?", (name,)):
        raise llm.AppError("zone_duplicate", "知识区名称已存在。")
    zone_id = db.new_id()
    now = db.now()
    db.x("INSERT INTO zones (id, name, created_at) VALUES (?,?,?)", (zone_id, name, now))
    return {"id": zone_id, "name": name, "created_at": now}


@app.patch("/api/zones/{zone_id}")
def rename_zone(zone_id: str, payload: dict | None = Body(default=None)):
    if not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    name = _zone_name(_body(payload).get("name"))
    other = db.q1("SELECT id FROM zones WHERE name = ? AND id <> ?", (name, zone_id))
    if other:
        raise llm.AppError("zone_duplicate", "知识区名称已存在。")
    db.x("UPDATE zones SET name = ? WHERE id = ?", (name, zone_id))
    return {"id": zone_id, "name": name}


@app.delete("/api/zones/{zone_id}")
def delete_zone(zone_id: str):
    if not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    count = db.q1("SELECT COUNT(*) AS c FROM documents WHERE zone_id = ?", (zone_id,))["c"]
    if count:
        raise llm.AppError("zone_not_empty", "知识区下还有资料，请先删除资料。")
    node_count = db.q1("SELECT COUNT(*) AS c FROM knowledge_nodes WHERE zone_id = ?", (zone_id,))["c"]
    conv_count = db.q1("SELECT COUNT(*) AS c FROM conversations WHERE zone_id = ?", (zone_id,))["c"]
    if node_count or conv_count:
        raise llm.AppError("zone_not_empty", "知识区下还有知识节点或会话，请先清理。")
    db.x("DELETE FROM zones WHERE id = ?", (zone_id,))
    return Response(status_code=204)


# ---------- 9.1 知识树 ----------

@app.get("/api/nodes")
def list_nodes(zone_id: str = ""):
    if zone_id and not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    return tree.list_nodes(zone_id or None)


@app.post("/api/nodes")
def create_node(payload: dict | None = Body(default=None)):
    data = _body(payload)
    zone_id = data.get("zone_id")
    if not isinstance(zone_id, str):
        raise llm.AppError("invalid_request", "zone_id 必须是字符串。")
    parent_id = data.get("parent_id")
    if parent_id is not None and not isinstance(parent_id, str):
        raise llm.AppError("invalid_request", "parent_id 必须是字符串或空值。")
    return tree.create_node(zone_id, parent_id, data.get("name"))


@app.patch("/api/nodes/{node_id}")
def rename_node(node_id: str, payload: dict | None = Body(default=None)):
    return tree.rename_node(node_id, _body(payload).get("name"))


@app.delete("/api/nodes/{node_id}")
def delete_node(node_id: str):
    tree.delete_node(node_id)
    return Response(status_code=204)


# ---------- 10-13 资料 ----------

def _ext(filename: str) -> str:
    return Path(filename).suffix.lower()


@app.post("/api/documents")
async def upload_document(
    file: UploadFile = File(...),
    zone_id: str = Form(...),
    node_id: str = Form(""),
):
    if not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    node_id = node_id.strip() or None
    tree.ensure_zone_node(zone_id, node_id)
    ext = _ext(file.filename or "")
    if ext not in config.SUPPORTED_DOC_EXTS:
        raise llm.AppError("document_unsupported", f"不支持的资料格式：{ext or '未知'}。")
    content = await file.read()
    if len(content) > config.UPLOAD_MAX_BYTES:
        raise llm.AppError("document_too_large", "文件超过 20MB 上限。")
    sha = hashlib.sha256(content).hexdigest()
    dup = db.q1("SELECT id FROM documents WHERE zone_id = ? AND sha256 = ?", (zone_id, sha))
    if dup:
        raise llm.AppError("duplicate_document", "该知识区已存在相同内容的资料。")
    doc_id = db.new_id()
    saved = config.upload_dir() / f"{doc_id}{ext}"
    saved.write_bytes(content)
    db.x(
        "INSERT INTO documents (id, zone_id, node_id, filename, ext, size, sha256, status, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (doc_id, zone_id, node_id, file.filename or f"未命名{ext}", ext, len(content), sha, "parsing", db.now()),
    )
    try:
        result = rag.parse_and_index(doc_id)
        return {
            "document_id": doc_id,
            "status": result["status"],
            "warning": result.get("warning"),
            "quality": result.get("quality", 1.0),
            "quality_note": result.get("quality_note") or "",
        }
    except Exception as exc:  # noqa: BLE001 - 解析失败必须落库可见
        error_text = f"{exc.__class__.__name__}: {exc}"[:500]
        quality_note = ("解析失败：" + error_text)[:500]
        db.x(
            "UPDATE documents SET status='failed', error=?, quality=0.0, quality_note=? WHERE id = ?",
            (error_text, quality_note, doc_id),
        )
        return {
            "document_id": doc_id,
            "status": "failed",
            "warning": None,
            "quality": 0.0,
            "quality_note": quality_note,
        }


@app.get("/api/documents")
def list_documents(zone_id: str = "", node_id: str = ""):
    if not zone_id:
        if node_id:
            raise llm.AppError("invalid_request", "按节点筛选资料时必须同时提供知识区。")
        return [dict(r) for r in db.q("SELECT * FROM documents ORDER BY created_at DESC")]
    if not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    node_id = node_id.strip() or None
    if not node_id:
        rows = db.q("SELECT * FROM documents WHERE zone_id = ? ORDER BY created_at DESC", (zone_id,))
        return [dict(r) for r in rows]
    document_ids = tree.scope_document_ids(zone_id, node_id) or []
    if not document_ids:
        return []
    placeholders = ",".join("?" for _ in document_ids)
    rows = db.q(
        f"SELECT * FROM documents WHERE zone_id = ? AND id IN ({placeholders}) ORDER BY created_at DESC",
        (zone_id, *document_ids),
    )
    return [dict(r) for r in rows]


@app.patch("/api/documents/{document_id}/node")
def move_document_node(document_id: str, payload: dict | None = Body(default=None)):
    node_id = _body(payload).get("node_id")
    if node_id is not None and not isinstance(node_id, str):
        raise llm.AppError("invalid_request", "node_id 必须是字符串或空值。")
    node_id = node_id.strip() if isinstance(node_id, str) else None
    return tree.move_document(document_id, node_id or None)


@app.post("/api/documents/{document_id}/reindex")
def reindex_document(document_id: str):
    if not db.q1("SELECT id FROM documents WHERE id = ?", (document_id,)):
        raise llm.AppError("invalid_request", "资料不存在。")
    result = rag.reindex(document_id)
    return {"status": result["status"], "chunk_count": result.get("chunk_count") or 0}


@app.delete("/api/documents/{document_id}")
def delete_document(document_id: str):
    row = db.q1("SELECT * FROM documents WHERE id = ?", (document_id,))
    if not row:
        raise llm.AppError("invalid_request", "资料不存在。")
    rag.delete_document_data(document_id)
    saved = config.upload_dir() / f"{document_id}{row['ext']}"
    if saved.exists():
        saved.unlink()
    db.x("DELETE FROM documents WHERE id = ?", (document_id,))
    return Response(status_code=204)


# ---------- 14-18 会话与聊天 ----------

def _conversation(conversation_id: str):
    row = db.q1("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
    if not row:
        raise llm.AppError("conversation_not_found", "会话不存在。")
    return row


def _scoped_preview_document(conversation_id: str, document_id: str) -> tuple[dict, Path]:
    conversation = _conversation(conversation_id)
    zone_id = conversation["zone_id"]
    allowed_ids = tree.scope_document_ids(zone_id, conversation["node_id"])
    row = db.q1("SELECT * FROM documents WHERE id = ? AND zone_id = ?", (document_id, zone_id))
    if row and row["ext"] in rag.PAGE_PREVIEW_EXTS and (allowed_ids is None or document_id in allowed_ids):
        path = config.upload_dir() / f"{row['id']}{row['ext']}"
        if path.is_file():
            return dict(row), path
    raise llm.AppError("document_not_found", "当前会话范围内找不到此资料。")


@app.get("/api/conversations/{conversation_id}/documents/{document_id}/original")
def download_source_document(conversation_id: str, document_id: str):
    document, path = _scoped_preview_document(conversation_id, document_id)
    return FileResponse(path, filename=document["filename"])


@app.get("/api/conversations/{conversation_id}/documents/{document_id}/pages/{page_number}/preview")
def preview_source_page(conversation_id: str, document_id: str, page_number: int):
    document, path = _scoped_preview_document(conversation_id, document_id)
    if not 1 <= page_number <= (document["page_count"] or 0):
        raise llm.AppError("page_not_found", "原文件中没有这页。")
    if document["ext"] == ".pptx":
        image = officedoc.pptx_slide_preview(path, page_number)
    else:
        try:
            image = rag.pdf_page_preview(path, page_number)
        except (OSError, RuntimeError) as exc:
            raise llm.AppError("page_preview_unavailable", "无法预览 PDF。请打开原文件查看。") from exc
    return Response(image, media_type="image/png")


@app.get("/api/conversations")
def list_conversations(zone_id: str = ""):
    sql = (
        "SELECT c.id, c.zone_id, c.node_id, c.title, c.created_at, COUNT(m.id) AS message_count "
        "FROM conversations c LEFT JOIN messages m ON m.conversation_id = c.id "
    )
    params: tuple = ()
    if zone_id:
        sql += "WHERE c.zone_id = ? "
        params = (zone_id,)
    sql += "GROUP BY c.id ORDER BY c.created_at DESC"
    return [dict(r) for r in db.q(sql, params)]


@app.post("/api/conversations")
def create_conversation(payload: dict | None = Body(default=None)):
    data = _body(payload)
    zone_id = data.get("zone_id")
    if not isinstance(zone_id, str) or not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    node_id = data.get("node_id")
    if node_id is not None and not isinstance(node_id, str):
        raise llm.AppError("invalid_request", "node_id 必须是字符串或空值。")
    node_id = node_id.strip() if isinstance(node_id, str) else None
    tree.ensure_zone_node(zone_id, node_id or None)
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        title = "新会话"
    title = title.strip()[:60]
    conv_id = db.new_id()
    now = db.now()
    db.x(
        "INSERT INTO conversations (id, zone_id, node_id, title, created_at) VALUES (?,?,?,?,?)",
        (conv_id, zone_id, node_id or None, title, now),
    )
    return {"id": conv_id, "zone_id": zone_id, "node_id": node_id or None, "title": title, "created_at": now}


@app.delete("/api/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    _conversation(conversation_id)
    msg_ids = [r["id"] for r in db.q("SELECT id FROM messages WHERE conversation_id = ?", (conversation_id,))]
    for message_id in msg_ids:
        db.x("DELETE FROM citations WHERE message_id = ?", (message_id,))
    db.x("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
    db.x("DELETE FROM conversations WHERE id = ?", (conversation_id,))
    return Response(status_code=204)


def _citation_payload(message_id: str) -> list[dict]:
    rows = db.q(
        "SELECT c.id AS citation_id, c.chunk_id, c.document_id, c.page, c.heading_path,"
        " c.snippet, c.text_hash, d.filename"
        " FROM citations c LEFT JOIN documents d ON d.id = c.document_id"
        " WHERE c.message_id = ? ORDER BY c.rowid ASC",
        (message_id,),
    )
    payload = []
    for r in rows:
        text = None
        source_state = "snapshot"
        page = r["page"]
        heading = r["heading_path"]
        if r["text_hash"]:
            exact = db.q1(
                "SELECT text, page, heading_path, archived FROM chunk_sources"
                " WHERE document_id = ? AND text_sha256 = ? ORDER BY archived ASC, page ASC LIMIT 1",
                (r["document_id"], r["text_hash"]),
            )
            if exact is not None:
                text = exact["text"]
                page = exact["page"] if exact["page"] is not None else page
                heading = exact["heading_path"] or heading
                source_state = "snapshot" if exact["archived"] else "current"
            elif r["filename"]:
                nearby = db.q1(
                    "SELECT text, page, heading_path FROM chunks"
                    " WHERE document_id = ? AND heading_path = ? ORDER BY ordinal ASC LIMIT 1",
                    (r["document_id"], heading or ""),
                )
                if nearby is None:
                    nearby = db.q1(
                        "SELECT text, page, heading_path FROM chunks"
                        " WHERE document_id = ? ORDER BY ordinal ASC LIMIT 1",
                        (r["document_id"],),
                    )
                if nearby is not None:
                    text = nearby["text"]
                    page = nearby["page"] if nearby["page"] is not None else page
                    heading = nearby["heading_path"] or heading
                    source_state = "drifted"
                else:
                    source_state = "missing"
            else:
                source_state = "missing"
        elif r["chunk_id"]:
            old = db.q1("SELECT text, page, heading_path, archived FROM chunk_sources WHERE id = ?", (r["chunk_id"],))
            if old is not None:
                text = old["text"]
                page = old["page"] if old["page"] is not None else page
                heading = old["heading_path"] or heading
                source_state = "snapshot" if old["archived"] else "current"
            else:
                source_state = "missing" if not r["filename"] else "snapshot"
        elif not r["filename"]:
            source_state = "missing"

        payload.append({
            "document_id": r["document_id"],
            "filename": r["filename"] or "",
            "page": page,
            "heading_path": heading,
            "snippet": r["snippet"] or "",
            "text": text,
            "source_state": source_state,
            "drifted": source_state == "drifted",
            "citation_id": r["citation_id"],
            "chunk_id": r["chunk_id"],
        })
    return payload


@app.get("/api/conversations/{conversation_id}/messages")
def list_messages(conversation_id: str):
    _conversation(conversation_id)
    rows = db.q("SELECT * FROM messages WHERE conversation_id = ? ORDER BY created_at ASC, rowid ASC",
                (conversation_id,))
    return [
        {
            "id": r["id"],
            "role": r["role"],
            "content": r["content"],
            "status": r["status"],
            "created_at": r["created_at"],
            "retrieved_pages": json.loads(r["retrieved_pages"]),
            "citations": _citation_payload(r["id"]),
        }
        for r in rows
    ]


def _insert_message(conversation_id: str, role: str, content: str, status: str, *, retrieved_pages=()) -> str:
    message_id = db.new_id()
    db.x(
        "INSERT INTO messages (id, conversation_id, role, content, status, retrieved_pages, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (message_id, conversation_id, role, content, status, json.dumps(retrieved_pages, ensure_ascii=False), db.now()),
    )
    return message_id


def _retrieved_pages(items: list[dict]) -> list[dict]:
    pages = {}
    for item in items:
        if (item["ext"] not in rag.PAGE_PREVIEW_EXTS or item["page_index_version"] != rag.PAGE_INDEX_VERSION
                or not item["page"]):
            continue
        key = (item["document_id"], item["page"])
        if key not in pages:
            pages[key] = {field: item[field] for field in ("document_id", "filename", "ext", "page")}
    return list(pages.values())


@app.post("/api/chat")
def chat(payload: dict | None = Body(default=None), stream: bool = False):
    data = _body(payload)
    conversation_id = data.get("conversation_id")
    question = data.get("question")
    if not isinstance(question, str) or not question.strip():
        raise llm.AppError("invalid_request", "问题不能为空。")
    question = question.strip()
    if len(question) > 4000:
        raise llm.AppError("invalid_request", "问题长度不能超过 4000 字。")
    conv = _conversation(conversation_id)
    zone_id = conv["zone_id"]
    document_ids = tree.scope_document_ids(zone_id, conv["node_id"])
    history = _recent_chat_history(conversation_id)
    retrieval_query = _retrieval_query(question, history)

    user_count = db.q1(
        "SELECT COUNT(*) AS c FROM messages WHERE conversation_id = ? AND role = 'user'",
        (conversation_id,),
    )["c"]
    if user_count == 0:
        db.x("UPDATE conversations SET title = ? WHERE id = ?", (question[:20], conversation_id))
    items = rag.retrieve(zone_id, retrieval_query, document_ids=document_ids)
    pages = _retrieved_pages(items)
    user_id = _insert_message(conversation_id, "user", question, "answered", retrieved_pages=pages)
    if stream:
        message = {
            "id": user_id, "role": "user", "content": question, "status": "answered",
            "created_at": db.now(), "retrieved_pages": pages,
        }
        return StreamingResponse(
            _chat_events(message, conversation_id, zone_id, history, items),
            media_type="application/x-ndjson",
        )
    answer = _answer_chat(conversation_id, zone_id, question, history, items)
    return {**answer, "retrieved_pages": pages}


def _chat_events(message: dict, conversation_id: str, zone_id: str, history: list[dict], items: list[dict]):
    yield json.dumps({"type": "retrieved", "message": message}, ensure_ascii=False) + "\n"
    try:
        result = _answer_chat(conversation_id, zone_id, message["content"], history, items)
        event = {"type": "answer", "result": result}
    except llm.AppError as exc:
        event = {"type": "error", "error": {"code": exc.code, "message": exc.message}}
    except Exception:
        logging.getLogger(__name__).exception("Chat generation failed")
        event = {"type": "error", "error": {"code": "internal_error", "message": "生成回答时发生内部错误。"}}
    yield json.dumps(event, ensure_ascii=False) + "\n"


def _answer_chat(conversation_id: str, zone_id: str, question: str, history: list[dict], items: list[dict]) -> dict:
    evidence_pack, mapping = rag.build_evidence(zone_id, conversation_id, items)
    if mapping:
        instructions = (
            "以下是宿主提供的课程证据包。请把其中的 evidence 作为本课程的首要依据，"
            "可以综合多个片段、用自己的话讲解、举例和补充教学说明。"
            "课件未覆盖的基础背景如需补充，必须标注“课件外补充：”，不得为补充内容编造引用。"
            "引用课件事实时使用 [e1] 形式，citations 只能填写实际使用的有效 evidence_id："
        )
    else:
        instructions = (
            "本轮没有检索到课件证据。请直接用通用知识教学生，按问题需要解释原理、给出步骤或举例。"
            "status 填 general_knowledge，citations 填空数组。正文直接开始讲解，来源提示由网站添加。"
            "不要把对话历史当成课件依据，也不要添加课件引用。以下是空证据包："
        )
    obj, usage = llm.chat_json(
        "rag",
        _history_user_content(question, history),
        kind="chat",
        system_extra=instructions + "\n" + json.dumps(evidence_pack, ensure_ascii=False),
        max_tokens=config.CHAT_MAX_TOKENS,
        temperature=config.CHAT_TEMPERATURE,
    )
    status = obj.get("status")
    allowed_statuses = ("answered", "partial", "insufficient_evidence", "needs_parse", "blocked_scope")
    if not mapping:
        allowed_statuses += ("general_knowledge",)
    if status not in allowed_statuses:
        raise llm.AppError("model_output_invalid", "模型返回了未定义的 status。")
    answer = obj.get("answer")
    if not isinstance(answer, str):
        raise llm.AppError("model_output_invalid", "模型返回的 answer 无效。")
    if not mapping:
        answer = re.sub(r"\[e[0-9]+\]", "", answer, flags=re.IGNORECASE)
    answer = answer.strip()
    if not answer:
        raise llm.AppError("model_output_invalid", "模型返回的 answer 为空。")
    raw_citations = obj.get("citations")
    valid_ids = [cid for cid in (raw_citations if isinstance(raw_citations, list) else [])
                 if isinstance(cid, str) and cid in mapping]
    if not mapping:
        status = "general_knowledge"
        answer = f"{GENERAL_KNOWLEDGE_NOTE}\n\n{answer}"
    elif status == "answered" and not valid_ids:
        status = "insufficient_evidence"
        answer = NO_CITE
    message_id = _insert_message(conversation_id, "assistant", answer, status)
    for cid in valid_ids:
        item = mapping[cid]
        text_hash = item.get("text_sha256") or hashlib.sha256(
            (item.get("text") or "").encode("utf-8")
        ).hexdigest()
        db.x(
            "INSERT INTO citations (id, message_id, chunk_id, document_id, page, heading_path, snippet, text_hash)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (db.new_id(), message_id, item["chunk_id"], item["document_id"],
             item.get("page"), item.get("heading_path"), rag.snippet(item["text"]), text_hash),
        )
    return {
        "message_id": message_id,
        "status": status,
        "answer": answer,
        "citations": _citation_payload(message_id),
        "usage": {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        },
        "retrieved": len(items),
    }


# ---------- 19-23 导图 ----------

def _enrich_mindmap_sources(payload: dict) -> dict:
    """给导图节点补只读溯源字段：第一个来源 chunk 的原文片段/页码/章节（不修改库）。"""
    nodes = payload.get("nodes") if isinstance(payload, dict) else None
    if not isinstance(nodes, list):
        return payload
    for node in nodes:
        if not isinstance(node, dict):
            continue
        chunk_ids = node.get("source_chunk_ids") or []
        if not isinstance(chunk_ids, list) or not chunk_ids:
            continue
        row = db.q1(
            "SELECT c.text, c.page, c.heading_path, d.filename FROM chunk_sources c "
            "LEFT JOIN documents d ON d.id = c.document_id WHERE c.id = ?",
            (chunk_ids[0],),
        )
        if row:
            node["source_snippet"] = rag.snippet(row["text"], 120)
            node["source_filename"] = row["filename"] or ""
            node["source_page"] = row["page"]
            node["source_heading_path"] = row["heading_path"]
    return payload


@app.post("/api/mindmaps")
def create_mindmap(payload: dict | None = Body(default=None)):
    data = _body(payload)
    zone_id = data.get("zone_id")
    document_ids = data.get("document_ids")
    title = data.get("title")
    mode = data.get("mode")
    if not isinstance(zone_id, str) or not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    if not isinstance(document_ids, list) or not document_ids:
        raise llm.AppError("invalid_request", "请选择至少一份资料。")
    if not isinstance(title, str) or not title.strip():
        raise llm.AppError("invalid_request", "导图标题不能为空。")
    if mode not in {"structure", "concept"}:
        raise llm.AppError("invalid_request", "导图模式只能是 structure 或 concept。")
    result = mindmap.generate(zone_id, [str(x) for x in document_ids], title.strip()[:80], mode)
    return _enrich_mindmap_sources(result)


@app.get("/api/mindmaps")
def list_mindmaps(zone_id: str = ""):
    return mindmap.list_maps(zone_id)


@app.get("/api/mindmaps/{mindmap_id}")
def get_mindmap(mindmap_id: str):
    return _enrich_mindmap_sources(mindmap.get_map(mindmap_id))


@app.put("/api/mindmaps/{mindmap_id}")
def save_mindmap(mindmap_id: str, payload: dict | None = Body(default=None)):
    data = _body(payload)
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        raise llm.AppError("invalid_request", "nodes 必须是数组。")
    theme = data.get("theme")
    if theme is not None and not isinstance(theme, str):
        raise llm.AppError("invalid_request", "theme 必须是字符串。")
    layout = data.get("layout")
    if layout is not None and not isinstance(layout, dict):
        raise llm.AppError("invalid_request", "layout 必须是对象。")
    return mindmap.save_nodes(mindmap_id, nodes, theme=theme, layout=layout)


@app.post("/api/mindmaps/{mindmap_id}/regenerate")
def regenerate_mindmap(mindmap_id: str):
    return _enrich_mindmap_sources(mindmap.regenerate(mindmap_id))


@app.post("/api/mindmaps/export/pdf")
def export_mindmap_pdf(file: UploadFile = File(...)):
    """把前端渲染的导图 PNG（2x）包装成单页 PDF；纯本地转换，不调用模型。"""
    content = file.file.read()
    if not content:
        raise llm.AppError("invalid_request", "缺少导图图片数据。")
    if len(content) > 12 * 1024 * 1024:
        raise llm.AppError("invalid_request", "导图图片超过 12MB，无法导出 PDF。")
    if content[:8] != b"\x89PNG\r\n\x1a\n":
        raise llm.AppError("invalid_request", "仅支持 PNG 图片导出 PDF。")
    try:
        import fitz
        with rag.PDF_LOCK, fitz.open(stream=content, filetype="png") as src, fitz.open() as doc:
            pix = src[0].get_pixmap()
            page = doc.new_page(width=pix.width * 0.5, height=pix.height * 0.5)
            page.insert_image(page.rect, stream=content)
            pdf = doc.tobytes()
    except Exception as exc:  # noqa: BLE001
        raise llm.AppError("internal_error", f"PDF 导出失败：{exc.__class__.__name__}")
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="mindmap.pdf"'},
    )


# ---------- 学术笔记 ----------

@app.get("/api/notes")
def list_notes():
    return notes.list_notes()


@app.post("/api/notes")
def create_note(payload: dict | None = Body(default=None)):
    return notes.save_note(_body(payload))


@app.post("/api/notes/draft")
def draft_note(payload: dict | None = Body(default=None)):
    return notes.draft_note(_body(payload))


@app.post("/api/notes/summarize")
def summarize_note(payload: dict | None = Body(default=None)):
    """课堂录音转写 → 回忆摘要；不写入知识库，也不自动保存笔记。"""
    return notes.summarize_transcript(_body(payload))


@app.get("/api/notes/{note_id}")
def get_note(note_id: str):
    return notes.get_note(note_id)


@app.put("/api/notes/{note_id}")
def update_note(note_id: str, payload: dict | None = Body(default=None)):
    return notes.save_note(_body(payload), note_id)


@app.delete("/api/notes/{note_id}")
def delete_note(note_id: str):
    notes.delete_note(note_id)
    return Response(status_code=204)


# ---------- 24-26 出题 ----------

@app.post("/api/quizzes")
def create_quiz(payload: dict | None = Body(default=None)):
    data = _body(payload)
    zone_id = data.get("zone_id")
    mindmap_id = data.get("mindmap_id")
    if not isinstance(zone_id, str) or not db.q1("SELECT id FROM zones WHERE id = ?", (zone_id,)):
        raise llm.AppError("zone_not_found", "知识区不存在。")
    return quiz.generate(zone_id, str(mindmap_id) if mindmap_id else None, data.get("question_counts"))


@app.get("/api/quizzes/{quiz_id}")
def get_quiz(quiz_id: str):
    return quiz.get_quiz(quiz_id)


@app.post("/api/quizzes/{quiz_id}/export")
def export_quiz(quiz_id: str):
    result = quiz.export_student(quiz_id)
    return {"docx": _url(result.get("docx")), "pdf": _url(result.get("pdf"))}


# ---------- 27-29 论文 ----------

@app.post("/api/papers")
async def upload_paper(file: UploadFile = File(...)):
    ext = _ext(file.filename or "")
    if ext != ".docx":
        raise llm.AppError("document_unsupported", "论文只支持 .docx 文件。")
    content = await file.read()
    if len(content) > config.PAPER_MAX_BYTES:
        raise llm.AppError("document_too_large", "论文文件超过 20MB 上限。")
    sha = hashlib.sha256(content).hexdigest()
    saved = config.upload_dir() / f"paper-{db.new_id()}.docx"
    saved.write_bytes(content)
    job = paper.create_job(file.filename or "论文.docx", str(saved), sha)
    return {"paper_id": job["paper_id"], "filename": job["filename"], "sha256": job["sha256"]}


@app.post("/api/papers/{paper_id}/check")
def check_paper(paper_id: str, payload: dict | None = Body(default=None)):
    data = _body(payload)
    profile_mode = data.get("profile_mode") or "builtin"
    profile_json = data.get("profile_json")
    if profile_mode not in {"builtin", "upload"}:
        raise llm.AppError("invalid_request", "profile_mode 只能是 builtin 或 upload。")
    if profile_mode == "upload" and not isinstance(profile_json, dict):
        raise llm.AppError("invalid_request", "上传模板时 profile_json 必须是对象。")
    return paper.check(paper_id, profile_mode, profile_json if isinstance(profile_json, dict) else None)


@app.post("/api/papers/{paper_id}/execute")
def execute_paper(paper_id: str):
    result = paper.execute(paper_id)
    return {
        "status": result["status"],
        "corrected_docx": _url(result.get("corrected_docx")),
        "corrected_pdf": _url(result.get("corrected_pdf")),
        "report_pdf": _url(result.get("report_pdf")),
        "checks": result.get("checks") or [],
    }


# ---------- 30-31 用量与清空 ----------

@app.get("/api/usage")
def usage(limit: int = 50):
    limit = max(1, min(int(limit or 50), 100))
    return [dict(r) for r in db.q("SELECT * FROM usage_logs ORDER BY created_at DESC LIMIT ?", (limit,))]


@app.post("/api/data/clear")
def clear_data(payload: dict | None = Body(default=None)):
    if _body(payload).get("confirm") != "CLEAR":
        raise llm.AppError("invalid_request", "请输入 CLEAR 确认清空。")
    for table in ("citations", "messages", "conversations", "mindmap_nodes", "mindmaps",
                  "quiz_questions", "quizzes", "scholar_notes", "chunks", "chunk_source_snapshots",
                  "documents", "knowledge_nodes", "zones", "paper_jobs", "usage_logs", "settings"):
        db.x(f"DELETE FROM {table}")
    db.x("DELETE FROM chunks_fts")
    for directory in (config.upload_dir(), config.export_dir(), config.tmp_dir()):
        if directory.exists():
            for item in directory.iterdir():
                if item.is_dir():
                    shutil.rmtree(item, ignore_errors=True)
                else:
                    try:
                        item.unlink()
                    except OSError:
                        pass
    return {"ok": True}


# ---------- 32 静态页面与导出文件 ----------

@app.get("/")
def index():
    page = config.WEB_DIR / "index.html"
    if not page.exists():
        return JSONResponse(
            {"error": {"code": "internal_error", "message": "前端页面尚未生成（web/index.html）。"}},
            status_code=500,
        )
    return FileResponse(page, media_type="text/html; charset=utf-8")


@app.get("/exports/{name}")
def export_file(name: str):
    safe = Path(name).name
    if safe != name or safe in ("", ".", "..") or ":" in safe:
        raise llm.AppError("invalid_request", "非法文件名。")
    target = config.export_dir() / safe
    if not target.is_file() or target.is_symlink():
        raise llm.AppError("export_not_ready", "导出文件不存在或尚未生成。")
    return FileResponse(target)
