"""OpenAI 兼容调用、skill 加载与哈希、JSON 解析与校验、用量记录。

对外接口见 docs/模块接口约定.md。所有业务模块通过 AppError 抛错，由 app.py 统一映射为
{"error":{"code","message"}} 响应。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Tuple

import httpx

import config
import db


class AppError(Exception):
    code = "internal_error"

    def __init__(self, code: str | None = None, message: str = "") -> None:
        self.code = code or self.__class__.code
        self.message = message or code or self.__class__.code
        super().__init__(self.message)


class ModelNotConfigured(AppError):
    code = "model_not_configured"


class ModelRequestFailed(AppError):
    code = "model_request_failed"


class ModelOutputInvalid(AppError):
    code = "model_output_invalid"


_skill_cache: dict[str, Tuple[str, str]] = {}
MAX_TOKENS_CAP = 8000  # 截断自动加倍的上限


def _finish_reason(data: dict) -> str | None:
    try:
        return data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError):
        return None


def _strip_yaml_front_matter(raw: str) -> str:
    text = raw
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            text = text[end + 4:]
            if text.startswith("\n"):
                text = text[1:]
    return text.strip()


def load_skill(skill_key: str) -> Tuple[str, str]:
    """返回 (剥离子 YAML 的正文, sha256[:12])；文件缺失抛 RuntimeError。"""
    if skill_key in _skill_cache:
        return _skill_cache[skill_key]
    path = config.SKILL_FILES.get(skill_key)
    if path is None or not path.exists():
        raise RuntimeError(f"skill 文件缺失: {skill_key} -> {path}")
    raw = path.read_text(encoding="utf-8")
    body = _strip_yaml_front_matter(raw)
    sha = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    _skill_cache[skill_key] = (body, sha)
    return body, sha


def get_model_config() -> dict:
    cfg = config.load_config()
    base_url = (cfg.get("base_url") or "").strip().rstrip("/")
    api_key = (cfg.get("api_key") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not base_url or not api_key or not model:
        raise ModelNotConfigured("model_not_configured", "未配置模型 API：请到设置填写 base_url、api_key、model。")
    return {"base_url": base_url, "api_key": api_key, "model": model}


def _record_usage(*, kind: str, model: str, prompt_tokens, completion_tokens, latency_ms: int,
                  ok: bool, error: str | None, skill_name: str, skill_sha256: str) -> None:
    db.x(
        "INSERT INTO usage_logs (id, created_at, kind, model, prompt_tokens, completion_tokens,"
        " latency_ms, ok, error, skill_name, skill_sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (db.new_id(), db.now(), kind, model, prompt_tokens, completion_tokens, latency_ms,
         1 if ok else 0, error, skill_name, skill_sha256),
    )


def _extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ModelOutputInvalid("model_output_invalid", "模型输出不是 JSON 对象。")
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ModelOutputInvalid("model_output_invalid", f"模型输出 JSON 解析失败：{exc.msg}") from exc
    if not isinstance(obj, dict):
        raise ModelOutputInvalid("model_output_invalid", "模型输出不是 JSON 对象。")
    return obj


def _deepseek_payload(cfg: dict) -> dict:
    """DeepSeek V4.1 Flash 默认开启思考模式（temperature 不生效、可能吃满 token）。
    本项目的结构化 JSON 任务需要确定性输出，因此在 DeepSeek 端点显式关闭思考模式。
    其他 OpenAI 兼容厂商不发送该字段。"""
    base = (cfg.get("base_url") or "").lower()
    model = (cfg.get("model") or "").lower()
    if "deepseek" in base or model.startswith("deepseek"):
        return {"thinking": {"type": "disabled"}, "reasoning_effort": "none"}
    return {}


def _post(messages: list[dict], *, max_tokens: int, temperature: float, timeout: float) -> dict:
    cfg = get_model_config()
    url = cfg["base_url"] + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    payload.update(_deepseek_payload(cfg))
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    last_error = ""
    for attempt in (1, 2):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload, headers=headers)
            if resp.status_code >= 500:
                last_error = f"上游返回 HTTP {resp.status_code}"
                if attempt == 1:
                    time.sleep(2)
                    continue
                raise ModelRequestFailed("model_request_failed", last_error)
            if resp.status_code >= 400:
                detail = resp.text[:200]
                raise ModelRequestFailed("model_request_failed", f"上游返回 HTTP {resp.status_code}：{detail}")
            return resp.json()
        except (httpx.HTTPError, OSError) as exc:
            last_error = f"网络错误：{exc.__class__.__name__}"
            if attempt == 1:
                time.sleep(2)
                continue
            raise ModelRequestFailed("model_request_failed", last_error) from exc
    raise ModelRequestFailed("model_request_failed", last_error or "模型请求失败")


def _message_content(data: dict) -> str:
    try:
        return str(data["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as exc:
        raise ModelOutputInvalid("model_output_invalid", "模型响应缺少 choices[0].message.content") from exc


def chat_json(skill_key: str, user_content: str, *, kind: str, system_extra: str = "",
              max_tokens: int, temperature: float, timeout: float = 60) -> Tuple[dict, dict]:
    """调用模型并解析 JSON。成功后写 usage_logs；返回 (json_obj, usage)。"""
    skill_text, skill_sha = load_skill(skill_key)
    skill_name = skill_key
    system = skill_text if not system_extra else skill_text + "\n\n" + system_extra
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_content},
    ]
    started = time.time()
    data = _post(messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
    elapsed = int((time.time() - started) * 1000)
    usage_meta = data.get("usage") or {}
    model_name = str(data.get("model") or get_model_config()["model"])
    prompt_tokens = usage_meta.get("prompt_tokens")
    completion_tokens = usage_meta.get("completion_tokens")

    # 截断时先记录失败用量，自动加倍上限重试一次；仍截断则终止，避免重试同一上限。
    if _finish_reason(data) == "length":
        _record_usage(kind=kind, model=model_name, prompt_tokens=prompt_tokens,
                      completion_tokens=completion_tokens, latency_ms=elapsed, ok=False,
                      error="truncated", skill_name=skill_name, skill_sha256=skill_sha)
        bigger = min(max(max_tokens * 2, 2000), MAX_TOKENS_CAP)
        started = time.time()
        data = _post(messages, max_tokens=bigger, temperature=temperature, timeout=timeout)
        elapsed = int((time.time() - started) * 1000)
        usage_meta = data.get("usage") or {}
        model_name = str(data.get("model") or model_name)
        prompt_tokens = usage_meta.get("prompt_tokens")
        completion_tokens = usage_meta.get("completion_tokens")
        if _finish_reason(data) == "length":
            _record_usage(kind=kind, model=model_name, prompt_tokens=prompt_tokens,
                          completion_tokens=completion_tokens, latency_ms=elapsed, ok=False,
                          error="truncated", skill_name=skill_name, skill_sha256=skill_sha)
            raise ModelOutputInvalid(
                "model_output_invalid",
                f"模型输出被截断（finish_reason=length）：max_tokens={bigger} 仍不足，请缩小资料范围或重试。",
            )

    content = _message_content(data)
    try:
        obj = _extract_json(content)
    except ModelOutputInvalid:
        _record_usage(kind=kind, model=model_name, prompt_tokens=prompt_tokens,
                      completion_tokens=completion_tokens, latency_ms=elapsed, ok=False,
                      error="json_parse_failed", skill_name=skill_name, skill_sha256=skill_sha)
        retry_messages = messages + [
            {"role": "assistant", "content": content[:2000]},
            {"role": "user", "content": "上一次输出不是合法 JSON。请只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。"},
        ]
        started2 = time.time()
        data2 = _post(retry_messages, max_tokens=max_tokens, temperature=temperature, timeout=timeout)
        elapsed2 = int((time.time() - started2) * 1000)
        usage2 = data2.get("usage") or {}
        content2 = _message_content(data2)
        obj = _extract_json(content2)
        usage = {
            "model": str(data2.get("model") or model_name),
            "prompt_tokens": usage2.get("prompt_tokens"),
            "completion_tokens": usage2.get("completion_tokens"),
            "latency_ms": elapsed2,
            "ok": 1,
            "error": None,
            "skill_name": skill_name,
            "skill_sha256": skill_sha,
        }
        _record_usage(kind=kind, model=usage["model"], prompt_tokens=usage["prompt_tokens"],
                      completion_tokens=usage["completion_tokens"], latency_ms=elapsed2, ok=True,
                      error=None, skill_name=skill_name, skill_sha256=skill_sha)
        return obj, usage

    usage = {
        "model": model_name,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "latency_ms": elapsed,
        "ok": 1,
        "error": None,
        "skill_name": skill_name,
        "skill_sha256": skill_sha,
    }
    _record_usage(kind=kind, model=model_name, prompt_tokens=prompt_tokens,
                  completion_tokens=completion_tokens, latency_ms=elapsed, ok=True,
                  error=None, skill_name=skill_name, skill_sha256=skill_sha)
    return obj, usage


def test_connection() -> Tuple[bool, str]:
    cfg = get_model_config()
    url = cfg["base_url"] + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "max_tokens": 5,
        "temperature": 0,
        "messages": [{"role": "user", "content": "ping"}],
    }
    payload.update(_deepseek_payload(cfg))
    headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=20) as client:
            resp = client.post(url, json=payload, headers=headers)
        if resp.status_code == 200:
            return True, "连接成功"
        return False, f"HTTP {resp.status_code}：{resp.text[:200]}"
    except httpx.HTTPError as exc:
        return False, f"网络错误：{exc.__class__.__name__}"
