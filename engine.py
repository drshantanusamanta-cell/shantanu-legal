"""
Orchestration layer: prompt -> retrieve -> generate -> VERIFY -> render.

Everything the UI needs goes through `LegalEngine`. The UI never talks to a
provider directly, which keeps the verification step impossible to bypass.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import PRIMARY_SOURCE_DOMAINS, SECONDARY_SOURCE_DOMAINS, AppSettings
from legal.knowledge_bases import KBDocument, KnowledgeBaseRegistry
from legal.prompts import CITATION_MAP_PROMPT, PROMPTS
from legal.verifier import CitationVerifier, VerificationReport
from llm.anthropic_client import AnthropicProvider
from llm.base import Attachment, LLMResponse
from llm.gemini_client import GeminiProvider


@dataclass
class IKStatus:
    """Per-run record of whether Indian Kanoon actually did any work."""

    configured: bool = False        # token present
    budget_exhausted: bool = False
    prefetch_attempted: bool = False
    prefetch_hits: int = 0
    calls_this_run: int = 0
    spend_this_run_inr: float = 0.0
    citations_verified_via_ik: int = 0
    citations_verified_total: int = 0
    error: str = ""

    @property
    def used(self) -> bool:
        """True when IK contributed to THIS result."""
        return self.calls_this_run > 0 or self.prefetch_hits > 0

    @property
    def level(self) -> str:
        """'full' | 'partial' | 'unused' | 'unavailable' — drives the badge colour."""
        if not self.configured:
            return "unavailable"
        if self.budget_exhausted and not self.used:
            return "unavailable"
        if not self.used:
            return "unused"
        if (
            self.citations_verified_total
            and self.citations_verified_via_ik == self.citations_verified_total
        ):
            return "full"
        return "partial"

    @property
    def headline(self) -> str:
        return {
            "full": "Indian Kanoon — verified",
            "partial": "Indian Kanoon — partial",
            "unused": "Indian Kanoon — not needed",
            "unavailable": "Indian Kanoon — unavailable",
        }[self.level]

    @property
    def detail(self) -> str:
        if self.level == "unavailable":
            if self.budget_exhausted:
                return (
                    "Session budget reached, so Indian Kanoon was not queried for this "
                    "answer. Verification fell back to grounded web search, which is weaker."
                )
            return (
                "No API token configured. Verification for this answer relied on grounded "
                "web search restricted to primary domains — weaker than a database lookup."
            )
        if self.level == "unused":
            return (
                "Token is configured, but this answer needed no case-law lookup "
                "(no authority was asserted, or it was already in context)."
            )
        parts = [
            f"{self.calls_this_run} API call(s), ₹{self.spend_this_run_inr:.2f} spent"
        ]
        if self.prefetch_hits:
            parts.append(f"{self.prefetch_hits} judgment(s) pre-retrieved")
        if self.citations_verified_total:
            parts.append(
                f"{self.citations_verified_via_ik}/{self.citations_verified_total} "
                "citation(s) confirmed against the database"
            )
        return " · ".join(parts) + "."


@dataclass
class EngineResult:
    raw_text: str = ""
    text: str = ""                       # sanitised, safe to display
    report: VerificationReport | None = None
    llm: LLMResponse | None = None
    retrieved: list[KBDocument] = field(default_factory=list)
    error: str = ""
    cost_note: dict[str, Any] = field(default_factory=dict)
    ik: IKStatus = field(default_factory=IKStatus)

    @property
    def ok(self) -> bool:
        return not self.error


class LegalEngine:
    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.registry = KnowledgeBaseRegistry()
        self.anthropic = AnthropicProvider()
        self.gemini = GeminiProvider()

    # ------------------------------------------------------------------
    @property
    def provider(self):
        return self.anthropic if self.settings.provider == "anthropic" else self.gemini

    def provider_status(self) -> list[dict[str, Any]]:
        return [
            {"name": "Anthropic (Claude)", "key": "anthropic", "available": self.anthropic.available},
            {"name": "Google (Gemini)", "key": "gemini", "available": self.gemini.available},
        ]

    def any_provider_available(self) -> bool:
        return self.anthropic.available or self.gemini.available

    def allowed_domains(self) -> list[str] | None:
        if not self.settings.restrict_to_primary:
            return None
        # Anthropic caps domain-list size implicitly via request size; keep it tight.
        return PRIMARY_SOURCE_DOMAINS[:30]

    # ------------------------------------------------------------------
    def prefetch(self, query: str, max_results: int = 6) -> list[KBDocument]:
        """
        Retrieve real authority BEFORE generation and put it in the prompt.

        This is the single biggest quality lever: grounding the model in real
        retrieved documents means it has correct citations available and does
        not need to reach into memory for them.
        """
        if not self.settings.use_indian_kanoon:
            return []
        try:
            return self.registry.search_case_law(
                query, court=self.settings.court_filter, max_results=max_results
            )
        except PermissionError:
            return []
        except Exception:
            return []

    @staticmethod
    def _format_retrieved(docs: list[KBDocument]) -> str:
        if not docs:
            return ""
        lines = [
            "\n\n## RETRIEVED AUTHORITIES (verified real documents — cite these, "
            "and prefer them over anything you recall)\n"
        ]
        for i, d in enumerate(docs, 1):
            lines.append(
                f"\n[{i}] {d.title}\n"
                f"    Court: {d.court or 'n/a'} | Date: {d.date or 'n/a'}\n"
                f"    Citation: {d.citation or 'n/a'}\n"
                f"    URL: {d.url}\n"
                f"    Extract: {(d.snippet or d.full_text[:500])[:700]}\n"
            )
        lines.append(
            "\nUse the URLs above verbatim in the [[CITE:]] source field. "
            "Do not alter titles or citations.\n"
        )
        return "".join(lines)

    # ------------------------------------------------------------------
    def run(
        self,
        mode: str,
        user_input: str,
        *,
        history: list[dict[str, Any]] | None = None,
        attachments: list[Attachment] | None = None,
        document_context: str = "",
        prefetch_query: str = "",
        enable_search: bool = True,
        verify: bool = True,
    ) -> EngineResult:
        result = EngineResult()

        # Snapshot Indian Kanoon accounting so we can attribute usage to THIS run.
        ik_kb = self.registry.indian_kanoon
        ik_spend_before = ik_kb.spend_inr
        ik_calls_before = sum(ik_kb.call_counts.values())
        result.ik.configured = bool(ik_kb.token)
        result.ik.budget_exhausted = ik_kb.budget_exhausted

        provider = self.provider
        if not provider.available:
            other = self.gemini if self.settings.provider == "anthropic" else self.anthropic
            if other.available:
                provider = other
                result.cost_note["provider_fallback"] = (
                    f"{self.settings.provider} unavailable; used {provider.name}."
                )
            else:
                result.error = (
                    "No LLM provider configured. Add ANTHROPIC_API_KEY or GEMINI_API_KEY "
                    "to your secrets."
                )
                return result

        system = PROMPTS.get(mode, PROMPTS["research"])

        # ---- ground the model in real retrieved authority ----
        retrieved: list[KBDocument] = []
        if enable_search and mode in ("research", "summary", "authority", "drafting"):
            q = prefetch_query.strip() or user_input.strip()
            if q:
                result.ik.prefetch_attempted = bool(
                    self.settings.use_indian_kanoon and ik_kb.available
                )
                retrieved = self.prefetch(q[:400])
        result.retrieved = retrieved
        result.ik.prefetch_hits = sum(
            1 for d in retrieved if d.source == "indiankanoon"
        )

        prompt_parts = []
        if document_context:
            prompt_parts.append(
                "## DOCUMENTS PROVIDED BY THE USER\n"
                "(Quote these freely — they are the record, not an external authority.)\n"
                + document_context
            )
        if retrieved:
            prompt_parts.append(self._format_retrieved(retrieved))
        prompt_parts.append(f"\n\n## INSTRUCTION\n{user_input}")
        composed = "\n".join(prompt_parts)

        messages = list(history or [])
        messages.append({"role": "user", "content": composed})

        llm = provider.generate(
            system=system,
            messages=messages,
            model=(
                self.settings.anthropic_model
                if provider.name == "anthropic"
                else self.settings.gemini_model
            ),
            max_tokens=self.settings.max_tokens,
            temperature=self.settings.temperature,
            enable_search=enable_search,
            allowed_domains=self.allowed_domains(),
            max_searches=self.settings.max_searches,
            attachments=attachments,
        )
        result.llm = llm

        if not llm.ok:
            result.error = llm.error
            return result

        result.raw_text = llm.text

        # ---- HARD BLOCK ----
        if verify:
            verifier = CitationVerifier(
                self.registry,
                strict=self.settings.strict_mode,
                require_quote_match=self.settings.require_quote_match,
            )
            report = verifier.verify(
                llm.text, web_citations=llm.citations, retrieved_docs=retrieved
            )
            result.report = report
            result.text = report.sanitised_text

            result.ik.citations_verified_total = len(report.verified)
            result.ik.citations_verified_via_ik = sum(
                1 for c in report.verified if c.verified_via == "indiankanoon"
            )
        else:
            result.text = llm.text

        # Attribute Indian Kanoon usage to this run.
        result.ik.calls_this_run = sum(ik_kb.call_counts.values()) - ik_calls_before
        result.ik.spend_this_run_inr = round(ik_kb.spend_inr - ik_spend_before, 2)
        result.ik.budget_exhausted = ik_kb.budget_exhausted

        if ik_kb.token:
            result.cost_note["indian_kanoon"] = ik_kb.spend_summary()
        if llm.usage:
            result.cost_note["llm_usage"] = llm.usage

        return result

    # ------------------------------------------------------------------
    def citation_map(self, context: str) -> dict[str, Any]:
        """Build a precedent graph from verified cases only."""
        provider = self.provider if self.provider.available else (
            self.gemini if self.anthropic.available is False else self.anthropic
        )
        if not provider.available:
            return {"nodes": [], "links": []}

        llm = provider.generate(
            system=CITATION_MAP_PROMPT,
            messages=[{"role": "user", "content": context[:30000]}],
            model=(
                self.settings.anthropic_model
                if provider.name == "anthropic"
                else self.settings.gemini_model
            ),
            max_tokens=2000,
            temperature=0.0,
            enable_search=False,
        )
        if not llm.ok:
            return {"nodes": [], "links": []}

        import json
        import re

        raw = re.sub(r"```(?:json)?", "", llm.text).strip()
        m = re.search(r"\{.*\}", raw, re.S)
        if not m:
            return {"nodes": [], "links": []}
        try:
            data = json.loads(m.group(0))
            return {
                "nodes": data.get("nodes", []) or [],
                "links": data.get("links", []) or [],
            }
        except Exception:
            return {"nodes": [], "links": []}
