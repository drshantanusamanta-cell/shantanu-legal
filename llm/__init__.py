"""
llm — provider-agnostic language model clients.

Gemini-only build. Anthropic support was removed at the user's request.
llm/anthropic_client.py is kept only as a tombstone (raises ImportError on
import) because the build sandbox could not delete it outright.

Modules
-------
base           LLMProvider interface, LLMResponse, Attachment
gemini_client  Gemini, with google_search grounding

IMPORTANT: This file must never be empty. GitHub's web upload interface silently
drops zero-byte files, which causes `ModuleNotFoundError: No module named 'llm'`
on deployment. Keep content here.

(c) 2026 Dr Shantanu Samanta. All rights reserved.
"""

__all__ = ["base", "gemini_client"]
__version__ = "1.1.0"
