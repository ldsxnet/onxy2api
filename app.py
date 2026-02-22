from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

VERSION = "0.5.0-py"
ONYX_BASE_DEFAULT = "https://cloud.onyx.app"
MAX_RETRIES = 3
RETRY_BACKOFF = [2, 5, 10]
RETRY_STATUS = {502, 503, 504, 429, 401, 403}

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("onyx2api")


@dataclass
class ProviderModel:
    provider: str
    version: str


MODEL_MAP: dict[str, ProviderModel] = {
    "claude-opus-4-6": ProviderModel("Anthropic", "claude-opus-4-6"),
    "claude-opus-4-5": ProviderModel("Anthropic", "claude-opus-4-5"),
    "claude-sonnet-4-6": ProviderModel("Anthropic", "claude-sonnet-4-6"),
    "claude-sonnet-4-5": ProviderModel("Anthropic", "claude-sonnet-4-5"),
    "claude-sonnet-4": ProviderModel("Anthropic", "claude-sonnet-4-20250514"),
    "claude-3-5-sonnet": ProviderModel("Anthropic", "claude-3-5-sonnet-20241022"),
    "claude-3-5-haiku": ProviderModel("Anthropic", "claude-3-5-haiku-20241022"),
    "claude-3-opus": ProviderModel("Anthropic", "claude-3-opus-20240229"),
    "claude-haiku-4-5": ProviderModel("Anthropic", "claude-haiku-4-5"),
    "gpt-4o": ProviderModel("OpenAI", "gpt-4o"),
    "gpt-4o-mini": ProviderModel("OpenAI", "gpt-4o-mini"),
    "gpt-4-turbo": ProviderModel("OpenAI", "gpt-4-turbo"),
    "gpt-4": ProviderModel("OpenAI", "gpt-4"),
    "o1": ProviderModel("OpenAI", "o1"),
    "o1-mini": ProviderModel("OpenAI", "o1-mini"),
    "o3-mini": ProviderModel("OpenAI", "o3-mini"),
    "gemini-2.0-flash": ProviderModel("Google", "gemini-2.0-flash"),
    "gemini-2.5-pro": ProviderModel("Google", "gemini-2.5-pro-preview-05-06"),
}


class AppConfig(BaseModel):
    onyx_base: str = ONYX_BASE_DEFAULT
    onyx_keys: list[str] = Field(default_factory=list)
    client_api_keys: list[str] = Field(default_factory=list)
    default_persona: int = 1
    default_model: str = "claude-opus-4-6"
    request_timeout_seconds: int = 300
    admin_password: str = ""


class ConfigUpdate(BaseModel):
    onyx_base: str = ONYX_BASE_DEFAULT
    onyx_keys: list[str] = Field(default_factory=list)
    client_api_keys: list[str] = Field(default_factory=list)
    default_persona: int = 1
    default_model: str = "claude-opus-4-6"
    request_timeout_seconds: int = 300
    admin_password: str | None = None


class AppendOnyxKeyReq(BaseModel):
    key: str = ""


def generate_admin_password() -> str:
    return f"adm-{secrets.token_urlsafe(12)}"


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._cfg = AppConfig()

    @staticmethod
    def _norm_keys(values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for item in values:
            v = item.strip()
            if not v or v in seen:
                continue
            seen.add(v)
            out.append(v)
        return out

    def _normalize(self, cfg: AppConfig) -> AppConfig:
        cfg.onyx_keys = self._norm_keys(cfg.onyx_keys)
        cfg.client_api_keys = self._norm_keys(cfg.client_api_keys)
        if cfg.client_api_keys and not cfg.onyx_keys:
            raise ValueError("client_api_keys 已配置时，onyx_keys 不能为空")
        if cfg.default_persona <= 0:
            cfg.default_persona = 1
        if cfg.request_timeout_seconds < 30:
            cfg.request_timeout_seconds = 30
        if not cfg.default_model.strip():
            cfg.default_model = "claude-opus-4-6"
        if not cfg.onyx_base.strip():
            cfg.onyx_base = ONYX_BASE_DEFAULT
        if not cfg.admin_password.strip():
            cfg.admin_password = generate_admin_password()
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

    def append_onyx_key(self, key: str) -> tuple[AppConfig, bool]:
        with self._lock:
            v = key.strip()
            if not v:
                raise ValueError("key 不能为空")
            exists = v in self._cfg.onyx_keys
            if not exists:
                self._cfg.onyx_keys.append(v)
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
store = ConfigStore(BASE_DIR / "config.json")
store.load()

app = FastAPI(title="onyx2api-py", version=VERSION)


@app.middleware("http")
async def add_cors_headers(request: Request, call_next):
    if request.method == "OPTIONS":
        response = JSONResponse(status_code=204, content={})
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

_key_lock = threading.Lock()
_key_index = 0


class OnyxHTTPError(Exception):
    def __init__(self, status: int, body: str):
        super().__init__(f"Onyx HTTP {status}: {body}")
        self.status = status
        self.body = body


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


def next_key(keys: list[str]) -> str:
    global _key_index
    if not keys:
        return ""
    with _key_lock:
        idx = _key_index % len(keys)
        _key_index += 1
    return keys[idx]


def resolve_auth(cfg: AppConfig, *headers: str | None) -> str:
    if cfg.onyx_keys:
        return next_key(cfg.onyx_keys)
    for h in headers:
        token = extract_token(h)
        if token:
            return token
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


async def do_onyx_request(
    cfg: AppConfig,
    token: str,
    model: str,
    msgs: list[ChatMsg],
    system: str,
    temp: float,
    persona: int,
) -> httpx.Response:
    if http is None:
        raise RuntimeError("HTTP client is not initialized")

    body = {
        "message": messages_to_onyx(system, msgs),
        "chat_session_info": {"persona_id": persona},
        "llm_override": build_llm_override(model, temp),
        "stream": True,
        "file_descriptors": [],
        "deep_research": False,
        "origin": "api",
    }

    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        cur = token
        if attempt > 0 and cfg.onyx_keys:
            cur = next_key(cfg.onyx_keys)
            logger.warning("Retry %s/%s, switched key", attempt, MAX_RETRIES)

        try:
            req = http.build_request(
                "POST",
                f"{cfg.onyx_base.rstrip('/')}/api/chat/send-chat-message",
                json=body,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {cur}",
                },
                timeout=httpx.Timeout(float(cfg.request_timeout_seconds), connect=30.0),
            )
            resp = await http.send(req, stream=True)
        except httpx.HTTPError as exc:
            last_err = exc
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning("Attempt %s failed (%s), retry in %ss", attempt + 1, exc, wait)
                await asyncio.sleep(wait)
                continue
            break

        if resp.status_code != 200:
            body_bytes = await resp.aread()
            body_text = body_bytes.decode("utf-8", errors="ignore")[:1024]
            status = resp.status_code
            await resp.aclose()
            logger.warning("Onyx HTTP %s for model=%s: %s", status, model, body_text)
            if "Error: An unexpected error occurred while processing your request. Please try again later." in body_text:
                status = 503
                logger.error("Onyx HTTP %s for model=%s: %s", status, model, body_text)

            if status in RETRY_STATUS and attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                logger.warning("Attempt %s failed (HTTP %s), retry in %ss", attempt + 1, status, wait)
                await asyncio.sleep(wait)
                continue
            raise OnyxHTTPError(status, body_text)

        return resp

    raise RuntimeError(f"all retries failed: {last_err}")


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


async def stream_openai(resp: httpx.Response, model: str, rid: str) -> AsyncIterator[bytes]:
    created = int(time.time())
    sent_role = False
    tool_active = False

    async for ev in iter_onyx_events(resp):
        etype = ev["type"]
        obj = ev["obj"]

        if ev["err"]:
            yield sse(make_chunk(rid, created, model, {"content": f"\n\n[Error: {ev['err']}]"}, "stop"))
            yield b"data: [DONE]\n\n"
            return

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
            yield sse(make_chunk(rid, created, model, {"content": f"\n[Error: {msg}]"}, "stop"))
            yield b"data: [DONE]\n\n"
            return

        if etype == "stop":
            yield sse(make_chunk(rid, created, model, {}, "stop"))
            yield b"data: [DONE]\n\n"
            return

    yield b"data: [DONE]\n\n"


async def collect_openai(resp: httpx.Response) -> str:
    parts: list[str] = []
    tool_ctx: list[str] = []

    async for ev in iter_onyx_events(resp):
        etype = ev["type"]
        obj = ev["obj"]
        if ev["err"]:
            parts.append(f"\n[Error: {ev['err']}]")
            break

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
            parts.append(f"\n[Error: {msg}]")
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


async def stream_anthropic(resp: httpx.Response, model: str, rid: str) -> AsyncIterator[bytes]:
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

    async for ev in iter_onyx_events(resp):
        etype = ev["type"]
        obj = ev["obj"]

        if ev["err"]:
            if not in_text:
                yield start_block("text")
                in_text = True
            yield anthropic_sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": block_idx,
                    "delta": {"type": "text_delta", "text": f"\n[Error: {ev['err']}]"},
                },
            )
            break

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
            text_parts.append(f"\n[Error: {ev['err']}]")
            break

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
        "keys": len(cfg.onyx_keys),
        "client_keys": len(cfg.client_api_keys),
        "onyx_base": cfg.onyx_base,
    }


@app.get("/v1/models")
async def handle_models() -> dict[str, Any]:
    models = [
        "claude-opus-4-6",
        "claude-opus-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-5",
        "claude-sonnet-4",
        "claude-3-5-sonnet",
        "claude-3-5-haiku",
        "claude-haiku-4-5",
        "gpt-4o",
        "gpt-4o-mini",
        "o1",
        "o3-mini",
        "gemini-2.0-flash",
        "gemini-2.5-pro",
    ]
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
        onyx_keys=payload.onyx_keys,
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


@app.post("/api/onyx-keys/append")
async def append_onyx_key(payload: AppendOnyxKeyReq, request: Request) -> dict[str, Any]:
    cfg = await run_in_threadpool(store.get)
    if not check_admin_auth(cfg, request):
        raise HTTPException(status_code=401, detail="invalid admin password")

    try:
        saved, inserted = await run_in_threadpool(store.append_onyx_key, payload.key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "ok": True,
        "inserted": inserted,
        "total": len(saved.onyx_keys),
    }


@app.post("/v1/chat/completions")
async def handle_openai(req: OpenAIReq, request: Request) -> Any:
    cfg = await run_in_threadpool(store.get)
    if not check_client_auth(cfg, request):
        return JSONResponse(status_code=401, content={"error": "invalid api key"})

    token = resolve_auth(cfg, request.headers.get("authorization"))
    if not token:
        return JSONResponse(status_code=401, content={"error": "No auth. Configure onyx_keys in local config."})

    if not req.messages:
        return JSONResponse(status_code=400, content={"error": "messages is required"})

    model = req.model.strip() or cfg.default_model
    temp = req.temperature if req.temperature is not None else 0.5
    persona = req.persona_id if req.persona_id is not None else cfg.default_persona
    rid = gen_id("chatcmpl-")

    try:
        resp = await do_onyx_request(cfg, token, model, req.messages, "", temp, persona)
    except OnyxHTTPError as exc:
        return JSONResponse(status_code=exc.status, content={"error": str(exc), "detail": exc.body})
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})

    if req.stream:
        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in stream_openai(resp, model, rid):
                    yield chunk
            finally:
                await resp.aclose()

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
        content = await collect_openai(resp)
    finally:
        await resp.aclose()

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

    token = resolve_auth(cfg, x_api_key, request.headers.get("authorization"))
    if not token:
        return JSONResponse(
            status_code=401,
            content={
                "type": "error",
                "error": {"type": "authentication_error", "message": "No auth. Configure onyx_keys in local config."},
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

    try:
        resp = await do_onyx_request(cfg, token, model, req.messages, system, temp, persona)
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

    if req.stream:
        async def gen() -> AsyncIterator[bytes]:
            try:
                async for chunk in stream_anthropic(resp, model, rid):
                    yield chunk
            finally:
                await resp.aclose()

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
        text, thinking = await collect_anthropic(resp)
    finally:
        await resp.aclose()

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
        "onyx2api v%s | onyx_keys=%s | client_keys=%s | admin_password=%s",
        VERSION,
        len(c.onyx_keys),
        len(c.client_api_keys),
        c.admin_password,
    )
    uvicorn.run("app:app", host="0.0.0.0", port=19898, reload=False)
