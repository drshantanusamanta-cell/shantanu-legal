"""
HARD-BLOCK citation verifier.

Design principle: the model is a *drafter*, never an *authority*. Nothing the
model asserts about a case is shown to the user unless an independent retrieval
system returned a matching real document.

Pipeline
--------
    raw model output
        |
        v
    extract candidates  (structured [[CITE:]] + free-text sweep)
        |
        v
    for each candidate:
        try Indian Kanoon  (cite: / title: filters)   -> strongest
        try local corpus
        try web_search citations from whitelisted domains
        try Digital SCR
        |
        v
    verified?  -- yes -> render with real URL, real title, real court/date
               -- no  -> DELETE the assertion from the text, log suppression
        |
        v
    quote check: every quoted passage must appear in a retrieved document
        |
        v
    sanitised text + audit report

The suppression log is shown to the user. Silence would be worse than the
hallucination: the user must know an authority was removed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from config import PRIMARY_SOURCE_DOMAINS, SECONDARY_SOURCE_DOMAINS
from legal.citations import (
    CitationCandidate,
    extract_all,
    extract_quotes,
    normalise,
    strip_structured_markers,
    token_set_ratio,
)
from legal.knowledge_bases import KBDocument, KnowledgeBaseRegistry


# Similarity required between the model's case name and the retrieved title.
TITLE_MATCH_THRESHOLD = 0.62
# Similarity required for a quoted passage to count as genuinely present.
QUOTE_MATCH_THRESHOLD = 0.80

SUPPRESSION_NOTICE = "*[authority suppressed — could not be verified against a primary source]*"


def domain_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def is_primary_domain(url: str) -> bool:
    d = domain_of(url)
    return any(d == pd or d.endswith("." + pd) for pd in PRIMARY_SOURCE_DOMAINS)


def is_secondary_domain(url: str) -> bool:
    d = domain_of(url)
    return any(d == sd or d.endswith("." + sd) for sd in SECONDARY_SOURCE_DOMAINS)


@dataclass
class VerificationReport:
    """Everything the UI needs to show what was kept, fixed, and killed."""

    verified: list[CitationCandidate] = field(default_factory=list)
    suppressed: list[CitationCandidate] = field(default_factory=list)
    quotes_checked: int = 0
    quotes_failed: list[str] = field(default_factory=list)
    sanitised_text: str = ""
    sources_consulted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.verified) + len(self.suppressed)

    @property
    def clean(self) -> bool:
        return not self.suppressed and not self.quotes_failed

    @property
    def integrity_score(self) -> float:
        """Share of asserted authorities that survived verification (0..1)."""
        if self.total == 0:
            return 1.0
        return len(self.verified) / self.total


class CitationVerifier:
    def __init__(
        self,
        registry: KnowledgeBaseRegistry,
        strict: bool = True,
        require_quote_match: bool = True,
        title_threshold: float = TITLE_MATCH_THRESHOLD,
    ):
        self.registry = registry
        self.strict = strict
        self.require_quote_match = require_quote_match
        self.title_threshold = title_threshold

    # ------------------------------------------------------------------
    # Matching helpers
    # ------------------------------------------------------------------
    def _best_match(
        self, cand: CitationCandidate, docs: list[KBDocument]
    ) -> tuple[KBDocument | None, float]:
        """Pick the retrieved document that best matches the asserted citation."""
        best, best_score = None, 0.0
        target_title = cand.title or cand.raw
        for d in docs:
            score = token_set_ratio(target_title, d.title)

            # A matching reporter citation is very strong evidence.
            if cand.reporter and d.citation:
                if normalise(cand.reporter) and normalise(cand.reporter) in normalise(d.citation):
                    score = max(score, 0.95)
            # Year agreement is a weak positive; year conflict is a red flag.
            if cand.year and d.date:
                if cand.year in d.date:
                    score += 0.05
                elif re.search(r"\b(1[89]\d{2}|20\d{2})\b", d.date):
                    found_year = re.search(r"\b(1[89]\d{2}|20\d{2})\b", d.date).group(0)
                    if found_year != cand.year:
                        score -= 0.15
            if score > best_score:
                best, best_score = d, score
        return best, best_score

    def _verify_via_indian_kanoon(self, cand: CitationCandidate) -> bool:
        kb = self.registry.indian_kanoon
        if not kb.available:
            return False

        docs: list[KBDocument] = []

        # 1. If the model supplied an IK docid, resolve it directly.
        m = re.search(r"indiankanoon\.org/doc/(\d+)", cand.source_hint or "")
        if not m:
            m = re.search(r"^\s*(\d{4,})\s*$", cand.source_hint or "")
        if m:
            doc = kb.get_document(m.group(1))
            if doc:
                docs.append(doc)

        # 2. Targeted cite:/title: lookup.
        if not docs:
            docs = kb.find_by_citation(cand.reporter, cand.title)

        # 3. Last resort: plain search on the case name.
        if not docs and cand.title:
            docs = kb.search(cand.title, max_results=8)

        if not docs:
            return False

        best, score = self._best_match(cand, docs)
        if best and score >= self.title_threshold:
            cand.verified = True
            cand.verified_url = best.url
            cand.verified_title = best.title
            cand.verified_court = best.court
            cand.verified_date = best.date
            cand.verified_via = "indiankanoon"
            cand.snippet = best.snippet or best.full_text[:400]
            if best.citation and not cand.reporter:
                cand.reporter = best.citation
            return True

        cand.failure_reason = (
            f"Indian Kanoon returned {len(docs)} result(s) but none matched the asserted "
            f"case name closely enough (best similarity {score:.2f} < {self.title_threshold:.2f})."
        )
        return False

    def _verify_via_local(self, cand: CitationCandidate) -> bool:
        kb = self.registry.local
        if not kb.available:
            return False
        docs = kb.search(cand.title or cand.raw, max_results=8)
        best, score = self._best_match(cand, docs)
        if best and score >= self.title_threshold:
            cand.verified = True
            cand.verified_url = best.url or f"local:{best.doc_id}"
            cand.verified_title = best.title
            cand.verified_court = best.court
            cand.verified_date = best.date
            cand.verified_via = "local_corpus"
            cand.snippet = best.snippet
            return True
        return False

    def _verify_via_web_citations(
        self, cand: CitationCandidate, web_citations: list[dict[str, Any]]
    ) -> bool:
        """
        Use the LLM provider's own grounded search citations.

        A web citation only counts if it sits on a PRIMARY domain. Commentary
        sites (LiveLaw, Bar & Bench, SCC Online blog) are informative but are
        not the report -- they cannot alone validate a citation.
        """
        for c in web_citations or []:
            url = c.get("url", "")
            if not url or not is_primary_domain(url):
                continue
            title = c.get("title", "")
            cited_text = c.get("cited_text", "")
            haystack = f"{title} {cited_text}"

            score = token_set_ratio(cand.title or cand.raw, haystack)
            if cand.reporter and normalise(cand.reporter) in normalise(haystack):
                score = max(score, 0.9)

            if score >= self.title_threshold:
                cand.verified = True
                cand.verified_url = url
                cand.verified_title = title or cand.title
                cand.verified_via = "web_search"
                cand.snippet = cited_text
                return True
        return False

    def _verify_via_digiscr(self, cand: CitationCandidate) -> bool:
        kb = self.registry.digiscr
        if not kb.available or not cand.title:
            return False
        docs = kb.search(cand.title, max_results=6)
        best, score = self._best_match(cand, docs)
        if best and score >= self.title_threshold:
            cand.verified = True
            cand.verified_url = best.url
            cand.verified_title = best.title
            cand.verified_court = "Supreme Court of India"
            cand.verified_via = "digiscr"
            return True
        return False

    # ------------------------------------------------------------------
    # Quote verification
    # ------------------------------------------------------------------
    def _quote_present(self, quote: str, haystacks: list[str]) -> bool:
        nq = normalise(quote)
        if len(nq) < 20:
            return True  # too short to be a meaningful attributed quote
        for hay in haystacks:
            nh = normalise(hay)
            if not nh:
                continue
            if nq in nh:
                return True
            # Sliding-window token overlap for OCR/typographic drift.
            q_tokens = nq.split()
            if len(q_tokens) >= 6:
                window = len(q_tokens)
                h_tokens = nh.split()
                for i in range(0, max(1, len(h_tokens) - window + 1), max(1, window // 2)):
                    seg = " ".join(h_tokens[i : i + window])
                    if token_set_ratio(nq, seg) >= QUOTE_MATCH_THRESHOLD:
                        return True
        return False

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def verify(
        self,
        text: str,
        web_citations: list[dict[str, Any]] | None = None,
        retrieved_docs: list[KBDocument] | None = None,
    ) -> VerificationReport:
        report = VerificationReport()
        web_citations = web_citations or []
        retrieved_docs = retrieved_docs or []

        candidates = extract_all(text)
        replacement_map: dict[str, str] = {}

        for cand in candidates:
            # Try each source, strongest first. Short-circuit on success.
            ok = (
                self._verify_via_indian_kanoon(cand)
                or self._verify_via_local(cand)
                or self._verify_via_web_citations(cand, web_citations)
                or self._verify_via_digiscr(cand)
            )

            # Also accept a match against docs already retrieved this turn.
            if not ok and retrieved_docs:
                best, score = self._best_match(cand, retrieved_docs)
                if best and score >= self.title_threshold:
                    cand.verified = True
                    cand.verified_url = best.url
                    cand.verified_title = best.title
                    cand.verified_court = best.court
                    cand.verified_date = best.date
                    cand.verified_via = best.source
                    cand.snippet = best.snippet
                    ok = True

            if ok:
                report.verified.append(cand)
                if cand.structured:
                    replacement_map[cand.raw] = self._render_verified(cand)
            else:
                if not cand.failure_reason:
                    cand.failure_reason = (
                        "No matching document found in Indian Kanoon, the local corpus, "
                        "Digital SCR, or any whitelisted primary source."
                    )
                report.suppressed.append(cand)
                if cand.structured:
                    replacement_map[cand.raw] = SUPPRESSION_NOTICE if self.strict else cand.label()

        # ---------------- text sanitisation ----------------
        sanitised = strip_structured_markers(text, replacement_map)

        if self.strict:
            # Remove free-text (unstructured) assertions that failed verification.
            for cand in report.suppressed:
                if cand.structured:
                    continue
                pattern = re.escape(cand.raw)
                sanitised = re.sub(pattern, SUPPRESSION_NOTICE, sanitised)

        # ---------------- quote verification ----------------
        if self.require_quote_match:
            haystacks = [d.full_text or d.snippet for d in retrieved_docs]
            haystacks += [c.snippet for c in report.verified if c.snippet]
            haystacks += [c.get("cited_text", "") for c in web_citations]

            kb = self.registry.indian_kanoon
            verified_doc_ids = []
            for cand in report.verified:
                m = re.search(r"indiankanoon\.org/doc/(\d+)", cand.verified_url or "")
                if m:
                    verified_doc_ids.append(m.group(1))

            for q in extract_quotes(sanitised):
                report.quotes_checked += 1
                ok = self._quote_present(q, haystacks)

                # Cost-optimised second pass: /docfragment costs Rs 0.05 versus
                # Rs 0.20 for a full /doc fetch, and it answers precisely the
                # question we are asking -- "does this text appear in this
                # judgment?" Only spend it if the cheap local check failed.
                if not ok and kb.available and verified_doc_ids:
                    probe = " ".join(q.split()[:12])
                    for doc_id in verified_doc_ids[:4]:
                        frag = kb.get_fragment(doc_id, f'"{probe}"')
                        if frag and self._quote_present(q, [frag]):
                            haystacks.append(frag)
                            ok = True
                            break

                if not ok:
                    report.quotes_failed.append(q)
                    if self.strict:
                        for opener, closer in (('"', '"'), ("“", "”")):
                            sanitised = sanitised.replace(
                                f"{opener}{q}{closer}",
                                "*[quotation suppressed — text not found in the cited judgment]*",
                            )

        report.sanitised_text = sanitised
        report.sources_consulted = [kb.name for kb in self.registry.live]

        if not self.registry.indian_kanoon.available:
            report.notes.append(
                "Indian Kanoon API token not configured — verification fell back to grounded "
                "web search only, which is materially weaker. Add INDIAN_KANOON_API_TOKEN."
            )
        if report.suppressed:
            report.notes.append(
                f"{len(report.suppressed)} asserted authority(ies) could not be verified and "
                "were removed from the output."
            )
        return report

    @staticmethod
    def _render_verified(cand: CitationCandidate) -> str:
        """Markdown for a citation that survived verification."""
        title = cand.verified_title or cand.title
        bits = [f"***{title}***"]
        if cand.reporter:
            bits.append(f", {cand.reporter}")
        meta = ", ".join(x for x in (cand.verified_court, cand.verified_date) if x)
        if meta:
            bits.append(f" ({meta})")
        if cand.verified_url:
            bits.append(f" [[source]({cand.verified_url})]")
        return "".join(bits)
