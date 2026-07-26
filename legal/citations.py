"""
Citation extraction and normalisation for Indian legal materials.

Two jobs:
  1. Pull STRUCTURED citations the model was told to emit, in the form
         [[CITE: Title | Reporter | Court | Year | SourceURLorDocID]]
  2. Sweep the free text for citation-SHAPED strings that escaped the
     structured form -- because a hallucinated cite usually escapes.

Anything found by (2) but not backed by (1) is treated as suspect and is
handed to the verifier for hard-blocking.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable


# --------------------------------------------------------------------------
# Reporter patterns for Indian law reports
# --------------------------------------------------------------------------
# Deliberately broad: we would rather over-detect (and verify) than miss a
# fabricated citation.
REPORTER_PATTERNS: list[tuple[str, str]] = [
    # (2017) 10 SCC 1     |  (2017) 10 S.C.C. 1
    ("SCC", r"\(?\b(1[89]\d{2}|20\d{2})\)?\s*\(?\d{0,2}\)?\s*S\.?\s?C\.?\s?C\.?\s+\d+"),
    # AIR 1973 SC 1461    |  AIR 2019 Del 45
    ("AIR", r"\bA\.?\s?I\.?\s?R\.?\s+(1[89]\d{2}|20\d{2})\s+[A-Z][A-Za-z\.]{1,12}\s+\d+"),
    # 2019 SCC OnLine Del 1234
    ("SCC OnLine", r"\b(1[89]\d{2}|20\d{2})\s+S\.?C\.?C\.?\s*OnLine\s+[A-Z][A-Za-z]{1,10}\s+\d+"),
    # (2020) 5 SCR 210
    ("SCR", r"\(?\b(1[89]\d{2}|20\d{2})\)?\s*\(?\d{0,2}\)?\s*S\.?\s?C\.?\s?R\.?\s+\d+"),
    # 2021 (3) BomCR 145 / ALL LJ / CriLJ etc.
    ("CriLJ", r"\b(1[89]\d{2}|20\d{2})\s*\(?\d{0,2}\)?\s*Cri\.?\s?L\.?\s?J\.?\s+\d+"),
    ("BomCR", r"\b(1[89]\d{2}|20\d{2})\s*\(?\d{0,2}\)?\s*Bom\.?\s?C\.?\s?R\.?\s+\d+"),
    ("ITR", r"\b\d{1,3}\s+I\.?\s?T\.?\s?R\.?\s+\d+"),
    ("Comp Cas", r"\b\d{1,3}\s+Comp\.?\s?Cas\.?\s+\d+"),
    ("SCALE", r"\b\(?(1[89]\d{2}|20\d{2})\)?\s*\(?\d{0,2}\)?\s*SCALE\s+\d+"),
    ("JT", r"\b(1[89]\d{2}|20\d{2})\s*\(?\d{0,2}\)?\s*J\.?T\.?\s+\d+"),
]

# "X v. Y" case-name shape. Requires capitalised words either side of v./vs.
CASE_NAME_PATTERN = re.compile(
    r"\b((?:[A-Z][\w&'\.\-]*\s+){0,6}[A-Z][\w&'\.\-]*)"
    r"\s+(?:v\.?|vs\.?|versus)\s+"
    r"((?:[A-Z][\w&'\.\-]*\s+){0,6}[A-Z][\w&'\.\-]*)",
    re.IGNORECASE | re.UNICODE,
)

STRUCTURED_CITE_PATTERN = re.compile(
    r"\[\[\s*CITE\s*:(?P<body>.*?)\]\]",
    re.IGNORECASE | re.DOTALL,
)

# Quoted passages the model attributes to a judgment.
QUOTE_PATTERN = re.compile(
    r"[\"“](?P<quote>[^\"“”]{40,1200})[\"”]",
    re.UNICODE,
)

# Statutory provision references (BNS/IPC/CrPC/BNSS/Evidence/Constitution...).
STATUTE_PATTERN = re.compile(
    r"\b(?:Section|Sec\.?|S\.|u/s|Article|Art\.?)\s*"
    r"(?P<num>\d{1,4}[A-Z]{0,3}(?:\(\w{1,4}\))*)"
    r"(?:\s*(?:of|,)?\s*(?:the\s+)?(?P<act>[A-Z][A-Za-z0-9\s,\.\-]{2,70}?"
    r"(?:Act|Code|Sanhita|Adhiniyam|Constitution)"
    r"(?:\s*,?\s*(?:1[89]\d{2}|20\d{2}))?))?",
    re.UNICODE,
)


def normalise(text: str) -> str:
    """Aggressive normalisation for fuzzy comparison of case names/quotes."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.lower()
    text = re.sub(r"[‘’“”]", "'", text)
    text = re.sub(r"\bversus\b|\bvs\.?\b|\bv\.?\b", " v ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\b(state of|union of india|the|of|and|ors|anr|others|another)\b", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def token_set_ratio(a: str, b: str) -> float:
    """Cheap Jaccard-style similarity on normalised token sets (0..1)."""
    ta, tb = set(normalise(a).split()), set(normalise(b).split())
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb))


@dataclass
class CitationCandidate:
    """One authority the model asserted. Guilty until proven verified."""

    raw: str                       # exact substring in the model output
    title: str = ""                # "Kesavananda Bharati v. State of Kerala"
    reporter: str = ""             # "(1973) 4 SCC 225"
    court: str = ""
    year: str = ""
    source_hint: str = ""          # URL or Indian Kanoon docid the model claimed
    structured: bool = False       # came from [[CITE:...]] not a text sweep
    quotes: list[str] = field(default_factory=list)

    # populated by the verifier
    verified: bool = False
    verified_url: str = ""
    verified_title: str = ""
    verified_court: str = ""
    verified_date: str = ""
    verified_via: str = ""         # "indiankanoon" | "web_search" | "ecourts"
    snippet: str = ""
    quote_ok: bool | None = None
    failure_reason: str = ""

    def key(self) -> str:
        return normalise(f"{self.title} {self.reporter}") or normalise(self.raw)

    def label(self) -> str:
        bits = [b for b in (self.title, self.reporter) if b]
        return " , ".join(bits) if bits else self.raw.strip()


def parse_structured_citations(text: str) -> list[CitationCandidate]:
    """Parse [[CITE: Title | Reporter | Court | Year | Source]] blocks."""
    out: list[CitationCandidate] = []
    for m in STRUCTURED_CITE_PATTERN.finditer(text or ""):
        body = m.group("body")
        parts = [p.strip() for p in body.split("|")]
        parts += [""] * (5 - len(parts))
        out.append(
            CitationCandidate(
                raw=m.group(0),
                title=parts[0],
                reporter=parts[1],
                court=parts[2],
                year=parts[3],
                source_hint=parts[4],
                structured=True,
            )
        )
    return out


def sweep_freetext_citations(text: str) -> list[CitationCandidate]:
    """Find citation-shaped strings not wrapped in [[CITE:]]."""
    text = text or ""
    # Blank out structured blocks so we don't double count.
    masked = STRUCTURED_CITE_PATTERN.sub(" ", text)

    found: dict[str, CitationCandidate] = {}

    # Reporter-style citations
    for name, pat in REPORTER_PATTERNS:
        for m in re.finditer(pat, masked, re.IGNORECASE):
            raw = m.group(0).strip()
            k = normalise(raw)
            if k and k not in found:
                found[k] = CitationCandidate(raw=raw, reporter=raw, structured=False)

    # "A v. B" case names
    for m in CASE_NAME_PATTERN.finditer(masked):
        raw = m.group(0).strip()
        # Filter obvious false positives (e.g. "Plaintiff v. Defendant" generic).
        if len(raw) < 8 or len(raw) > 200:
            continue
        k = normalise(raw)
        if k and k not in found:
            found[k] = CitationCandidate(raw=raw, title=raw, structured=False)

    return list(found.values())


def extract_all(text: str) -> list[CitationCandidate]:
    """Structured citations first, then unclaimed free-text ones."""
    structured = parse_structured_citations(text)
    seen = {c.key() for c in structured}
    extras = [c for c in sweep_freetext_citations(text) if c.key() not in seen]
    return structured + extras


def extract_quotes(text: str) -> list[str]:
    """Long quoted passages that purport to come from a judgment."""
    out: list[str] = []
    for m in QUOTE_PATTERN.finditer(text or ""):
        q = " ".join(m.group("quote").split())
        if len(q) >= 40:
            out.append(q)
    return out


def extract_statutes(text: str) -> list[dict[str, str]]:
    """Statutory provisions referenced, for the statute-check panel."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for m in STATUTE_PATTERN.finditer(text or ""):
        num = (m.group("num") or "").strip()
        act = " ".join((m.group("act") or "").split()).strip(" ,.")
        if not num:
            continue
        key = f"{num}|{act}".lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"provision": num, "act": act, "raw": m.group(0).strip()})
    return out


def strip_structured_markers(text: str, replacement_map: dict[str, str] | None = None) -> str:
    """
    Replace [[CITE:...]] markers with rendered text.

    replacement_map maps the raw marker -> replacement string. Markers with no
    entry are removed entirely (that is how suppression happens).
    """
    replacement_map = replacement_map or {}

    def _sub(m: re.Match) -> str:
        return replacement_map.get(m.group(0), "")

    return STRUCTURED_CITE_PATTERN.sub(_sub, text or "")
