"""
REMOVED — Anthropic support was dropped at the user's request; this app is
Gemini-only. This file is kept only as a tombstone because the sandbox that
built this project could not delete it outright; it is not imported anywhere.

Do not import from this module. Use llm.gemini_client.GeminiProvider instead.

If Anthropic support is ever wanted again, restore from version control history
or the original implementation, then re-add it to engine.py's LegalEngine and
to app.py's sidebar provider selector.

(c) 2026 Dr Shantanu Samanta. All rights reserved.
"""

raise ImportError(
    "llm.anthropic_client has been removed from this build (Gemini-only). "
    "Do not import it. Use llm.gemini_client.GeminiProvider instead."
)
