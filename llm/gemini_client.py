"""
Google Gemini provider.

Carried over from the original React prototype, now server-side so the key is
never exposed to the browser. Uses Google Search grounding, and normalises
`groundingMetadata` into the same citation shape the verifier expects.

Note a real asymmetry: Gemini's grounding does not accept a domain allow-list,
so `allowed_domains` cannot be enforced at the API level the way it can with
Anthropic. We compensate by (a) instructing the model in the prompt and
(b) letting the verifier reject any citation that is not on a primary domain.
The verifier is the real guard either way.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from config import get_secret
from llm.base import Attachment, LLMProvider, LLMResponse

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = 180


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or get_secret("GEMINI_API_KEY") or get_secret("GOOGLE_API_KEY")
        self._session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    # ------------------------------------------------------------------
    @staticmethod
    def _parts_for(text: str, attachments: list[Attachment] | None) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        for att in attachments or []:
            parts.append(
                {"inline_data": {"mime_type": att.media_type, "data": att.data_b64}}
            )
        parts.append({"text": text})
        return parts

    def _to_contents(
        self, messages: list[dict[str, Any]], attachments: list[Attachment] | None
    ) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for i, m in enumerate(messages):
            role = "user" if m.get("role") == "user" else "model"
            text = m.get("content", "")
            if not isinstance(text, str):
                text = json.dumps(text)
            is_last = i == len(messages) - 1
            parts = (
                self._parts_for(text, attachments)
                if (is_last and role == "user")
                else [{"text": text}]
            )
            contents.append({"role": role, "parts": parts})
        return contents

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
        out = LLMResponse(provider=self.name, model=model)
        if not self.available:
            out.error = "Gemini API key not configured. Set GEMINI_API_KEY."
            return out

        # Gemini can't filter domains server-side, so say it in the prompt.
        sys_text = system
        if enable_search and allowed_domains:
            sys_text += (
                "\n\nSEARCH RESTRICTION: Only rely on results from these domains: "
                + ", ".join(allowed_domains[:25])
                + ". Any authority sourced elsewhere will be discarded downstream."
            )

        payload: dict[str, Any] = {
            "system_instruction": {"parts": [{"text": sys_text}]},
            "contents": self._to_contents(messages, attachments),
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if enable_search:
            payload["tools"] = [{"google_search": {}}]

        url = f"{API_ROOT}/{model}:generateContent"

        delay = 1.5
        last_err = ""
        for attempt in range(4):
            try:
                r = self._session.post(
                    url,
                    params={"key": self.api_key},
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=TIMEOUT,
                )
                if r.status_code == 429 or r.status_code >= 500:
                    last_err = f"HTTP {r.status_code}: {r.text[:300]}"
                    import time

                    time.sleep(delay)
                    delay *= 2
                    continue
                if r.status_code >= 400:
                    out.error = f"Gemini API error HTTP {r.status_code}: {r.text[:500]}"
                    return out
                data = r.json()
                break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                import time

                time.sleep(delay)
                delay *= 2
        else:
            out.error = f"Gemini API failed after retries: {last_err}"
            return out

        candidates = data.get("candidates") or []
        if not candidates:
            fb = data.get("promptFeedback", {})
            out.error = f"Gemini returned no candidates. Feedback: {json.dumps(fb)[:300]}"
            return out

        cand = candidates[0]
        for part in (cand.get("content") or {}).get("parts") or []:
            if "text" in part:
                out.text += part["text"]

        out.stop_reason = cand.get("finishReason", "") or ""

        # ---- grounding metadata -> normalised citations ----
        gm = cand.get("groundingMetadata") or {}
        for q in gm.get("webSearchQueries") or []:
            out.searches_performed.append(q)

        chunks = gm.get("groundingChunks") or []
        chunk_urls: list[dict[str, str]] = []
        for ch in chunks:
            web = ch.get("web") or {}
            chunk_urls.append(
                {"url": web.get("uri", ""), "title": web.get("title", "")}
            )

        # supports map text spans -> chunk indices, giving us cited_text
        for sup in gm.get("groundingSupports") or []:
            seg = sup.get("segment") or {}
            cited_text = seg.get("text", "")
            for idx in sup.get("groundingChunkIndices") or []:
                if 0 <= idx < len(chunk_urls) and chunk_urls[idx]["url"]:
                    out.citations.append(
                        {
                            "url": chunk_urls[idx]["url"],
                            "title": chunk_urls[idx]["title"],
                            "cited_text": cited_text,
                            "source": "gemini_grounding",
                        }
                    )

        # Legacy shape used by older API versions.
        for g in gm.get("groundingAttributions") or []:
            web = g.get("web") or {}
            if web.get("uri"):
                out.citations.append(
                    {
                        "url": web["uri"],
                        "title": web.get("title", ""),
                        "cited_text": "",
                        "source": "gemini_attribution",
                    }
                )

        # Any chunk we never matched to a span still counts as a consulted source.
        seen = {c["url"] for c in out.citations}
        for cu in chunk_urls:
            if cu["url"] and cu["url"] not in seen:
                out.citations.append(
                    {"url": cu["url"], "title": cu["title"], "cited_text": "",
                     "source": "gemini_chunk"}
                )

        usage = data.get("usageMetadata") or {}
        out.usage = {
            "input_tokens": usage.get("promptTokenCount", 0),
            "output_tokens": usage.get("candidatesTokenCount", 0),
        }
        return out
