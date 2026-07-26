"""
Tests for Gemini quota (429) handling: fallback, Retry-After parsing, and error
classification. No real network calls — `requests.Session.post` is monkeypatched
to return scripted responses.

Run:  python tests/test_gemini_quota.py     (or: python -m pytest tests/ -v)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import llm.gemini_client as gc  # noqa: E402
from llm.gemini_client import (  # noqa: E402
    GeminiProvider,
    _classify_error,
    _parse_retry_delay,
)

# The retry loop calls time.sleep() using Google's own retryDelay hint (up to
# MAX_RETRY_SLEEP_SECONDS per attempt), which is correct production behaviour
# but would make this suite take the better part of a minute to run. Patch it
# out — the sleep durations themselves are covered by test_parse_retry_delay_*.
gc.time.sleep = lambda *_a, **_k: None


class FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None,
                 text: str | None = None, headers: dict | None = None):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.text = text if text is not None else str(self._json)
        self.headers = headers or {}

    def json(self):
        return self._json


def quota_error_response(retry_delay_s: str | None = "38s", headers: dict | None = None):
    details = []
    if retry_delay_s:
        details.append({
            "@type": "type.googleapis.com/google.rpc.RetryInfo",
            "retryDelay": retry_delay_s,
        })
    body = {
        "error": {
            "code": 429,
            "message": "You exceeded your current quota, please check your plan and billing details.",
            "status": "RESOURCE_EXHAUSTED",
            "details": details,
        }
    }
    import json as _json
    return FakeResponse(429, json_body=body, text=_json.dumps(body), headers=headers or {})


def ok_response(text: str = "The answer is 42."):
    body = {
        "candidates": [{
            "content": {"parts": [{"text": text}]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
    return FakeResponse(200, json_body=body)


def make_provider(monkeypatch, responses_by_model: dict[str, list]) -> GeminiProvider:
    """
    responses_by_model maps model name -> list of FakeResponse to return in
    order for successive calls against that model's URL.
    """
    provider = GeminiProvider(api_key="fake-key-for-tests")
    call_log: list[str] = []
    state = {m: list(rs) for m, rs in responses_by_model.items()}

    def fake_post(url, params=None, headers=None, json=None, timeout=None):
        model = url.rsplit("/", 1)[-1].split(":")[0]
        call_log.append(model)
        queue = state.get(model, [])
        if not queue:
            raise AssertionError(f"No scripted response left for model {model}")
        return queue.pop(0)

    monkeypatch_session_post(provider, fake_post)
    provider._test_call_log = call_log  # type: ignore[attr-defined]
    return provider


def monkeypatch_session_post(provider: GeminiProvider, fn) -> None:
    provider._session.post = fn  # type: ignore[method-assign]


# ---------------------------------------------------------------- unit: parsing
def test_parse_retry_delay_from_body():
    body = '{"error":{"details":[{"retryDelay":"38s"}]}}'
    assert _parse_retry_delay(body, {}) == 38.0


def test_parse_retry_delay_from_header():
    assert _parse_retry_delay("{}", {"Retry-After": "12"}) == 12.0


def test_parse_retry_delay_prefers_header_over_body():
    body = '{"error":{"details":[{"retryDelay":"38s"}]}}'
    assert _parse_retry_delay(body, {"Retry-After": "5"}) == 5.0


def test_parse_retry_delay_missing_returns_none():
    assert _parse_retry_delay("{}", {}) is None


def test_classify_error_kinds():
    assert _classify_error(429, "") == "quota"
    assert _classify_error(401, "") == "auth"
    assert _classify_error(403, "") == "auth"
    assert _classify_error(500, "") == "network"
    assert _classify_error(503, "") == "network"
    assert _classify_error(400, "") == "other"


# ---------------------------------------------------------------- integration: fallback
def test_pro_quota_exhausted_falls_back_to_flash(monkeypatch):
    """
    The headline behaviour this whole fix is about: gemini-2.5-pro hits 429
    repeatedly, and the provider automatically succeeds on gemini-2.5-flash
    instead of just failing.
    """
    provider = make_provider(monkeypatch, {
        "gemini-2.5-pro": [quota_error_response()] * 3,   # MAX_ATTEMPTS_PER_MODEL
        "gemini-2.5-flash": [ok_response("Fallback answer.")],
    })

    resp = provider.generate(
        system="You are a test.", messages=[{"role": "user", "content": "hi"}],
        model="gemini-2.5-pro", enable_search=False,
    )

    assert resp.ok, f"Expected success via fallback, got error: {resp.error}"
    assert resp.text == "Fallback answer."
    assert resp.requested_model == "gemini-2.5-pro"
    assert resp.model == "gemini-2.5-flash", "Response must report the model that actually served it"
    assert any("rate-limited" in n for n in resp.notices), "User must be told a fallback happened"
    assert provider._test_call_log.count("gemini-2.5-pro") == 3
    assert provider._test_call_log.count("gemini-2.5-flash") == 1


def test_both_models_quota_exhausted_gives_clear_quota_error(monkeypatch):
    provider = make_provider(monkeypatch, {
        "gemini-2.5-pro": [quota_error_response()] * 3,
        "gemini-2.5-flash": [quota_error_response()] * 3,
    })

    resp = provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="gemini-2.5-pro", enable_search=False,
    )

    assert not resp.ok
    assert resp.error_kind == "quota"
    assert "gemini-2.5-pro" in resp.error
    assert "automatically retried" in resp.error, "Must explain the fallback was attempted"
    assert "billing" in resp.error.lower() or "enable billing" in resp.error.lower()


def test_flash_default_does_not_fall_back_to_itself(monkeypatch):
    """If the caller is already on the fallback model, there is nowhere to fall
    back to — must fail cleanly rather than loop or double-call."""
    provider = make_provider(monkeypatch, {
        "gemini-2.5-flash": [quota_error_response()] * 3,
    })

    resp = provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="gemini-2.5-flash", enable_search=False,
    )

    assert not resp.ok
    assert resp.error_kind == "quota"
    assert provider._test_call_log.count("gemini-2.5-flash") == 3
    assert "gemini-2.5-pro" not in provider._test_call_log


def test_success_on_first_try_no_fallback_no_notice(monkeypatch):
    provider = make_provider(monkeypatch, {
        "gemini-2.5-pro": [ok_response("Direct success.")],
    })

    resp = provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="gemini-2.5-pro", enable_search=False,
    )

    assert resp.ok
    assert resp.text == "Direct success."
    assert resp.model == "gemini-2.5-pro"
    assert resp.notices == []
    assert provider._test_call_log == ["gemini-2.5-pro"]


def test_transient_500_recovers_without_fallback(monkeypatch):
    """A transient server error should retry the SAME model, not jump to Flash —
    fallback is specifically for quota exhaustion, not generic flakiness."""
    provider = make_provider(monkeypatch, {
        "gemini-2.5-pro": [FakeResponse(503, text="server hiccup"), ok_response("Recovered.")],
    })

    resp = provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="gemini-2.5-pro", enable_search=False,
    )

    assert resp.ok
    assert resp.text == "Recovered."
    assert resp.model == "gemini-2.5-pro"
    assert provider._test_call_log == ["gemini-2.5-pro", "gemini-2.5-pro"]


def test_auth_error_is_not_retried_as_quota(monkeypatch):
    provider = make_provider(monkeypatch, {
        "gemini-2.5-pro": [FakeResponse(403, text='{"error":{"message":"API key invalid"}}')],
    })

    resp = provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="gemini-2.5-pro", enable_search=False,
    )

    assert not resp.ok
    assert resp.error_kind == "auth"
    assert "API key" in resp.error
    # A 4xx that isn't 429 must not trigger the quota fallback machinery.
    assert provider._test_call_log == ["gemini-2.5-pro"]


def test_retry_after_surfaced_on_final_failure(monkeypatch):
    provider = make_provider(monkeypatch, {
        "gemini-2.5-pro": [quota_error_response(retry_delay_s="7s")] * 3,
        "gemini-2.5-flash": [quota_error_response(retry_delay_s="7s")] * 3,
    })

    resp = provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}],
        model="gemini-2.5-pro", enable_search=False,
    )

    assert not resp.ok
    assert resp.retry_after_seconds == 7.0


def test_no_api_key_fails_fast_without_network_call(monkeypatch):
    provider = GeminiProvider(api_key="")
    calls = []
    monkeypatch_session_post(provider, lambda *a, **k: calls.append(1) or ok_response())

    resp = provider.generate(
        system="sys", messages=[{"role": "user", "content": "hi"}], model="gemini-2.5-pro",
    )
    assert not resp.ok
    assert resp.error_kind == "auth"
    assert calls == [], "Must not attempt a network call with no key configured"


# ---------------------------------------------------------------- config sanity
def test_default_model_is_the_generous_one():
    """The actual fix for the reported 429: default to Flash, not Pro."""
    from config import DEFAULT_GEMINI_MODEL, GEMINI_QUOTA_FALLBACK_MODEL

    assert DEFAULT_GEMINI_MODEL == "gemini-2.5-flash"
    assert GEMINI_QUOTA_FALLBACK_MODEL == "gemini-2.5-flash"


# ---------------------------------------------------------------- minimal monkeypatch shim
class _MonkeyPatch:
    """Tiny stand-in so this file has zero external test-framework dependency."""

    def setattr(self, *a, **k):  # unused; kept for pytest compatibility if run there
        pass


def _run_with_shim(fn):
    fn(_MonkeyPatch())


# ---------------------------------------------------------------- runner
if __name__ == "__main__":
    import inspect
    import traceback

    GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"
    fns = [(n, o) for n, o in sorted(globals().items())
           if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in fns:
        try:
            sig = inspect.signature(fn)
            if "monkeypatch" in sig.parameters:
                fn(_MonkeyPatch())
            else:
                fn()
            print(f"  {GREEN}PASS{RESET}  {name}")
            passed += 1
        except AssertionError as exc:
            print(f"  {RED}FAIL{RESET}  {name}\n        {exc}")
            failed += 1
        except Exception:
            print(f"  {RED}ERROR{RESET} {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    sys.exit(1 if failed else 0)
