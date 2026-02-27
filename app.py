from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import AliasChoices, BaseModel, Field
from starlette.concurrency import run_in_threadpool

VERSION = "0.5.0-py"
ONYX_BASE_URL = os.getenv("ONYX_BASE_URL", "https://cloud.onyx.app").rstrip("/")
ONYX_AUTH_COOKIE = os.getenv("ONYX_AUTH_COOKIE", "")
ONYX_PERSONA_ID = int(os.getenv("ONYX_PERSONA_ID", "0"))
ONYX_ORIGIN = os.getenv("ONYX_ORIGIN", "webapp")
ONYX_REFERER = os.getenv("ONYX_REFERER", "https://cloud.onyx.app/app")
ONYX_BASE_DEFAULT = ONYX_BASE_URL
MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]
RETRY_STATUS = {502, 503, 504, 429, 401, 403}
ONYX_COOKIE_ERROR_LIMIT = max(int(os.getenv("ONYX_COOKIE_ERROR_LIMIT", "3")), 1)
CONFIG_FILE_PATH = "/data/config.json"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("onyx2api")


@dataclass
class ProviderModel:
    provider: str
    version: str


MODEL_MAP: dict[str, ProviderModel] = {
    "claude-opus-4.6": ProviderModel("Anthropic", "claude-opus-4-6"),
    "claude-opus-4.5": ProviderModel("Anthropic", "claude-opus-4-5"),
    "claude-sonnet-4.5": ProviderModel("Anthropic", "claude-sonnet-4-5"),
    "gpt-5.2": ProviderModel("OpenAI", "gpt-5.2"),
    "gpt-5-mini": ProviderModel("OpenAI", "gpt-5-mini"),
    "gpt-4.1": ProviderModel("OpenAI", "gpt-4.1"),
    "gpt-4o": ProviderModel("OpenAI", "gpt-4o"),
    "o3": ProviderModel("OpenAI", "o3"),
}


class AppConfig(BaseModel):
    onyx_base: str = ONYX_BASE_DEFAULT
    onyx_cookies: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("onyx_cookies", "onyx_keys"),
        serialization_alias="onyx_cookies",
    )
    client_api_keys: list[str] = Field(default_factory=list)
    default_persona: int = ONYX_PERSONA_ID if ONYX_PERSONA_ID > 0 else 1
    default_model: str = "claude-opus-4.6"
    request_timeout_seconds: int = 300
    admin_password: str = ""


class ConfigUpdate(BaseModel):
    onyx_base: str = ONYX_BASE_DEFAULT
    onyx_cookies: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("onyx_cookies", "onyx_keys"),
        serialization_alias="onyx_cookies",
    )
    client_api_keys: list[str] = Field(default_factory=list)
    default_persona: int = ONYX_PERSONA_ID if ONYX_PERSONA_ID > 0 else 1
    default_model: str = "claude-opus-4.6"
    request_timeout_seconds: int = 300
    admin_password: str | None = None


class AppendOnyxCookieReq(BaseModel):
    cookie: str = Field(default="", validation_alias=AliasChoices("cookie", "key"), serialization_alias="cookie")


def generate_admin_password() -> str:
    return f"adm-{secrets.token_urlsafe(12)}"


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cfg = AppConfig()

    @staticmethod
    def _cookie_identity(cookie: str) -> str:
        raw = cookie.strip()
        if not raw:
            return ""

        pairs: dict[str, str] = {}
        for piece in raw.split(";"):
            seg = piece.strip()
            if not seg or "=" not in seg:
                continue
            key, value = seg.split("=", 1)
            k = key.strip()
            if not k:
                continue
            pairs[k] = value.strip()

        csrf = pairs.get("fastapiusersoauthcsrf", "").strip()
        if csrf:
            return f"csrf:{csrf}"

        auth = pairs.get("fastapiusersauth", "").strip()
        if auth:
            return f"auth:{auth}"

        return f"raw:{raw}"

    @classmethod
    def _norm_keys(cls, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            v = item.strip()
            if not v:
                continue
            key = cls._cookie_identity(v) or f"raw:{v}"
            if key in seen:
                continue
            seen.add(key)
            out.append(v)
        return out

    def _normalize(self, cfg: AppConfig) -> AppConfig:
        cfg.onyx_cookies = self._norm_keys(cfg.onyx_cookies)
        cfg.client_api_keys = self._norm_keys(cfg.client_api_keys)
        if cfg.client_api_keys and not cfg.onyx_cookies:
            raise ValueError("client_api_keys 已配置时，onyx_cookies 不能为空")
        if cfg.default_persona <= 0:
            cfg.default_persona = 1
        if cfg.request_timeout_seconds < 30:
            cfg.request_timeout_seconds = 30
        if not cfg.default_model.strip():
            cfg.default_model = "claude-opus-4.6"
        if not cfg.onyx_base.strip():
            cfg.onyx_base = ONYX_BASE_DEFAULT
        if not cfg.admin_password.strip():
            cfg.admin_password = ADMIN_PASSWORD if ADMIN_PASSWORD else generate_admin_password()
        return cfg

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self._cfg = self._normalize(AppConfig())
                self.path.write_text(self._cfg.model_dump_json(indent=2), encoding="utf-8")
                return
            raw = self.path.read_text(encoding="utf-8").strip()
            if not raw:
                self._cfg = self._normalize(AppConfig())
                self.path.write_text(self._cfg.model_dump_json(indent=2), encoding="utf-8")
                return
            data = json.loads(raw)
            self._cfg = self._normalize(AppConfig(**data))
            self.path.write_text(self._cfg.model_dump_json(indent=2), encoding="utf-8")

    def get(self) -> AppConfig:
        with self._lock:
            return AppConfig(**self._cfg.model_dump())

    def set(self, payload: AppConfig) -> AppConfig:
        with self._lock:
            cfg = self._normalize(payload)
            self._cfg = cfg
            self.path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
            return AppConfig(**cfg.model_dump())

    def append_onyx_cookie(self, cookie: str) -> tuple[AppConfig, bool]:
        with self._lock:
            v = cookie.strip()
            if not v:
                raise ValueError("cookie 不能为空")

            incoming_key = self._cookie_identity(v)
            exists = any(self._cookie_identity(item) == incoming_key for item in self._cfg.onyx_cookies)

            if not exists:
                self._cfg.onyx_cookies.append(v)
                self._cfg = self._normalize(self._cfg)
                self.path.write_text(self._cfg.model_dump_json(indent=2), encoding="utf-8")
            return AppConfig(**self._cfg.model_dump()), (not exists)


class ChatMsg(BaseModel):
    role: str
    content: Any
    tool_call_id: str | None = None


class OpenAIReq(BaseModel):
    model: str = ""
    messages: list[ChatMsg]
    stream: bool = False
    temperature: float | None = None
    persona_id: int | None = None


class AnthropicReq(BaseModel):
    model: str = ""
    messages: list[ChatMsg]
    system: Any | None = None
    stream: bool = False
    max_tokens: int | None = None
    temperature: float | None = None
    persona_id: int | None = None


BASE_DIR = Path(__file__).resolve().parent

# Make config path absolute if it's not already
config_path_obj = Path(CONFIG_FILE_PATH)
if not config_path_obj.is_absolute():
    config_path_obj = BASE_DIR / config_path_obj

# Ensure the directory exists (e.g. /data)
config_path_obj.parent.mkdir(parents=True, exist_ok=True)

store = ConfigStore(config_path_obj)
store.load()

app = FastAPI(title="onyx2api-py", version=VERSION)


@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        response = Response(status_code=204)
    else:
        response = await call_next(request)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Credentials"] = "false"
    return response


templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

http: httpx.AsyncClient | None = None


@app.on_event("startup")
async def on_startup() -> None:
    global http
    limits = httpx.Limits(max_connections=200, max_keepalive_connections=100)
    http = httpx.AsyncClient(http2=True, limits=limits)
    cfg = store.get()
    logger.info("管理页面密码：admin_password=%s", cfg.admin_password)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if http is not None:
        await http.aclose()

_cookie_lock = threading.Lock()
_cookie_index = 0
_cookie_error_lock = threading.Lock()
_cookie_error_counts: dict[str, int] = {}


class OnyxHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Onyx HTTP {status}: {body}")
        self.status = status
        self.body = body


class OnyxUpstreamRejectedError(RuntimeError):
    pass


def gen_id(prefix: str) -> str:
    return f"{prefix}{secrets.token_hex(15)[:29]}"


def extract_token(value: str | None) -> str:
    if not value:
        return ""
    s = value.strip()
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        return s[7:].strip()
    return s


def extract_cookie_source(value: str | None) -> str:
    if not value:
        return ""
    s = value.strip()
    if not s:
        return ""
    if s.lower().startswith("bearer "):
        return s[7:].strip()
    return s


def _split_cookie_pairs(cookie_str: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for piece in cookie_str.split(";"):
        seg = piece.strip()
        if not seg or "=" not in seg:
            continue
        key, value = seg.split("=", 1)
        k = key.strip()
        v = value.strip()
        if not k:
            continue
        pairs[k] = v
    return pairs


def _extract_auth_value(cookie_str: str) -> str:
    # 兼容完整 Cookie 串或仅 fastapiusersauth 的值
    raw = cookie_str.strip()
    if not raw:
        return ""

    pairs = _split_cookie_pairs(raw)
    auth = pairs.get("fastapiusersauth", "").strip()
    if auth:
        return auth

    # 兼容旧格式：直接存 fastapiusersauth 的值
    return raw


def _extract_csrf_value(cookie_str: str) -> str:
    raw = cookie_str.strip()
    if not raw:
        return ""
    return _split_cookie_pairs(raw).get("fastapiusersoauthcsrf", "").strip()


def _build_cookie_string(auth_value: str, csrf_value: str = "") -> str:
    auth = auth_value.strip()
    csrf = csrf_value.strip()
    if not auth:
        return ""
    if csrf:
        return f"fastapiusersauth={auth}; fastapiusersoauthcsrf={csrf}"
    return f"fastapiusersauth={auth}"


def _build_onyx_request_cookies(cookie_str: str) -> dict[str, str]:
    auth = _extract_auth_value(cookie_str)
    csrf = _extract_csrf_value(cookie_str)
    out: dict[str, str] = {}
    if auth:
        out["fastapiusersauth"] = auth
    if csrf:
        out["fastapiusersoauthcsrf"] = csrf
    return out


def _extract_set_cookie_value(set_cookie_header: str, cookie_name: str) -> str:
    target = f"{cookie_name}="
    for piece in set_cookie_header.split(";"):
        seg = piece.strip()
        if seg.startswith(target):
            return seg.split("=", 1)[1].strip()
    return ""


def _cookie_error_identifier(cookie_str: str) -> str:
    csrf = _extract_csrf_value(cookie_str)
    if csrf:
        return f"csrf:{csrf}"
    auth = _extract_auth_value(cookie_str).strip()
    return f"auth:{auth}" if auth else ""


def is_upstream_rejected_error(message: str) -> bool:
    msg = str(message or "").strip().lower()
    if not msg:
        return False

    # 模糊匹配：避免上游文案有轻微变化
    if "unexpected error occurred while processing your request" in msg:
        return True
    if "unexpected error" in msg and "try again later" in msg:
        return True
    return False


def build_onyx_headers(cfg: AppConfig, with_json: bool = False) -> dict[str, str]:
    base = cfg.onyx_base.rstrip("/")
    headers = {
        "accept": "application/json",
        "origin": base,
        "referer": ONYX_REFERER,
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 Edg/145.0.0.0"
        ),
    }
    if with_json:
        headers["content-type"] = "application/json"
    return headers


async def refresh_onyx_auth_cookie(cfg: AppConfig, cookie_str: str) -> str:
    if http is None:
        return ""

    req_cookies = _build_onyx_request_cookies(cookie_str)
    auth = req_cookies.get("fastapiusersauth", "").strip()
    csrf = req_cookies.get("fastapiusersoauthcsrf", "").strip()
    # 有些场景只有 fastapiusersauth，也允许尝试 refresh
    if not auth:
        return ""

    try:
        response = await http.post(
            f"{cfg.onyx_base.rstrip('/')}/api/auth/refresh",
            headers=build_onyx_headers(cfg),
            cookies=req_cookies,
            timeout=httpx.Timeout(float(cfg.request_timeout_seconds), connect=20.0),
        )
    except httpx.HTTPError as exc:
        logger.warning("Cookie refresh request failed: %s", exc)
        return ""

    if response.status_code < 200 or response.status_code >= 300:
        logger.warning("Cookie refresh failed with HTTP %s", response.status_code)
        return ""

    set_cookie_values = response.headers.get_list("set-cookie")
    if not set_cookie_values:
        single = response.headers.get("set-cookie", "")
        if single:
            set_cookie_values = [single]

    new_auth = ""
    new_csrf = ""
    for set_cookie in set_cookie_values:
        if not new_auth:
            new_auth = _extract_set_cookie_value(set_cookie, "fastapiusersauth")
        if not new_csrf:
            new_csrf = _extract_set_cookie_value(set_cookie, "fastapiusersoauthcsrf")
        if new_auth and new_csrf:
            break

    final_auth = (new_auth or auth).strip()
    final_csrf = (new_csrf or csrf).strip()

    if not final_auth:
        logger.warning("Cookie refresh succeeded but fastapiusersauth is empty after merge")
        return ""

    refreshed_cookie = _build_cookie_string(final_auth, final_csrf)
    current_cookie = _build_cookie_string(auth, csrf)
    if refreshed_cookie == current_cookie:
        return ""

    return refreshed_cookie


async def persist_refreshed_cookie(cfg: AppConfig, old_cookie: str, new_cookie: str) -> None:
    old = old_cookie.strip()
    new_val = new_cookie.strip()
    if not old or not new_val:
        return

    if not cfg.onyx_cookies:
        return

    old_auth = _extract_auth_value(old)
    old_csrf = _extract_csrf_value(old)

    replaced = False
    next_cookies: list[str] = []
    for item in cfg.onyx_cookies:
        item_auth = _extract_auth_value(item)
        item_csrf = _extract_csrf_value(item)
        should_replace = (
            item == old
            or (old_csrf and item_csrf == old_csrf)
            or (old_auth and item_auth == old_auth)
        )

        if should_replace and not replaced:
            next_cookies.append(new_val)
            replaced = True
            continue

        if should_replace and replaced:
            continue

        next_cookies.append(item)

    if not replaced:
        return

    cfg.onyx_cookies = next_cookies

    try:
        latest = await run_in_threadpool(store.get)
        latest_auth = _extract_auth_value(old)
        latest_csrf = _extract_csrf_value(old)
        merged: list[str] = []
        merged_replaced = False

        for item in latest.onyx_cookies:
            item_auth = _extract_auth_value(item)
            item_csrf = _extract_csrf_value(item)
            same_cookie = (
                item == old
                or (latest_csrf and item_csrf == latest_csrf)
                or (latest_auth and item_auth == latest_auth)
            )

            if same_cookie and not merged_replaced:
                merged.append(new_val)
                merged_replaced = True
                continue

            if same_cookie and merged_replaced:
                continue

            merged.append(item)

        if merged_replaced:
            latest.onyx_cookies = merged
            saved = await run_in_threadpool(store.set, latest)
            cfg.onyx_cookies = saved.onyx_cookies
    except Exception as exc:  # noqa: BLE001
        logger.warning("Persist refreshed cookie failed: %s", exc)


def next_cookie(cookies: list[str]) -> str:
    global _cookie_index
    if not cookies:
        return ""
    with _cookie_lock:
        idx = _cookie_index % len(cookies)
        _cookie_index += 1
    return cookies[idx]


def clear_cookie_error_count(cookie_str: str) -> None:
    cookie_id = _cookie_error_identifier(cookie_str)
    if not cookie_id:
        return
    with _cookie_error_lock:
        _cookie_error_counts.pop(cookie_id, None)


def get_failed_cookie_items(cfg: AppConfig) -> list[dict[str, Any]]:
    cookie_by_id: dict[str, str] = {}
    for item in cfg.onyx_cookies:
        cookie_id = _cookie_error_identifier(item)
        if cookie_id:
            cookie_by_id[cookie_id] = item

    with _cookie_error_lock:
        snapshot = dict(_cookie_error_counts)
        stale_ids = [cookie_id for cookie_id in snapshot if cookie_id not in cookie_by_id]
        for cookie_id in stale_ids:
            _cookie_error_counts.pop(cookie_id, None)

    items: list[dict[str, Any]] = []
    for cookie_id, fail_count in snapshot.items():
        if fail_count <= 0:
            continue
        cookie = cookie_by_id.get(cookie_id, "")
        if not cookie:
            continue
        items.append({"cookie": cookie, "fail_count": fail_count})

    items.sort(key=lambda x: int(x.get("fail_count", 0)), reverse=True)
    return items


async def mark_cookie_error(cfg: AppConfig, cookie_str: str, reason: str) -> None:
    cookie_id = _cookie_error_identifier(cookie_str)
    if not cookie_id:
        return

    with _cookie_error_lock:
        fail_count = _cookie_error_counts.get(cookie_id, 0) + 1
        _cookie_error_counts[cookie_id] = fail_count

    logger.warning(
        "Cookie auth failed (%s/%s): %s",
        fail_count,
        ONYX_COOKIE_ERROR_LIMIT,
        reason,
    )

    if fail_count < ONYX_COOKIE_ERROR_LIMIT:
        return

    if not cfg.onyx_cookies:
        return

    cur_cfg = await run_in_threadpool(store.get)
    if not cur_cfg.onyx_cookies:
        return

    target = cookie_str.strip()
    target_auth = _extract_auth_value(cookie_str)
    target_csrf = _extract_csrf_value(cookie_str)
    kept_cookies: list[str] = []
    removed_count = 0

    for item in cur_cfg.onyx_cookies:
        item_auth = _extract_auth_value(item)
        item_csrf = _extract_csrf_value(item)
        if (
            item == target
            or (target_csrf and item_csrf == target_csrf)
            or (target_auth and item_auth == target_auth)
        ):
            removed_count += 1
            continue
        kept_cookies.append(item)

    if removed_count <= 0:
        return

    cur_cfg.onyx_cookies = kept_cookies
    try:
        saved_cfg = await run_in_threadpool(store.set, cur_cfg)
    except ValueError as exc:
        logger.error("Cookie reached error limit but cannot be removed: %s", exc)
        return

    cfg.onyx_cookies = saved_cfg.onyx_cookies
    with _cookie_lock:
        if cfg.onyx_cookies:
            _cookie_index %= len(cfg.onyx_cookies)
        else:
            _cookie_index = 0

    with _cookie_error_lock:
        _cookie_error_counts.pop(cookie_id, None)

    logger.error(
        "Removed invalid Onyx cookie after %s failures, remaining cookies=%s",
        fail_count,
        len(cfg.onyx_cookies),
    )


def resolve_auth_cookie(cfg: AppConfig, *headers: str | None) -> str:
    if cfg.onyx_cookies:
        return next_cookie(cfg.onyx_cookies)
    for h in headers:
        cookie_src = extract_cookie_source(h)
        if cookie_src:
            return cookie_src
    if ONYX_AUTH_COOKIE:
        return ONYX_AUTH_COOKIE
    return ""

def check_client_auth(cfg: AppConfig, request: Request) -> bool:
    if not cfg.client_api_keys:
        return True
    token = request.headers.get("x-api-key", "").strip()
    if not token:
        token = extract_token(request.headers.get("authorization"))
    return token in set(cfg.client_api_keys)


def check_admin_auth(cfg: AppConfig, request: Request) -> bool:
    header_pwd = request.headers.get("x-admin-password", "").strip()
    if not header_pwd:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            header_pwd = auth[7:].strip()
    return bool(header_pwd) and secrets.compare_digest(header_pwd, cfg.admin_password)


def text_content(raw: Any) -> str:
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return str(raw)


def to_str_slice(v: Any) -> list[str]:
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for item in v:
        if isinstance(item, str):
            out.append(item)
    return out


def build_llm_override(model: str, temp: float) -> dict[str, Any]:
    pm = MODEL_MAP.get(model)
    if pm is None:
        parts = model.split("__", 2)
        if len(parts) == 3:
            pm = ProviderModel(parts[0], parts[2])
        else:
            pm = ProviderModel("Anthropic", model)
    return {
        "model_provider": pm.provider,
        "model_version": pm.version,
        "temperature": temp,
    }


def messages_to_onyx(system: str, msgs: list[ChatMsg]) -> str:
    conv: list[str] = []
    last_user = ""

    for msg in msgs:
        c = text_content(msg.content)
        if msg.role == "system":
            if not system:
                system = c
            else:
                system += "\n" + c
        elif msg.role == "user":
            last_user = c
            conv.append(f"User: {c}")
        elif msg.role == "assistant":
            conv.append(f"Assistant: {c}")
        elif msg.role == "tool":
            tid = msg.tool_call_id or "unknown"
            conv.append(f"Tool result ({tid}): {c}")

    if not system and len(conv) == 1 and len(msgs) == 1 and msgs[0].role == "user":
        return last_user
    if system and len(conv) == 1 and len(msgs) >= 1:
        return f"[System: {system}]\n\n{last_user}"

    out = []
    if system:
        out.append(f"[System: {system}]\n")
    out.append("\n".join(conv))
    return "\n".join(out).strip()


async def create_chat_session(cfg: AppConfig, auth_cookie: str, persona: int) -> tuple[str, str]:
    if http is None:
        raise RuntimeError("HTTP client is not initialized")

    refreshed_cookie = await refresh_onyx_auth_cookie(cfg, auth_cookie)
    effective_cookie = refreshed_cookie or auth_cookie
    req_cookies = _build_onyx_request_cookies(effective_cookie)

    response = await http.post(
        f"{cfg.onyx_base.rstrip('/')}/api/chat/create-chat-session",
        headers=build_onyx_headers(cfg, with_json=True),
        json={"persona_id": persona, "description": None, "project_id": None},
        cookies=req_cookies,
        timeout=httpx.Timeout(float(cfg.request_timeout_seconds), connect=30.0),
    )
    if response.status_code == 401:
        raise RuntimeError("Onyx auth failed - check onyx cookie")
    if response.status_code != 200:
        raise RuntimeError(f"Onyx create-chat-session HTTP {response.status_code}: {response.text[:300]}")

    data = response.json()
    chat_session_id = data.get("chat_session_id") or data.get("id")
    if not chat_session_id:
        raise RuntimeError(f"create-chat-session missing chat_session_id: {data}")

    if refreshed_cookie:
        await persist_refreshed_cookie(cfg, auth_cookie, refreshed_cookie)

    return str(chat_session_id), effective_cookie


async def do_onyx_request(
    cfg: AppConfig,
    auth_cookie: str,
    model: str,
    msgs: list[ChatMsg],
    system: str,
    temp: float,
    persona: int,
) -> httpx.Response:
    if http is None:
        raise RuntimeError("HTTP client is not initialized")

    total_cookies = max(len(cfg.onyx_cookies), 1)
    max_attempts = total_cookies * MAX_RETRIES
    last_err: Exception | None = None

    for attempt in range(max_attempts):
        current_total = max(len(cfg.onyx_cookies), 1)
        cur_cookie = auth_cookie if attempt == 0 else (next_cookie(cfg.onyx_cookies) if cfg.onyx_cookies else auth_cookie)
        cookie_idx = attempt % current_total
        round_num = attempt // current_total

        if attempt > 0:
            logger.warning(
                "Retry %s/%s (cookie %s/%s, round %s), switching cookie",
                attempt,
                max_attempts,
                cookie_idx + 1,
                current_total,
                round_num + 1,
            )

        try:
            chat_session_id, effective_cookie = await create_chat_session(cfg, cur_cookie, persona)
            body = {
                "message": messages_to_onyx(system, msgs),
                "chat_session_id": chat_session_id,
                "parent_message_id": None,
                "file_descriptors": [],
                "internal_search_filters": {
                    "source_type": None,
                    "document_set": None,
                    "time_cutoff": None,
                    "tags": [],
                },
                "deep_research": False,
                "forced_tool_id": None,
                "llm_override": build_llm_override(model, temp),
                "origin": ONYX_ORIGIN,
            }

            req = http.build_request(
                "POST",
                f"{cfg.onyx_base.rstrip('/')}/api/chat/send-chat-message",
                json=body,
                headers=build_onyx_headers(cfg, with_json=True),
                cookies=_build_onyx_request_cookies(effective_cookie),
                timeout=httpx.Timeout(float(cfg.request_timeout_seconds), connect=30.0),
            )
            resp = await http.send(req, stream=True)
        except (httpx.HTTPError, RuntimeError) as exc:
            last_err = exc
            await mark_cookie_error(cfg, cur_cookie, str(exc))
            wait = RETRY_BACKOFF[min(round_num, len(RETRY_BACKOFF) - 1)]
            logger.warning("Attempt %s failed (%s), retry in %ss", attempt + 1, exc, wait)
            await asyncio.sleep(wait)
            continue

        if resp.status_code != 200:
            body_bytes = await resp.aread()
            body_text = body_bytes.decode("utf-8", errors="ignore")[:1024]
            status = resp.status_code
            await resp.aclose()
            logger.warning("Onyx HTTP %s for model=%s (cookie %s/%s): %s", status, model, cookie_idx + 1, current_total, body_text)

            if "unexpected error" in body_text.lower() or "try again later" in body_text.lower():
                status = 503

            last_err = OnyxHTTPError(status, body_text)
            await mark_cookie_error(cfg, cur_cookie, f"HTTP {status}")
            wait = RETRY_BACKOFF[min(round_num, len(RETRY_BACKOFF) - 1)]
            logger.warning("Attempt %s failed (HTTP %s), trying next cookie in %ss", attempt + 1, status, wait)
            await asyncio.sleep(wait)
            continue

        resp.extensions["onyx_cookie"] = cur_cookie
        clear_cookie_error_count(cur_cookie)
        return resp

    raise last_err if last_err is not None else RuntimeError("all retries failed")

async def iter_onyx_events(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    async for line in resp.aiter_lines():
        text = line.strip()
        if not text:
            continue
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            continue
        if raw.get("user_message_id") is not None:
            continue
        if raw.get("error") is not None and raw.get("obj") is None:
            yield {"type": "error", "err": str(raw.get("error")), "obj": {}}
            continue
        obj = raw.get("obj")
        if not isinstance(obj, dict):
            continue
        yield {"type": str(obj.get("type", "")), "err": "", "obj": obj}




async def safe_iter_onyx_events(
    cfg: AppConfig,
    auth_cookie: str,
    model: str,
    msgs: list[ChatMsg],
    system: str,
    temp: float,
    persona: int,
) -> AsyncIterator[tuple[httpx.Response, AsyncIterator[dict[str, Any]]]]:
    """包装 iter_onyx_events，遇到流式错误自动换 cookie 重试"""
    total_cookies = max(len(cfg.onyx_cookies), 1)
    max_stream_retries = total_cookies * 2

    for stream_attempt in range(max_stream_retries):
        cur_cookie = auth_cookie if stream_attempt == 0 else (next_cookie(cfg.onyx_cookies) if cfg.onyx_cookies else auth_cookie)

        try:
            resp = await do_onyx_request(
                cfg,
                cur_cookie,
                model,
                msgs,
                system,
                temp,
                cfg.default_persona if persona is None else persona,
            )
            used_cookie = str(resp.extensions.get("onyx_cookie") or cur_cookie)
        except Exception as exc:
            logger.error("do_onyx_request failed on stream attempt %s: %s", stream_attempt + 1, exc)
            if stream_attempt < max_stream_retries - 1:
                await asyncio.sleep(2)
                continue
            raise

        has_error = False
        error_msg = ""
        upstream_rejected = False

        async def checked_events() -> AsyncIterator[dict[str, Any]]:
            nonlocal has_error, error_msg, upstream_rejected
            async for ev in iter_onyx_events(resp):
                if ev.get("err"):
                    has_error = True
                    error_msg = str(ev.get("err") or "")
                    upstream_rejected = is_upstream_rejected_error(error_msg)
                    logger.error("Stream error on attempt %s: %s", stream_attempt + 1, error_msg)
                    if upstream_rejected:
                        raise OnyxUpstreamRejectedError(error_msg)
                    return

                etype = ev.get("type", "")
                obj = ev.get("obj", {})
                if etype == "error":
                    has_error = True
                    error_msg = str(obj.get("error") or ev.get("err") or "Unknown error")
                    upstream_rejected = is_upstream_rejected_error(error_msg)
                    logger.error("Stream error event on attempt %s: %s", stream_attempt + 1, error_msg)
                    if upstream_rejected:
                        raise OnyxUpstreamRejectedError(error_msg)
                    return

                yield ev

        yield resp, checked_events()

        if not has_error:
            clear_cookie_error_count(used_cookie)
            return

        if upstream_rejected:
            await mark_cookie_error(cfg, used_cookie, f"upstream rejected stream: {error_msg}")

        await resp.aclose()
        logger.warning(
            "Stream had error, retrying with next cookie (%s/%s): %s",
            stream_attempt + 1,
            max_stream_retries,
            error_msg,
        )
        await asyncio.sleep(2)

    raise RuntimeError("All stream retry attempts failed")

def sse(data: Any) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


def make_chunk(
    rid: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish: str | None,
) -> dict[str, Any]:
    return {
        "id": rid,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish,
            }
        ],
    }


async def stream_openai(events: AsyncIterator[dict[str, Any]], model: str, rid: str) -> AsyncIterator[bytes]:
    created = int(time.time())
    sent_role = False
    tool_active = False

    async for ev in events:
        etype = ev["type"]
        obj = ev["obj"]

        if ev["err"]:
            err_msg = str(ev["err"])
            logger.error("Stream error (skipped, not sent to client): %s", err_msg)
            if is_upstream_rejected_error(err_msg):
                raise OnyxUpstreamRejectedError(err_msg)
            continue

        if etype in {"reasoning_start", "reasoning_done", "image_generation_heartbeat"}:
            continue

        if etype == "reasoning_delta":
            s = obj.get("reasoning")
            if isinstance(s, str) and s:
                yield sse(make_chunk(rid, created, model, {"reasoning_content": s}, None))
            continue

        if etype == "message_start":
            if not sent_role:
                yield sse(make_chunk(rid, created, model, {"role": "assistant", "content": ""}, None))
                sent_role = True
            continue

        if etype == "message_delta":
            c = obj.get("content")
            if isinstance(c, str) and c:
                delta: dict[str, Any] = {"content": c}
                if not sent_role:
                    delta["role"] = "assistant"
                    sent_role = True
                yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype == "search_tool_start":
            tool_active = True
            label = "Web Search" if obj.get("is_internet_search") is True else "Internal Search"
            delta = {"content": f"\n[{label}] "}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype == "search_tool_queries_delta":
            qs = to_str_slice(obj.get("queries"))
            if qs:
                quoted = ", ".join([f"“{q}”" for q in qs])
                yield sse(make_chunk(rid, created, model, {"content": f"Searching: {quoted}\n"}, None))
            continue

        if etype == "search_tool_documents_delta":
            docs = obj.get("documents")
            if isinstance(docs, list) and docs:
                yield sse(make_chunk(rid, created, model, {"content": f"Found {len(docs)} results.\n"}, None))
            continue

        if etype == "open_url_start":
            tool_active = True
            delta = {"content": "\n[Opening URL] "}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype == "open_url_urls":
            urls = to_str_slice(obj.get("urls"))
            if urls:
                yield sse(make_chunk(rid, created, model, {"content": ", ".join(urls) + "\n"}, None))
            continue

        if etype == "open_url_documents":
            docs = obj.get("documents")
            if isinstance(docs, list) and docs:
                yield sse(make_chunk(rid, created, model, {"content": f"Loaded {len(docs)} pages.\n"}, None))
            continue

        if etype == "python_tool_start":
            tool_active = True
            code = obj.get("code")
            text = "\n[Code Interpreter]\n"
            if isinstance(code, str) and code:
                text += f"```python\n{code}\n```\n"
            delta = {"content": text}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype == "python_tool_delta":
            parts: list[str] = []
            stdout = obj.get("stdout")
            stderr = obj.get("stderr")
            if isinstance(stdout, str) and stdout:
                parts.append(f"Output: {stdout}")
            if isinstance(stderr, str) and stderr:
                parts.append(f"Error: {stderr}")
            if parts:
                yield sse(make_chunk(rid, created, model, {"content": "\n".join(parts) + "\n"}, None))
            continue

        if etype == "custom_tool_start":
            tool_active = True
            tool_name = obj.get("tool_name") if isinstance(obj.get("tool_name"), str) else "custom_tool"
            delta = {"content": f"\n[Tool: {tool_name}]\n"}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype == "custom_tool_delta":
            data = obj.get("data")
            if data is not None:
                txt = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
                if txt:
                    yield sse(make_chunk(rid, created, model, {"content": txt + "\n"}, None))
            continue

        if etype == "image_generation_start":
            tool_active = True
            delta = {"content": "\n[Generating Image...]\n"}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype == "image_generation_final":
            images = obj.get("images")
            if isinstance(images, list):
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    url = image.get("url")
                    prompt = image.get("revised_prompt")
                    if isinstance(url, str) and url:
                        p = prompt if isinstance(prompt, str) else "image"
                        yield sse(make_chunk(rid, created, model, {"content": f"![{p}]({url})\n"}, None))
            continue

        if etype == "file_reader_start":
            tool_active = True
            delta = {"content": "\n[Reading File...]\n"}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype == "file_reader_result":
            fn = obj.get("file_name")
            if isinstance(fn, str) and fn:
                yield sse(make_chunk(rid, created, model, {"content": f"Read: {fn}\n"}, None))
            continue

        if etype in {"deep_research_plan_start", "intermediate_report_start"}:
            delta = {"content": "\n[Research Plan]\n"}
            if not sent_role:
                delta["role"] = "assistant"
                sent_role = True
            yield sse(make_chunk(rid, created, model, delta, None))
            continue

        if etype in {"deep_research_plan_delta", "intermediate_report_delta"}:
            c = obj.get("content")
            if isinstance(c, str) and c:
                yield sse(make_chunk(rid, created, model, {"content": c}, None))
            continue

        if etype == "research_agent_start":
            task = obj.get("research_task")
            if isinstance(task, str):
                yield sse(make_chunk(rid, created, model, {"content": f"\n[Researching: {task}]\n"}, None))
            continue

        if etype == "section_end":
            if tool_active:
                yield sse(make_chunk(rid, created, model, {"content": "\n"}, None))
                tool_active = False
            continue

        if etype == "error":
            msg = obj.get("error") if isinstance(obj.get("error"), str) else "Unknown error"
            logger.error("Stream event error (skipped, not sent to client): %s", msg)
            continue

        if etype == "stop":
            yield sse(make_chunk(rid, created, model, {}, "stop"))
            yield b"data: [DONE]\n\n"
            return

    yield b"data: [DONE]\n\n"


async def collect_openai(events: AsyncIterator[dict[str, Any]]) -> str:
    parts: list[str] = []
    tool_ctx: list[str] = []

    async for ev in events:
        etype = ev["type"]
        obj = ev["obj"]
        if ev["err"]:
            err_msg = str(ev["err"])
            logger.error("Collect error (skipped): %s", err_msg)
            if is_upstream_rejected_error(err_msg):
                raise OnyxUpstreamRejectedError(err_msg)
            continue

        if etype == "message_delta":
            c = obj.get("content")
            if isinstance(c, str) and c:
                parts.append(c)
        elif etype == "search_tool_start":
            label = "Web Search" if obj.get("is_internet_search") is True else "Internal Search"
            tool_ctx.append(f"[{label}]")
        elif etype == "search_tool_queries_delta":
            qs = to_str_slice(obj.get("queries"))
            if qs:
                tool_ctx.append("Searching: " + ", ".join(qs))
        elif etype == "search_tool_documents_delta":
            docs = obj.get("documents")
            if isinstance(docs, list) and docs:
                tool_ctx.append(f"Found {len(docs)} results.")
        elif etype == "open_url_start":
            tool_ctx.append("[Opening URL]")
        elif etype == "open_url_urls":
            urls = to_str_slice(obj.get("urls"))
            if urls:
                tool_ctx.append(", ".join(urls))
        elif etype == "python_tool_start":
            code = obj.get("code") if isinstance(obj.get("code"), str) else ""
            tool_ctx.append(f"[Code Interpreter]\n```python\n{code}\n```")
        elif etype == "python_tool_delta":
            stdout = obj.get("stdout")
            stderr = obj.get("stderr")
            if isinstance(stdout, str) and stdout:
                tool_ctx.append(f"Output: {stdout}")
            if isinstance(stderr, str) and stderr:
                tool_ctx.append(f"Error: {stderr}")
        elif etype == "image_generation_final":
            images = obj.get("images")
            if isinstance(images, list):
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    url = image.get("url")
                    prompt = image.get("revised_prompt")
                    if isinstance(url, str) and url:
                        p = prompt if isinstance(prompt, str) else "image"
                        tool_ctx.append(f"![{p}]({url})")
        elif etype == "custom_tool_start":
            tool_name = obj.get("tool_name") if isinstance(obj.get("tool_name"), str) else "custom_tool"
            tool_ctx.append(f"[Tool: {tool_name}]")
        elif etype == "custom_tool_delta":
            data = obj.get("data")
            if data is not None:
                tool_ctx.append(str(data))
        elif etype == "error":
            msg = obj.get("error") if isinstance(obj.get("error"), str) else "Unknown error"
            logger.error("Collect event error (skipped): %s", msg)
            continue
        elif etype == "stop":
            break

    out = []
    if tool_ctx:
        out.append("\n".join(tool_ctx))
        out.append("")
    out.append("".join(parts))
    return "\n".join(out)


def anthropic_sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


async def stream_anthropic(events: AsyncIterator[dict[str, Any]], model: str, rid: str) -> AsyncIterator[bytes]:
    block_idx = 0
    in_thinking = False
    in_text = False

    yield anthropic_sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": rid,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    def start_block(btype: str) -> bytes:
        block: dict[str, Any] = {"type": btype}
        if btype == "thinking":
            block["thinking"] = ""
        else:
            block["text"] = ""
        return anthropic_sse(
            "content_block_start",
            {"type": "content_block_start", "index": block_idx, "content_block": block},
        )

    def stop_block() -> bytes:
        return anthropic_sse("content_block_stop", {"type": "content_block_stop", "index": block_idx})

    async for ev in events:
        etype = ev["type"]
        obj = ev["obj"]

        if ev["err"]:
            logger.error("Anthropic stream error (skipped): %s", ev["err"])
            continue

        if etype == "reasoning_start":
            if not in_thinking:
                yield start_block("thinking")
                in_thinking = True
            continue

        if etype == "reasoning_delta":
            s = obj.get("reasoning")
            if isinstance(s, str) and s:
                if not in_thinking:
                    yield start_block("thinking")
                    in_thinking = True
                yield anthropic_sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": block_idx,
                        "delta": {"type": "thinking_delta", "thinking": s},
                    },
                )
            continue

        if etype == "reasoning_done":
            if in_thinking:
                yield stop_block()
                in_thinking = False
                block_idx += 1
            continue

        if etype in {
            "message_start",
            "message_delta",
            "search_tool_start",
            "search_tool_queries_delta",
            "search_tool_documents_delta",
            "open_url_start",
            "open_url_urls",
            "open_url_documents",
            "python_tool_start",
            "python_tool_delta",
            "custom_tool_start",
            "custom_tool_delta",
            "image_generation_start",
            "image_generation_final",
            "file_reader_start",
            "file_reader_result",
            "deep_research_plan_start",
            "deep_research_plan_delta",
            "intermediate_report_start",
            "intermediate_report_delta",
            "research_agent_start",
        }:
            if in_thinking:
                yield stop_block()
                in_thinking = False
                block_idx += 1
            if not in_text:
                yield start_block("text")
                in_text = True

        text_delta = ""
        if etype == "message_delta":
            c = obj.get("content")
            if isinstance(c, str):
                text_delta = c
        elif etype == "search_tool_start":
            label = "Web Search" if obj.get("is_internet_search") is True else "Internal Search"
            text_delta = f"\n[{label}] "
        elif etype == "search_tool_queries_delta":
            qs = to_str_slice(obj.get("queries"))
            if qs:
                text_delta = "Searching: " + ", ".join([f"“{q}”" for q in qs]) + "\n"
        elif etype == "search_tool_documents_delta":
            docs = obj.get("documents")
            if isinstance(docs, list) and docs:
                text_delta = f"Found {len(docs)} results.\n"
        elif etype == "open_url_start":
            text_delta = "\n[Opening URL] "
        elif etype == "open_url_urls":
            urls = to_str_slice(obj.get("urls"))
            if urls:
                text_delta = ", ".join(urls) + "\n"
        elif etype == "open_url_documents":
            docs = obj.get("documents")
            if isinstance(docs, list) and docs:
                text_delta = f"Loaded {len(docs)} pages.\n"
        elif etype == "python_tool_start":
            code = obj.get("code") if isinstance(obj.get("code"), str) else ""
            text_delta = "\n[Code Interpreter]\n"
            if code:
                text_delta += f"```python\n{code}\n```\n"
        elif etype == "python_tool_delta":
            parts: list[str] = []
            stdout = obj.get("stdout")
            stderr = obj.get("stderr")
            if isinstance(stdout, str) and stdout:
                parts.append(f"Output: {stdout}")
            if isinstance(stderr, str) and stderr:
                parts.append(f"Error: {stderr}")
            if parts:
                text_delta = "\n".join(parts) + "\n"
        elif etype == "custom_tool_start":
            tool_name = obj.get("tool_name") if isinstance(obj.get("tool_name"), str) else "custom_tool"
            text_delta = f"\n[Tool: {tool_name}]\n"
        elif etype == "custom_tool_delta":
            data = obj.get("data")
            if data is not None:
                text_delta = (json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)) + "\n"
        elif etype == "image_generation_start":
            text_delta = "\n[Generating Image...]\n"
        elif etype == "image_generation_final":
            images = obj.get("images")
            if isinstance(images, list):
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    url = image.get("url")
                    prompt = image.get("revised_prompt")
                    if isinstance(url, str) and url:
                        p = prompt if isinstance(prompt, str) else "image"
                        text_delta += f"![{p}]({url})\n"
        elif etype == "file_reader_start":
            text_delta = "\n[Reading File...]\n"
        elif etype == "file_reader_result":
            fn = obj.get("file_name")
            if isinstance(fn, str) and fn:
                text_delta = f"Read: {fn}\n"
        elif etype in {"deep_research_plan_start", "intermediate_report_start"}:
            text_delta = "\n[Research Plan]\n"
        elif etype in {"deep_research_plan_delta", "intermediate_report_delta"}:
            c = obj.get("content")
            if isinstance(c, str):
                text_delta = c
        elif etype == "research_agent_start":
            task = obj.get("research_task")
            if isinstance(task, str):
                text_delta = f"\n[Researching: {task}]\n"
        elif etype == "error":
            msg = obj.get("error") if isinstance(obj.get("error"), str) else "Unknown error"
            text_delta = f"\n[Error: {msg}]"

        if text_delta:
            yield anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": block_idx,
                    "delta": {"type": "text_delta", "text": text_delta},
                },
            )

        if etype in {"stop", "error"}:
            break

    if in_thinking:
        yield stop_block()
        block_idx += 1
    if in_text:
        yield stop_block()

    yield anthropic_sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 0},
        },
    )
    yield anthropic_sse("message_stop", {"type": "message_stop"})


async def collect_anthropic(resp: httpx.Response) -> tuple[str, str]:
    text_parts: list[str] = []
    think_parts: list[str] = []
    tool_ctx: list[str] = []

    async for ev in iter_onyx_events(resp):
        etype = ev["type"]
        obj = ev["obj"]

        if ev["err"]:
            logger.error("Anthropic collect error (skipped): %s", ev["err"])
            continue

        if etype == "reasoning_delta":
            s = obj.get("reasoning")
            if isinstance(s, str) and s:
                think_parts.append(s)
        elif etype == "message_delta":
            c = obj.get("content")
            if isinstance(c, str) and c:
                text_parts.append(c)
        elif etype == "search_tool_start":
            label = "Web Search" if obj.get("is_internet_search") is True else "Internal Search"
            tool_ctx.append(f"[{label}]")
        elif etype == "search_tool_queries_delta":
            qs = to_str_slice(obj.get("queries"))
            if qs:
                tool_ctx.append("Searching: " + ", ".join(qs))
        elif etype == "search_tool_documents_delta":
            docs = obj.get("documents")
            if isinstance(docs, list) and docs:
                tool_ctx.append(f"Found {len(docs)} results.")
        elif etype == "open_url_start":
            tool_ctx.append("[Opening URL]")
        elif etype == "open_url_urls":
            urls = to_str_slice(obj.get("urls"))
            if urls:
                tool_ctx.append(", ".join(urls))
        elif etype == "python_tool_start":
            code = obj.get("code") if isinstance(obj.get("code"), str) else ""
            tool_ctx.append(f"[Code Interpreter]\n```python\n{code}\n```")
        elif etype == "python_tool_delta":
            stdout = obj.get("stdout")
            stderr = obj.get("stderr")
            if isinstance(stdout, str) and stdout:
                tool_ctx.append(f"Output: {stdout}")
            if isinstance(stderr, str) and stderr:
                tool_ctx.append(f"Error: {stderr}")
        elif etype == "image_generation_final":
            images = obj.get("images")
            if isinstance(images, list):
                for image in images:
                    if not isinstance(image, dict):
                        continue
                    url = image.get("url")
                    prompt = image.get("revised_prompt")
                    if isinstance(url, str) and url:
                        p = prompt if isinstance(prompt, str) else "image"
                        tool_ctx.append(f"![{p}]({url})")
        elif etype == "custom_tool_start":
            tool_name = obj.get("tool_name") if isinstance(obj.get("tool_name"), str) else "custom_tool"
            tool_ctx.append(f"[Tool: {tool_name}]")
        elif etype == "custom_tool_delta":
            data = obj.get("data")
            if data is not None:
                tool_ctx.append(str(data))
        elif etype == "error":
            msg = obj.get("error") if isinstance(obj.get("error"), str) else "Unknown error"
            text_parts.append(f"\n[Error: {msg}]")
        elif etype == "stop":
            break

    text_builder: list[str] = []
    if tool_ctx:
        text_builder.append("\n".join(tool_ctx))
        text_builder.append("")
    text_builder.append("".join(text_parts))
    return "\n".join(text_builder), "".join(think_parts)


@app.get("/")
async def handle_root() -> dict[str, Any]:
    return {
        "name": "onyx2api-py",
        "version": VERSION,
        "endpoints": [
            "/v1/chat/completions",
            "/v1/messages",
            "/v1/models",
            "/health",
            "/api/config",
            "/ui",
        ],
    }


@app.get("/health")
async def handle_health() -> dict[str, Any]:
    cfg = await run_in_threadpool(store.get)
    return {
        "status": "ok",
        "version": VERSION,
        "cookies": len(cfg.onyx_cookies),
        "client_keys": len(cfg.client_api_keys),
        "onyx_base": cfg.onyx_base,
    }


@app.get("/v1/models")
async def handle_models() -> dict[str, Any]:
    models = list(MODEL_MAP.keys())
    data = [{"id": m, "object": "model", "created": 1700000000, "owned_by": "onyx"} for m in models]
    return {"object": "list", "data": data}


@app.get("/ui", response_class=HTMLResponse)
async def ui_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "index.html", {"version": VERSION})


@app.get("/api/config")
async def get_config(request: Request) -> dict[str, Any]:
    cfg = await run_in_threadpool(store.get)
    if not check_admin_auth(cfg, request):
        raise HTTPException(status_code=401, detail="invalid admin password")
    data = cfg.model_dump()
    data.pop("admin_password", None)
    return {"config": data}


@app.post("/api/config")
async def save_config(payload: ConfigUpdate, request: Request) -> dict[str, Any]:
    current = await run_in_threadpool(store.get)
    if not check_admin_auth(current, request):
        raise HTTPException(status_code=401, detail="invalid admin password")

    next_admin = payload.admin_password.strip() if payload.admin_password is not None else current.admin_password
    merged = AppConfig(
        onyx_base=payload.onyx_base,
        onyx_cookies=payload.onyx_cookies,
        client_api_keys=payload.client_api_keys,
        default_persona=payload.default_persona,
        default_model=payload.default_model,
        request_timeout_seconds=payload.request_timeout_seconds,
        admin_password=next_admin,
    )

    try:
        saved = await run_in_threadpool(store.set, merged)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    data = saved.model_dump()
    data.pop("admin_password", None)
    return {"ok": True, "config": data}


@app.post("/api/onyx-cookies/append")
@app.post("/api/onyx-keys/append")
async def append_onyx_cookie(payload: AppendOnyxCookieReq, request: Request) -> dict[str, Any]:
    cfg = await run_in_threadpool(store.get)
    if not check_admin_auth(cfg, request):
        raise HTTPException(status_code=401, detail="invalid admin password")

    try:
        saved, inserted = await run_in_threadpool(store.append_onyx_cookie, payload.cookie)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "inserted": inserted,
        "total": len(saved.onyx_cookies),
    }


class VerifyCookieReq(BaseModel):
    cookie: str = Field(default="", validation_alias=AliasChoices("cookie", "key"), serialization_alias="cookie")
    model: str = ""


class RemoveCookiesReq(BaseModel):
    cookies: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("cookies", "keys"),
        serialization_alias="cookies",
    )


@app.post("/api/onyx-cookies/verify")
@app.post("/api/onyx-keys/verify")
async def verify_onyx_cookie(payload: VerifyCookieReq, request: Request) -> dict[str, Any]:
    """验证单个 Onyx Cookie 是否可用"""
    cfg = await run_in_threadpool(store.get)
    if not check_admin_auth(cfg, request):
        raise HTTPException(status_code=401, detail="invalid admin password")

    cookie = payload.cookie.strip()
    if not cookie:
        raise HTTPException(status_code=400, detail="cookie is required")

    model = payload.model.strip() or cfg.default_model

    if http is None:
        raise HTTPException(status_code=500, detail="HTTP client not initialized")

    try:
        chat_session_id, effective_cookie = await create_chat_session(cfg, cookie, cfg.default_persona)
        body = {
            "message": "Hi",
            "chat_session_id": chat_session_id,
            "parent_message_id": None,
            "file_descriptors": [],
            "internal_search_filters": {
                "source_type": None,
                "document_set": None,
                "time_cutoff": None,
                "tags": [],
            },
            "deep_research": False,
            "forced_tool_id": None,
            "llm_override": build_llm_override(model, 0.5),
            "origin": ONYX_ORIGIN,
        }

        req = http.build_request(
            "POST",
            f"{cfg.onyx_base.rstrip('/')}/api/chat/send-chat-message",
            json=body,
            headers=build_onyx_headers(cfg, with_json=True),
            cookies=_build_onyx_request_cookies(effective_cookie),
            timeout=httpx.Timeout(30.0, connect=10.0),
        )
        resp = await http.send(req, stream=True)

        if resp.status_code != 200:
            body_bytes = await resp.aread()
            body_text = body_bytes.decode("utf-8", errors="ignore")[:512]
            await resp.aclose()
            return {
                "cookie": cookie,
                "alive": False,
                "status": resp.status_code,
                "error": body_text,
            }

        # 读取前几个事件，检查是否有错误
        found_error = None
        line_count = 0

        async for line in resp.aiter_lines():
            text = line.strip()
            if not text:
                continue
            line_count += 1
            try:
                raw = json.loads(text)
            except json.JSONDecodeError:
                continue

            if raw.get("error") is not None and raw.get("obj") is None:
                found_error = str(raw.get("error"))
                break

            obj = raw.get("obj")
            if isinstance(obj, dict):
                etype = str(obj.get("type", ""))
                if etype == "error":
                    found_error = str(obj.get("error", "Unknown error"))
                    break
                if etype == "stop":
                    break

            if line_count >= 20:
                break

        await resp.aclose()

        if found_error:
            return {
                "cookie": cookie,
                "alive": False,
                "status": 200,
                "error": found_error,
            }

        return {
            "cookie": cookie,
            "alive": True,
            "status": 200,
            "error": "",
        }

    except httpx.TimeoutException:
        return {
            "cookie": cookie,
            "alive": False,
            "status": 0,
            "error": "Request timeout",
        }
    except Exception as exc:
        return {
            "cookie": cookie,
            "alive": False,
            "status": 0,
            "error": str(exc),
        }


@app.post("/api/onyx-cookies/remove")
@app.post("/api/onyx-keys/remove")
async def remove_onyx_cookies(payload: RemoveCookiesReq, request: Request) -> dict[str, Any]:
    """批量删除指定的 Onyx Cookies"""
    cfg = await run_in_threadpool(store.get)
    if not check_admin_auth(cfg, request):
        raise HTTPException(status_code=401, detail="invalid admin password")

    to_remove = set(k.strip() for k in payload.cookies if k.strip())
    if not to_remove:
        raise HTTPException(status_code=400, detail="cookies list is empty")

    new_cookies = [k for k in cfg.onyx_cookies if k not in to_remove]
    removed_count = len(cfg.onyx_cookies) - len(new_cookies)

    cfg.onyx_cookies = new_cookies
    try:
        saved = await run_in_threadpool(store.set, cfg)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "removed": removed_count,
        "remaining": len(saved.onyx_cookies),
    }


@app.get("/api/onyx-cookies/failed")
@app.get("/api/onyx-keys/failed")
async def list_failed_onyx_cookies(request: Request) -> dict[str, Any]:
    """返回出现过失败记录的 Onyx Cookies"""
    cfg = await run_in_threadpool(store.get)
    if not check_admin_auth(cfg, request):
        raise HTTPException(status_code=401, detail="invalid admin password")

    items = get_failed_cookie_items(cfg)
    return {
        "ok": True,
        "items": items,
        "total": len(items),
    }


@app.post("/api/onyx-cookies/remove-failed")
@app.post("/api/onyx-keys/remove-failed")
async def remove_failed_onyx_cookies(request: Request) -> dict[str, Any]:
    """一键删除失败过的 Onyx Cookies"""
    global _cookie_index

    cfg = await run_in_threadpool(store.get)
    if not check_admin_auth(cfg, request):
        raise HTTPException(status_code=401, detail="invalid admin password")

    failed_items = get_failed_cookie_items(cfg)
    failed_ids = {_cookie_error_identifier(item.get("cookie", "")) for item in failed_items}
    failed_ids = {item for item in failed_ids if item}

    if not failed_ids:
        return {
            "ok": True,
            "removed": 0,
            "remaining": len(cfg.onyx_cookies),
        }

    kept_cookies: list[str] = []
    removed_count = 0
    removed_ids: set[str] = set()

    for cookie in cfg.onyx_cookies:
        cookie_id = _cookie_error_identifier(cookie)
        if cookie_id and cookie_id in failed_ids:
            removed_count += 1
            removed_ids.add(cookie_id)
            continue
        kept_cookies.append(cookie)

    if removed_count <= 0:
        return {
            "ok": True,
            "removed": 0,
            "remaining": len(cfg.onyx_cookies),
        }

    cfg.onyx_cookies = kept_cookies
    try:
        saved = await run_in_threadpool(store.set, cfg)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with _cookie_lock:
        if saved.onyx_cookies:
            _cookie_index %= len(saved.onyx_cookies)
        else:
            _cookie_index = 0

    with _cookie_error_lock:
        for cookie_id in removed_ids:
            _cookie_error_counts.pop(cookie_id, None)

    return {
        "ok": True,
        "removed": removed_count,
        "remaining": len(saved.onyx_cookies),
    }


@app.post("/v1/chat/completions")
async def handle_openai(req: OpenAIReq, request: Request) -> Any:
    cfg = await run_in_threadpool(store.get)
    if not check_client_auth(cfg, request):
        return JSONResponse(status_code=401, content={"error": "invalid api key"})

    token = resolve_auth_cookie(cfg, request.headers.get("authorization"))
    if not token:
        return JSONResponse(status_code=401, content={"error": "No auth. Configure onyx_cookies in local config."})

    if not req.messages:
        return JSONResponse(status_code=400, content={"error": "messages is required"})

    model = req.model.strip() or cfg.default_model
    temp = req.temperature if req.temperature is not None else 0.5
    persona = req.persona_id if req.persona_id is not None else cfg.default_persona
    rid = gen_id("chatcmpl-")

    if req.stream:
        async def gen() -> AsyncIterator[bytes]:
            async for resp, events in safe_iter_onyx_events(
                cfg,
                token,
                model,
                req.messages,
                "",
                temp,
                persona,
            ):
                try:
                    async for chunk in stream_openai(events, model, rid):
                        yield chunk
                    return
                except OnyxUpstreamRejectedError:
                    # safe_iter_onyx_events 会负责计数并换 cookie 重试
                    continue
                finally:
                    await resp.aclose()

            raise RuntimeError("All stream retry attempts failed")

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        async for resp, events in safe_iter_onyx_events(
            cfg,
            token,
            model,
            req.messages,
            "",
            temp,
            persona,
        ):
            try:
                content = await collect_openai(events)
                break
            except OnyxUpstreamRejectedError:
                # safe_iter_onyx_events 会负责计数并换 cookie 重试
                continue
            finally:
                await resp.aclose()
        else:
            raise RuntimeError("All stream retry attempts failed")
    except OnyxHTTPError as exc:
        return JSONResponse(status_code=exc.status, content={"error": str(exc), "detail": exc.body})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})

    return {
        "id": rid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/messages")
async def handle_anthropic(
    req: AnthropicReq,
    request: Request,
    x_api_key: str | None = Header(default=None),
) -> Any:
    cfg = await run_in_threadpool(store.get)
    if not check_client_auth(cfg, request):
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid api key"},
            },
        )

    token = resolve_auth_cookie(cfg, x_api_key, request.headers.get("authorization"))
    if not token:
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "error": {"type": "authentication_error", "message": "No auth. Configure onyx_cookies in local config."},
            },
        )

    if not req.messages:
        return JSONResponse(
            status_code=400,
            content={
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "messages is required"},
            },
        )

    model = req.model.strip() or cfg.default_model
    temp = req.temperature if req.temperature is not None else 0.5
    persona = req.persona_id if req.persona_id is not None else cfg.default_persona
    system = text_content(req.system)
    rid = gen_id("msg_")

    if req.stream:
        async def gen() -> AsyncIterator[bytes]:
            async for resp, events in safe_iter_onyx_events(
                cfg,
                token,
                model,
                req.messages,
                system,
                temp,
                persona,
            ):
                try:
                    async for chunk in stream_anthropic(events, model, rid):
                        yield chunk
                    return
                except OnyxUpstreamRejectedError:
                    # safe_iter_onyx_events 会负责计数并换 cookie 重试
                    continue
                finally:
                    await resp.aclose()

            raise RuntimeError("All stream retry attempts failed")

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    try:
        async for resp, events in safe_iter_onyx_events(
            cfg,
            token,
            model,
            req.messages,
            system,
            temp,
            persona,
        ):
            try:
                text, thinking = await collect_anthropic(events)
                break
            except OnyxUpstreamRejectedError:
                # safe_iter_onyx_events 会负责计数并换 cookie 重试
                continue
            finally:
                await resp.aclose()
        else:
            raise RuntimeError("All stream retry attempts failed")
    except OnyxHTTPError as exc:
        return JSONResponse(
            status_code=exc.status,
            content={"type": "error", "error": {"type": "api_error", "message": str(exc)}},
        )
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            status_code=502,
            content={"type": "error", "error": {"type": "api_error", "message": str(exc)}},
        )

    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": thinking})
    content.append({"type": "text", "text": text})

    return {
        "id": rid,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


if __name__ == "__main__":
    import uvicorn

    c = store.get()
    logger.info(
        "onyx2api v%s | onyx_cookies=%s | client_keys=%s | admin_password=%s",
        VERSION,
        len(c.onyx_cookies),
        len(c.client_api_keys),
        c.admin_password,
    )
    uvicorn.run("app:app", host="0.0.0.0", port=19898, reload=False)
