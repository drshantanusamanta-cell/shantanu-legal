"""
legal — Indian legal domain logic.

Modules
-------
citations        Citation extraction and normalisation (structured + free-text sweep)
knowledge_bases  Indian Kanoon, eCourts, Digital SCR, India Code, local corpus adapters
verifier         The hard-block citation verifier
prompts          Senior-advocate system prompts and the citation contract

IMPORTANT: This file must never be empty. GitHub's web upload interface silently
drops zero-byte files, which causes `ModuleNotFoundError: No module named 'legal'`
on deployment. Keep content here.

(c) 2026 Dr Shantanu Samanta. All rights reserved.
"""

__all__ = ["citations", "knowledge_bases", "verifier", "prompts"]
__version__ = "1.0.0"
