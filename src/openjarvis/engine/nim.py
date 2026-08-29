"""NVIDIA NIM inference engine — self-hosted inference microservices."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from typing import Any, Dict, List

import httpx

from openjarvis.core.registry import EngineRegistry
from openjarvis.core.types import Message
from openjarvis.engine._base import (
    EngineConnectionError,
    InferenceEngine,
    estimate_prompt_tokens,
    messages_to_dicts,
)
from openjarvis.engine._stubs import StreamChunk
from openjarvis.security.nim_rate_limit import StrictWindowLimiter

logger = logging.getLogger(__name__)


@EngineRegistry.register("nim")
class NIMEngine(InferenceEngine):
    """NVIDIA NIM inference engine.

    Supports local and remote NIM deployments with OpenAI-compatible API.
    Requires NIM_API_KEY for NVIDIA-hosted NIM endpoints, optional for self-hosted.
    """

    engine_id = "nim"
    _default_host = "https://integrate.api.nvidia.com"
    _api_prefix = "/v1"

    def __init__(self, host: str | None = None, *, timeout: float = 600.0) -> None:
        env_host = os.environ.get("NIM_HOST")
        self._host = (host or env_host or self._default_host).rstrip("/")

        api_key = os.environ.get("NIM_API_KEY", "")
        self._api_key = api_key if api_key else None

        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        self._timeout = timeout
        try:
            rpm_limit = int(os.environ.get("NIM_RPM_LIMIT", "40"))
        except ValueError:
            rpm_limit = 40
        try:
            queue_timeout = float(os.environ.get("NIM_RPM_QUEUE_TIMEOUT", "600"))
        except ValueError:
            queue_timeout = 600.0
        self._rpm_queue_timeout = max(0.1, queue_timeout)
        # Settings validation is not a sufficient boundary because deployment
        # environment values can bypass it. NVIDIA NIM traffic must never use
        # more than 40 requests/minute for this server process.
        self._request_limiter = StrictWindowLimiter(limit=max(1, min(rpm_limit, 40)))
        self._client = httpx.Client(
            base_url=self._host,
            timeout=timeout,
            headers=headers,
        )

    def _acquire_sync(self) -> None:
        self._request_limiter.acquire(timeout=self._rpm_queue_timeout)

    async def _acquire_async(self) -> None:
        await self._request_limiter.acquire_async(timeout=self._rpm_queue_timeout)

    def rate_limit_snapshot(self) -> dict[str, float | int]:
        snapshot = self._request_limiter.snapshot()
        return {
            "limit": snapshot.limit,
            "window_seconds": snapshot.window_seconds,
            "used": snapshot.used,
            "remaining": snapshot.remaining,
            "reset_after_seconds": snapshot.reset_after_seconds,
        }

    def _get_headers(self) -> Dict[str, str]:
        """Get headers for requests, including auth if configured."""
        headers = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def generate(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages_to_dicts(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
            **kwargs,
        }
        if "tools" in payload and "tool_choice" not in payload:
            payload["tool_choice"] = "auto"
        try:
            self._acquire_sync()
            url = f"{self._api_prefix}/chat/completions"
            resp = self._client.post(url, json=payload, headers=self._get_headers())
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise EngineConnectionError(
                f"NIM engine not reachable at {self._host}"
            ) from exc
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return {
                "content": "",
                "usage": data.get("usage", {}),
                "model": data.get("model", model),
                "finish_reason": "error",
            }
        choice = choices[0]
        usage = data.get("usage", {})
        reported_prompt = usage.get("prompt_tokens", 0)
        estimated_prompt = estimate_prompt_tokens(messages)
        prompt_tokens = max(reported_prompt, estimated_prompt)
        completion_tokens = usage.get("completion_tokens", 0)
        result: Dict[str, Any] = {
            "content": choice["message"].get("content") or "",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "prompt_tokens_evaluated": reported_prompt or prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
            "model": data.get("model", model),
            "finish_reason": choice.get("finish_reason", "stop"),
        }
        raw_tool_calls = choice["message"].get("tool_calls", [])
        if raw_tool_calls:
            result["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "name": tc.get("function", {}).get("name", ""),
                    "arguments": tc.get("function", {}).get("arguments", "{}"),
                }
                for tc in raw_tool_calls
            ]
        return result

    def generate_vision(
        self,
        *,
        model: str,
        image_data_url: str,
        question: str,
        max_tokens: int = 400,
    ) -> Dict[str, Any]:
        """Run a single OpenAI-compatible multimodal NIM request.

        This intentionally shares the engine's strict limiter with normal
        chat calls.  It accepts an in-memory data URL only; callers remain
        responsible for validating and bounding the image before this call.
        """
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Describe only what is visible in the supplied image. "
                        "Separate direct observations from uncertainty. Do not "
                        "identify people, infer sensitive traits, or claim facts "
                        "that are not visible."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": question},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                },
            ],
            "temperature": 0.2,
            "max_tokens": max(1, min(int(max_tokens), 800)),
            "stream": False,
        }
        try:
            self._acquire_sync()
            response = self._client.post(
                f"{self._api_prefix}/chat/completions",
                json=payload,
                headers=self._get_headers(),
            )
            response.raise_for_status()
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise EngineConnectionError(f"NIM engine not reachable at {self._host}") from exc
        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            return {"content": "", "model": data.get("model", model), "finish_reason": "error"}
        choice = choices[0]
        return {
            "content": choice.get("message", {}).get("content") or "",
            "model": data.get("model", model),
            "finish_reason": choice.get("finish_reason", "stop"),
            "usage": data.get("usage", {}),
        }

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator[str]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages_to_dicts(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **kwargs,
        }
        if "tools" in payload and "tool_choice" not in payload:
            payload["tool_choice"] = "auto"
        try:
            await self._acquire_async()
            url = f"{self._api_prefix}/chat/completions"
            async with httpx.AsyncClient(
                base_url=self._host,
                timeout=self._timeout,
                headers=self._get_headers(),
            ) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:") :].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        delta = choices[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            yield content
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise EngineConnectionError(
                f"NIM engine not reachable at {self._host}"
            ) from exc

    async def stream_full(
        self,
        messages: Sequence[Message],
        *,
        model: str,
        temperature: float = 0.7,
        max_tokens: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator["StreamChunk"]:
        """Yield StreamChunks with content, tool_calls, and finish_reason."""
        msg_dicts = messages_to_dicts(messages)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": msg_dicts,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
            **kwargs,
        }
        if "tools" in payload and "tool_choice" not in payload:
            payload["tool_choice"] = "auto"
        try:
            await self._acquire_async()
            url = f"{self._api_prefix}/chat/completions"
            async with httpx.AsyncClient(
                base_url=self._host,
                timeout=self._timeout,
                headers=self._get_headers(),
            ) as client:
                async with client.stream("POST", url, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data_str = line[len("data:") :].strip()
                        if data_str == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data_str)
                        except json.JSONDecodeError:
                            continue
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                        choice = choices[0]
                        delta = choice.get("delta", {})
                        finish = choice.get("finish_reason")
                        content = delta.get("content")
                        tool_calls = delta.get("tool_calls")
                        usage = chunk.get("usage")

                        if content or tool_calls or finish or usage:
                            yield StreamChunk(
                                content=content,
                                tool_calls=tool_calls,
                                finish_reason=finish,
                                usage=usage,
                            )
        except (httpx.ConnectError, httpx.TimeoutException) as exc:
            raise EngineConnectionError(
                f"NIM engine not reachable at {self._host}"
            ) from exc

    def list_models(self) -> List[str]:
        try:
            self._acquire_sync()
            resp = self._client.get(
                f"{self._api_prefix}/models",
                headers=self._get_headers(),
            )
            resp.raise_for_status()
        except (
            httpx.ConnectError,
            httpx.TimeoutException,
            httpx.HTTPStatusError,
        ) as exc:
            logger.warning("Failed to list models from NIM at %s: %s", self._host, exc)
            return []
        data = resp.json()
        return [m["id"] for m in data.get("data", [])]

    def health(self) -> bool:
        try:
            self._acquire_sync()
            resp = self._client.get(
                f"{self._api_prefix}/health/ready",
                timeout=2.0,
                headers=self._get_headers(),
            )
            if resp.status_code == 200:
                return True
        except Exception:
            pass
        try:
            self._acquire_sync()
            resp = self._client.get(
                f"{self._api_prefix}/models",
                timeout=2.0,
                headers=self._get_headers(),
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.debug("NIM health check failed at %s: %s", self._host, exc)
            return False

    def close(self) -> None:
        self._client.close()

    def __repr__(self) -> str:
        return f"NIMEngine(host={self._host})"
