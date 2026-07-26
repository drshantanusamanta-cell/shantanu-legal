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

QUOTA HANDLING
--------------
The free tier is genuinely stingy for gemini-2.5-pro (roughly 5 requests/min,
50/day) versus gemini-2.5-flash (roughly 15/min, 1,500/day) — see
https://ai.google.dev/gemini-api/docs/rate-limits for current figures, which
Google does change. A 429 here is a real account-level quota limit, not a bug
in this app; the fixes available at the code level are (a) retry with the
delay Google actually asks for, (b) fall back to the much more generous Flash
model rather than failing outright, and (c) tell the user plainly what
happened and what their options are. That is what this module does.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from config import GEMINI_QUOTA_FALLBACK_MODEL, get_secret
from llm.base import Attachment, LLMProvider, LLMResponse

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"
TIMEOUT = 180

# Keep retry waits bounded even if Google asks for longer — a Streamlit
# request should fail fast enough for the user to see a clear message and
# choose Flash, rather than hang for a minute-plus.
MAX_RETRY_SLEEP_SECONDS = 12.0
MAX_ATTEMPTS_PER_MODEL = 3

QUOTA_HELP_URL = "https://ai.google.dev/gemini-api/docs/rate-limits"


def _parse_retry_delay(body_text: str, headers: dict[str, str]) -> float | None:
    """
    Pull a retry delay out of Google's response, in seconds.

    Two sources, checked in order:
      1. The `Retry-After` HTTP header (seconds, or an HTTP-date — we only
         handle the common seconds form here).
      2. The `RetryInfo` detail Google embeds in the JSON error body, shaped
         like {"retryDelay": "38s"}.
    """
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass

    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', body_text or "")
    if m:
        try:
            return float(m.group(1))
        except ValueError:
            pass
    return None


def _classify_error(status_code: int, body_text: str) -> str:
    if status_code == 429:
        return "quota"
    if status_code in (401, 403):
        return "auth"
    if status_code >= 500:
        return "network"
    return "other"


def _quota_message(model: str, fallback_tried: bool, fallback_model: str) -> str:
    base = (
        f"Gemini rejected the request with a quota error (HTTP 429) for `{model}`. "
        "This is Google's free-tier rate limit, not a bug in the app — "
        "gemini-2.5-pro's free allowance is roughly 5 requests/minute and 50/day, "
        f"far smaller than {fallback_model}'s roughly 15/minute and 1,500/day."
    )
    if fallback_tried:
        base += (
            f" The app automatically retried with `{fallback_model}` as well, and "
            "that also hit a quota limit — which usually means the whole Google "
            "Cloud project's quota is exhausted, not just one model."
        )
    base += (
        f"\n\nOptions: switch the sidebar model to **{fallback_model}** if you "
        "haven't already, wait for the quota to reset (daily caps reset at "
        "midnight Pacific time), or enable billing on your Google Cloud project "
        f"to move off the free tier. Current limits: {QUOTA_HELP_URL}"
    )
    return base


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self, api_key: str | None = None):
        # Lazy for the same reason as the Anthropic provider (Streamlit caching).
        self._api_key_override = api_key
        self._session = requests.Session()

    @property
    def api_key(self) -> str | None:
        raw = (
            self._api_key_override
            or get_secret("GEMINI_API_KEY")
            or get_secret("GOOGLE_API_KEY")
        )
        if raw is None:
            return None
        key = str(raw).strip().strip('"').strip("'")
        if not key or key.startswith("AIza..."):
            return None
        return key

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
    def _call_model(
        self, model: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, dict[str, Any]]:
        """
        One model's worth of attempts. Returns (data, {}) on success, or
        (None, error_info) on failure. error_info carries status_code, kind,
        message and retry_after_seconds so the caller can decide whether to
        fall back to another model or give up.
        """
        url = f"{API_ROOT}/{model}:generateContent"
        delay = 1.5
        last_status = 0
        last_text = ""
        last_headers: dict[str, str] = {}
        last_retry_after: float | None = None

        for attempt in range(MAX_ATTEMPTS_PER_MODEL):
            try:
                r = self._session.post(
                    url,
                    params={"key": self.api_key},
                    headers={"Content-Type": "application/json"},
                    json=payload,
                    timeout=TIMEOUT,
                )
            except Exception as exc:  # noqa: BLE001
                last_status, last_text = 0, str(exc)
                time.sleep(min(delay, MAX_RETRY_SLEEP_SECONDS))
                delay *= 2
                continue

            if r.status_code == 429 or r.status_code >= 500:
                last_status, last_text, last_headers = r.status_code, r.text, dict(r.headers)
                last_retry_after = _parse_retry_delay(r.text, last_headers)
                if attempt < MAX_ATTEMPTS_PER_MODEL - 1:
                    wait = last_retry_after if last_retry_after is not None else delay
                    time.sleep(min(wait, MAX_RETRY_SLEEP_SECONDS))
                    delay *= 2
                continue

            if r.status_code >= 400:
                return None, {
                    "status_code": r.status_code,
                    "kind": _classify_error(r.status_code, r.text),
                    "message": f"HTTP {r.status_code}: {r.text[:500]}",
                    "retry_after_seconds": None,
                }

            try:
                return r.json(), {}
            except Exception as exc:  # noqa: BLE001
                return None, {
                    "status_code": r.status_code,
                    "kind": "other",
                    "message": f"Could not parse Gemini response as JSON: {exc}",
                    "retry_after_seconds": None,
                }

        return None, {
            "status_code": last_status,
            "kind": _classify_error(last_status, last_text) if last_status else "network",
            "message": last_text[:500] or "request failed with no response",
            "retry_after_seconds": last_retry_after,
        }

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
        out = LLMResponse(provider=self.name, model=model, requested_model=model)
        if not self.available:
            out.error = "Gemini API key not configured. Set GEMINI_API_KEY."
            out.error_kind = "auth"
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

        data, err = self._call_model(model, payload)

        fallback_tried = False
        if data is None and err.get("kind") == "quota" and model != GEMINI_QUOTA_FALLBACK_MODEL:
            # The configured model is rate-limited. Flash's free allowance is
            # roughly 30x larger, so retry there rather than failing outright.
            fallback_tried = True
            data, err2 = self._call_model(GEMINI_QUOTA_FALLBACK_MODEL, payload)
            if data is not None:
                out.model = GEMINI_QUOTA_FALLBACK_MODEL
                out.notices.append(
                    f"`{model}` was rate-limited (free-tier quota), so this answer used "
                    f"`{GEMINI_QUOTA_FALLBACK_MODEL}` instead."
                )
            else:
                err = err2
                err["kind"] = "quota"  # both failed on quota; treat as quota overall

        if data is None:
            out.error_kind = err.get("kind", "other")
            out.retry_after_seconds = err.get("retry_after_seconds")
            if out.error_kind == "quota":
                out.error = _quota_message(model, fallback_tried, GEMINI_QUOTA_FALLBACK_MODEL)
            elif out.error_kind == "auth":
                out.error = (
                    f"Gemini rejected the API key (HTTP {err.get('status_code')}). "
                    "Check that GEMINI_API_KEY is correct and has the Generative "
                    f"Language API enabled. Details: {err.get('message', '')}"
                )
            else:
                out.error = f"Gemini API failed after retries: {err.get('message', 'unknown error')}"
            return out

        candidates = data.get("candidates") or []
        if not candidates:
            fb = data.get("promptFeedback", {})
            out.error = f"Gemini returned no candidates. Feedback: {json.dumps(fb)[:300]}"
            out.error_kind = "other"
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
