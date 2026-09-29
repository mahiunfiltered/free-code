"""``ModelClient`` backed by the local FCC proxy (so helper calls use the key pool).

Intent extraction, the planner and the outcome judge call ``complete(system, user)``;
every failure raises, and those callers already degrade to deterministic fallbacks.
"""

from collections.abc import Callable

import httpx

from free_claude_code.core.json_types import JsonObject


class ProxyModelClient:
    """Non-streaming ``POST {proxy_root}/v1/messages`` with the proxy auth token."""

    def __init__(
        self,
        proxy_target: Callable[[], tuple[str, str]],
        model: str,
        *,
        timeout_s: float = 60.0,
        max_tokens: int = 4096,
    ) -> None:
        self._proxy_target = proxy_target
        self.model = model
        self._timeout_s = timeout_s
        self._max_tokens = max_tokens

    async def complete(self, system: str, user: str) -> str:
        root, token = self._proxy_target()
        body: JsonObject = {
            "model": self.model,
            "max_tokens": self._max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            response = await client.post(
                f"{root.rstrip('/')}/v1/messages",
                json=body,
                headers={"x-api-key": token, "anthropic-version": "2023-06-01"},
            )
        response.raise_for_status()
        return message_text(response.json())


def message_text(payload: object) -> str:
    """Concatenate the text blocks of an Anthropic Messages response."""

    content = payload.get("content") if isinstance(payload, dict) else None
    if not isinstance(content, list):
        raise ValueError("model response has no content")
    text = "".join(
        str(block.get("text", ""))
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )
    if not text.strip():
        raise ValueError("model response has no text")
    return text
