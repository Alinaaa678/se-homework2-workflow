"""OpenAI-compatible chat client with retries and provider-neutral telemetry."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional


class LLMError(Exception):
    """Raised when the LLM call ultimately fails after all retries."""


EventListener = Callable[[Dict[str, Any]], None]


class ChatClient:
    """Thin wrapper around an OpenAI-compatible /chat/completions endpoint."""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        max_retries: int = 4,
        timeout: int = 120,
        on_event: Optional[EventListener] = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model or os.environ.get("OPENAI_MODEL", "deepseek-chat")
        self.max_retries = max_retries
        self.timeout = timeout
        self.on_event = on_event
        if not self.base_url:
            raise LLMError("未配置 LLM 服务地址：请设置 OPENAI_BASE_URL 或在 config.json 中填写。")
        if not self.api_key:
            raise LLMError("未配置 API Key：请设置 OPENAI_API_KEY 或在 config.json 中填写。")

    def _emit(self, event: Dict[str, Any]) -> None:
        if self.on_event:
            self.on_event(event)

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: float = 0.2,
    ) -> Dict[str, Any]:
        """Send one request; retry transient failures with exponential backoff."""
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        last_err: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            wait = float(2 ** attempt)
            self._emit({"type": "llm_attempt", "attempt": attempt + 1, "model": self.model})
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                self._emit({
                    "type": "llm_response",
                    "model": self.model,
                    "usage": result.get("usage") or {},
                })
                return result
            except urllib.error.HTTPError as exc:
                transient = exc.code == 429 or 500 <= exc.code < 600
                last_err = LLMError(
                    f"HTTP {exc.code}: {exc.read().decode('utf-8', 'ignore')[:300]}"
                )
                if not transient or attempt == self.max_retries:
                    raise last_err
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after:
                    try:
                        wait = min(float(retry_after), 30.0)
                    except ValueError:
                        pass
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_err = LLMError(f"网络错误: {exc}")
                if attempt == self.max_retries:
                    raise last_err
            except json.JSONDecodeError as exc:
                raise LLMError(f"LLM 返回了无法解析的响应: {exc}")
            self._emit({"type": "llm_retry", "attempt": attempt + 1, "wait_s": wait})
            time.sleep(wait)

        raise LLMError(f"LLM 请求失败（已重试 {self.max_retries} 次）: {last_err}")


def load_client(
    config_path: str = "config.json", on_event: Optional[EventListener] = None
) -> ChatClient:
    """Build a client from config.json, falling back to environment variables."""
    cfg: Dict[str, Any] = {}
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            cfg = json.load(handle)
    return ChatClient(
        base_url=cfg.get("base_url"),
        api_key=cfg.get("api_key"),
        model=cfg.get("model"),
        on_event=on_event,
    )
