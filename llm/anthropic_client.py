"""
Anthropic provider.

Uses the server-side `web_search` tool so retrieval happens inside Anthropic's
infrastructure and comes back with citations that carry `url`, `title` and
`cited_text` -- exactly the fields the hard-block verifier needs.

Two details that matter and are easy to get wrong:

  1. `pause_turn`: long search turns can be paused. You must send the paused
     assistant message back UNCHANGED to continue. We loop on this.
  2. `encrypted_content`: multi-turn conversations must echo search results
     back byte-for-byte or the API 400s. We keep raw blocks for that.
"""

from __future__ import annotations

import base64
from typing import Any

from config import WEB_FETCH_TOOL_TYPE, WEB_SEARCH_TOOL_TYPE, get_secret
from llm.base import Attachment, LLMProvider, LLMResponse

try:
    import anthropic
except Exception:  # pragma: no cover
    anthropic = None  # type: ignore


MAX_PAUSE_CONTINUATIONS = 6


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or get_secret("ANTHROPIC_API_KEY")
        self._client = None
        if self.api_key and anthropic is not None:
            try:
                self._client = anthropic.Anthropic(api_key=self.api_key)
            except Exception:
                self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------
    def _build_tools(
        self, enable_search: bool, allowed_domains: list[str] | None, max_searches: int
    ) -> list[dict[str, Any]]:
        if not enable_search:
            return []
        search_tool: dict[str, Any] = {
            "type": WEB_SEARCH_TOOL_TYPE,
            "name": "web_search",
            "max_uses": max_searches,
        }
        fetch_tool: dict[str, Any] = {
            "type": WEB_FETCH_TOOL_TYPE,
            "name": "web_fetch",
            "max_uses": max(3, max_searches // 2),
            "citations": {"enabled": True},
            "max_content_tokens": 60000,
        }
        if allowed_domains:
            # Confining search to primary sources is the single highest-leverage
            # guard against fabricated authority.
            search_tool["allowed_domains"] = allowed_domains
            fetch_tool["allowed_domains"] = allowed_domains
        return [search_tool, fetch_tool]

    @staticmethod
    def _content_with_attachments(
        text: str, attachments: list[Attachment] | None
    ) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = []
        for att in attachments or []:
            if att.is_pdf:
                blocks.append(
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": att.data_b64,
                        },
                        "title": att.filename or "Uploaded document",
                        "citations": {"enabled": True},
                    }
                )
            else:
                blocks.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": att.media_type,
                            "data": att.data_b64,
                        },
                    }
                )
        blocks.append({"type": "text", "text": text})
        return blocks

    # ------------------------------------------------------------------
    def generate(
        self,
        system: str,
        messages: list[dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 8000,
        temperature: float = 0.2,
        enable_search: bool = True,
        allowed_domains: list[str] | None = None,
        max_searches: int = 8,
        attachments: list[Attachment] | None = None,
    ) -> LLMResponse:
        if not self.available:
            return LLMResponse(
                error="Anthropic API key not configured. Set ANTHROPIC_API_KEY.",
                provider=self.name,
            )

        api_messages = [dict(m) for m in messages]
        if attachments and api_messages:
            last = api_messages[-1]
            if last.get("role") == "user":
                txt = last["content"] if isinstance(last["content"], str) else ""
                last["content"] = self._content_with_attachments(txt, attachments)

        tools = self._build_tools(enable_search, allowed_domains, max_searches)

        out = LLMResponse(provider=self.name, model=model)
        raw_blocks: list[Any] = []

        try:
            continuations = 0
            while True:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system,
                    "messages": api_messages,
                }
                if tools:
                    kwargs["tools"] = tools

                resp = self._client.messages.create(**kwargs)  # type: ignore[union-attr]
                raw_blocks.extend(resp.content)

                for block in resp.content:
                    btype = getattr(block, "type", "")
                    if btype == "text":
                        out.text += getattr(block, "text", "")
                        for c in getattr(block, "citations", None) or []:
                            out.citations.append(
                                {
                                    "url": getattr(c, "url", "") or "",
                                    "title": getattr(c, "title", "")
                                    or getattr(c, "document_title", "")
                                    or "",
                                    "cited_text": getattr(c, "cited_text", "") or "",
                                    "source": "anthropic_citation",
                                }
                            )
                    elif btype == "server_tool_use":
                        inp = getattr(block, "input", {}) or {}
                        if getattr(block, "name", "") == "web_search":
                            q = inp.get("query", "")
                            if q:
                                out.searches_performed.append(q)
                    elif btype == "web_search_tool_result":
                        content = getattr(block, "content", None)
                        if isinstance(content, list):
                            for r in content:
                                url = getattr(r, "url", "") or ""
                                if url:
                                    out.citations.append(
                                        {
                                            "url": url,
                                            "title": getattr(r, "title", "") or "",
                                            "cited_text": "",
                                            "source": "web_search_result",
                                        }
                                    )
                    elif btype == "web_fetch_tool_result":
                        content = getattr(block, "content", None)
                        url = getattr(content, "url", "") if content is not None else ""
                        if url:
                            out.citations.append(
                                {"url": url, "title": "", "cited_text": "",
                                 "source": "web_fetch_result"}
                            )

                out.stop_reason = getattr(resp, "stop_reason", "") or ""

                usage = getattr(resp, "usage", None)
                if usage is not None:
                    out.usage = {
                        "input_tokens": getattr(usage, "input_tokens", 0),
                        "output_tokens": getattr(usage, "output_tokens", 0),
                    }
                    stu = getattr(usage, "server_tool_use", None)
                    if stu is not None:
                        out.usage["web_search_requests"] = getattr(
                            stu, "web_search_requests", 0
                        )

                # A paused turn must be continued by echoing the assistant
                # message back exactly as received.
                if out.stop_reason == "pause_turn" and continuations < MAX_PAUSE_CONTINUATIONS:
                    api_messages.append({"role": "assistant", "content": resp.content})
                    continuations += 1
                    continue
                break

        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            if "credit" in msg.lower() or "billing" in msg.lower():
                msg += "  (Check your Anthropic account balance.)"
            out.error = f"Anthropic API error: {msg}"

        # De-duplicate citations by URL, preferring ones with cited_text.
        dedup: dict[str, dict[str, Any]] = {}
        for c in out.citations:
            u = c.get("url", "")
            if not u:
                continue
            if u not in dedup or (c.get("cited_text") and not dedup[u].get("cited_text")):
                dedup[u] = c
        out.citations = list(dedup.values())

        return out


def encode_attachment(file_bytes: bytes, media_type: str, filename: str = "") -> Attachment:
    return Attachment(
        data_b64=base64.standard_b64encode(file_bytes).decode("utf-8"),
        media_type=media_type,
        filename=filename,
    )
