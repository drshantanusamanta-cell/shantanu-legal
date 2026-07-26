"""
llm — provider-agnostic language model clients.

Modules
-------
base              LLMProvider interface, LLMResponse, Attachment
anthropic_client  Claude, with server-side web_search + web_fetch
gemini_client     Gemini, with google_search grounding

IMPORTANT: This file must never be empty. GitHub's web upload interface silently
drops zero-byte files, which causes `ModuleNotFoundError: No module named 'llm'`
on deployment. Keep content here.

(c) 2026 Dr Shantanu Samanta. All rights reserved.
"""

__all__ = ["base", "anthropic_client", "gemini_client"]
__version__ = "1.0.0"
