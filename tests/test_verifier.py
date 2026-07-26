"""
Tests for the hard-block verifier.

The critical properties under test:

  1. A fabricated citation is SUPPRESSED (removed from the output text).
  2. A real citation corroborated by a primary-domain source is KEPT.
  3. A citation "verified" only by a commentary site is SUPPRESSED — LiveLaw
     reporting a case is not the same as the report of the case.
  4. The app still verifies with NO Indian Kanoon token (degraded, not broken).
  5. An invented quotation is stripped even when its case is genuine.

Run:  python -m pytest tests/ -v      (or: python tests/test_verifier.py)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from legal.citations import (  # noqa: E402
    extract_all,
    extract_quotes,
    extract_statutes,
    normalise,
    parse_structured_citations,
    sweep_freetext_citations,
    token_set_ratio,
)
from legal.knowledge_bases import KBDocument, KnowledgeBaseRegistry  # noqa: E402
from legal.verifier import (  # noqa: E402
    SUPPRESSION_NOTICE,
    CitationVerifier,
    is_primary_domain,
    is_secondary_domain,
)


# ---------------------------------------------------------------- fixtures
def offline_registry() -> KnowledgeBaseRegistry:
    """A registry with every network source disabled — the worst case."""
    reg = KnowledgeBaseRegistry(enable_scraping_sources=False)
    # `token` / `api_key` are read-only properties (resolved fresh from secrets
    # on every access, so Streamlit's cached engine picks up a key added after
    # startup). To force "no token" in tests, override the private backing
    # field instead of the property.
    reg.indian_kanoon._token_override = ""
    reg.ecourts._api_key_override = ""
    reg.digiscr.enabled = False
    reg.indiacode.enabled = False
    reg.local._docs = []
    return reg


REAL_CASE = "Kesavananda Bharati v. State of Kerala"
REAL_URL = "https://indiankanoon.org/doc/257876/"
FAKE_CASE = "Ramesh Chandra Textiles v. Union of India"


# ---------------------------------------------------------------- unit: parsing
def test_normalise_and_similarity():
    assert normalise("State of Kerala") != ""
    a = "Kesavananda Bharati v. State of Kerala"
    b = "Kesavananda Bharati  vs.  State Of Kerala And Anr"
    assert token_set_ratio(a, b) > 0.8, "Same case written differently must match"
    assert token_set_ratio(a, "Maneka Gandhi v. Union of India") < 0.5


def test_parse_structured_citation():
    text = (
        "The basic structure doctrine was laid down in "
        f"[[CITE: {REAL_CASE} | (1973) 4 SCC 225 | Supreme Court of India | 1973 | {REAL_URL}]]."
    )
    cites = parse_structured_citations(text)
    assert len(cites) == 1
    c = cites[0]
    assert c.title == REAL_CASE
    assert c.reporter == "(1973) 4 SCC 225"
    assert c.year == "1973"
    assert c.source_hint == REAL_URL
    assert c.structured is True


def test_sweep_catches_unstructured_citations():
    text = (
        "As held in Maneka Gandhi v. Union of India, AIR 1978 SC 597, the procedure "
        "must be fair. See also (2021) 2 SCC 1."
    )
    found = sweep_freetext_citations(text)
    raws = " ".join(c.raw for c in found)
    assert "Maneka Gandhi" in raws, "Case-name sweep failed"
    assert "AIR 1978 SC 597" in raws or "1978 SC 597" in raws, "AIR pattern failed"


def test_extract_quotes_and_statutes():
    text = (
        'The Court observed that "the Constitution is not a mere lawyers document, '
        'it is a vehicle of life and its spirit is always the spirit of the age". '
        "This engages Section 302 of the Indian Penal Code, 1860 and Article 21."
    )
    quotes = extract_quotes(text)
    assert len(quotes) == 1 and "vehicle of life" in quotes[0]

    stats = extract_statutes(text)
    provisions = {s["provision"] for s in stats}
    assert "302" in provisions and "21" in provisions


# ---------------------------------------------------------------- unit: domains
def test_domain_classification():
    assert is_primary_domain("https://indiankanoon.org/doc/257876/")
    assert is_primary_domain("https://main.sci.gov.in/judgment/x.pdf")
    assert is_primary_domain("https://www.indiacode.nic.in/handle/1234")
    assert not is_primary_domain("https://www.livelaw.in/top-stories/xyz")
    assert is_secondary_domain("https://www.livelaw.in/top-stories/xyz")
    assert not is_primary_domain("https://random-blog.example.com/case")


# ---------------------------------------------------------------- HARD BLOCK
def test_fabricated_citation_is_suppressed():
    """The headline guarantee: an unverifiable authority never reaches the user."""
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True, require_quote_match=False)

    text = (
        "The petition is maintainable. See "
        f"[[CITE: {FAKE_CASE} | (2019) 7 SCC 441 | Supreme Court of India | 2019 | "
        "https://indiankanoon.org/doc/999999999/]] which is squarely on point."
    )
    report = v.verify(text, web_citations=[], retrieved_docs=[])

    assert len(report.suppressed) == 1
    assert len(report.verified) == 0
    assert FAKE_CASE not in report.sanitised_text, "Fabricated case leaked into output!"
    assert SUPPRESSION_NOTICE in report.sanitised_text
    assert report.integrity_score == 0.0
    assert not report.clean


def test_verified_citation_is_kept_with_real_link():
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True, require_quote_match=False)

    text = (
        "The basic structure doctrine was settled in "
        f"[[CITE: {REAL_CASE} | (1973) 4 SCC 225 | Supreme Court of India | 1973 | {REAL_URL}]]."
    )
    # Simulate the provider's grounded search returning a primary-domain hit.
    web_citations = [
        {
            "url": REAL_URL,
            "title": "Kesavananda Bharati vs State Of Kerala And Anr",
            "cited_text": "the basic structure of the Constitution cannot be abrogated",
            "source": "web_search_result",
        }
    ]
    report = v.verify(text, web_citations=web_citations, retrieved_docs=[])

    assert len(report.verified) == 1, f"Expected 1 verified, got {report.suppressed}"
    assert len(report.suppressed) == 0
    assert "Kesavananda" in report.sanitised_text
    assert REAL_URL in report.sanitised_text, "Verified cite must carry its source link"
    assert report.integrity_score == 1.0


def test_commentary_site_alone_cannot_verify():
    """LiveLaw reporting a case is not the report of the case."""
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True, require_quote_match=False)

    text = f"[[CITE: {REAL_CASE} | (1973) 4 SCC 225 | Supreme Court | 1973 | x]]"
    web_citations = [
        {
            "url": "https://www.livelaw.in/top-stories/kesavananda-bharati-basic-structure",
            "title": "Kesavananda Bharati v. State of Kerala explained",
            "cited_text": "the basic structure doctrine",
            "source": "web_search_result",
        }
    ]
    report = v.verify(text, web_citations=web_citations, retrieved_docs=[])
    assert len(report.suppressed) == 1, "Secondary source must not satisfy verification"


def test_works_without_indian_kanoon_token():
    """
    Degraded, not broken.

    With no IK token the app must still verify citations that the LLM's own
    grounded search corroborates on a primary domain, and must warn the user
    that verification is weaker.
    """
    reg = offline_registry()
    assert not reg.indian_kanoon.available, "Fixture should have no IK token"

    v = CitationVerifier(reg, strict=True, require_quote_match=False)
    text = f"[[CITE: {REAL_CASE} | (1973) 4 SCC 225 | SC | 1973 | {REAL_URL}]]"
    report = v.verify(
        text,
        web_citations=[{
            "url": REAL_URL,
            "title": "Kesavananda Bharati vs State Of Kerala And Anr",
            "cited_text": "basic structure",
            "source": "web_search_result",
        }],
    )

    assert len(report.verified) == 1, "Must still verify via grounded web search"
    assert any("Indian Kanoon" in n for n in report.notes), \
        "User must be warned that verification is degraded"


def test_retrieved_docs_can_verify():
    """A judgment pre-retrieved this turn is itself sufficient proof."""
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True, require_quote_match=False)

    doc = KBDocument(
        doc_id="257876",
        title="Kesavananda Bharati vs State Of Kerala And Anr",
        url=REAL_URL,
        source="indiankanoon",
        court="Supreme Court of India",
        date="24 April, 1973",
        citation="(1973) 4 SCC 225",
        full_text="the basic structure of the Constitution cannot be abrogated even by an amendment",
    )
    text = f"[[CITE: {REAL_CASE} | (1973) 4 SCC 225 | SC | 1973 | ]]"
    report = v.verify(text, web_citations=[], retrieved_docs=[doc])

    assert len(report.verified) == 1
    assert report.verified[0].verified_url == REAL_URL


def test_invented_quote_is_stripped_even_for_real_case():
    """The case is genuine; the quotation is not. The quote must still die."""
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True, require_quote_match=True)

    doc = KBDocument(
        doc_id="257876",
        title="Kesavananda Bharati vs State Of Kerala And Anr",
        url=REAL_URL,
        source="indiankanoon",
        citation="(1973) 4 SCC 225",
        full_text=(
            "The basic structure of the Constitution cannot be abrogated even by "
            "an amendment made under Article 368 of the Constitution."
        ),
    )
    invented = (
        "every citizen shall possess an inviolable right to unlimited compensation "
        "for any administrative inconvenience whatsoever"
    )
    text = (
        f"[[CITE: {REAL_CASE} | (1973) 4 SCC 225 | SC | 1973 | {REAL_URL}]] "
        f'The Court held that "{invented}".'
    )
    report = v.verify(text, web_citations=[], retrieved_docs=[doc])

    assert len(report.verified) == 1, "The case itself is real and should verify"
    assert len(report.quotes_failed) == 1, "The invented quote should fail"
    assert invented not in report.sanitised_text, "Invented quotation leaked!"
    assert "quotation suppressed" in report.sanitised_text


def test_genuine_quote_survives():
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True, require_quote_match=True)

    genuine = "The basic structure of the Constitution cannot be abrogated"
    doc = KBDocument(
        doc_id="257876",
        title="Kesavananda Bharati vs State Of Kerala And Anr",
        url=REAL_URL, source="indiankanoon", citation="(1973) 4 SCC 225",
        full_text=f"{genuine} even by an amendment made under Article 368.",
    )
    text = (
        f"[[CITE: {REAL_CASE} | (1973) 4 SCC 225 | SC | 1973 | {REAL_URL}]] "
        f'The Court held that "{genuine}".'
    )
    report = v.verify(text, web_citations=[], retrieved_docs=[doc])

    assert not report.quotes_failed, f"Genuine quote was wrongly stripped: {report.quotes_failed}"
    assert genuine in report.sanitised_text


def test_permissive_mode_keeps_but_flags():
    """With strict=False the citation survives, but is still reported as unverified."""
    reg = offline_registry()
    v = CitationVerifier(reg, strict=False, require_quote_match=False)

    text = f"[[CITE: {FAKE_CASE} | (2019) 7 SCC 441 | SC | 2019 | x]]"
    report = v.verify(text, web_citations=[])

    assert len(report.suppressed) == 1, "Must still be REPORTED as unverified"
    assert FAKE_CASE in report.sanitised_text, "Permissive mode should retain the text"


def test_mixed_output_partial_suppression():
    """Real and fake in one answer: keep the real, kill the fake."""
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True, require_quote_match=False)

    text = (
        f"First, [[CITE: {REAL_CASE} | (1973) 4 SCC 225 | SC | 1973 | {REAL_URL}]] applies. "
        f"Second, [[CITE: {FAKE_CASE} | (2019) 7 SCC 441 | SC | 2019 | x]] also applies."
    )
    report = v.verify(
        text,
        web_citations=[{
            "url": REAL_URL,
            "title": "Kesavananda Bharati vs State Of Kerala And Anr",
            "cited_text": "basic structure", "source": "web_search_result",
        }],
    )
    assert len(report.verified) == 1
    assert len(report.suppressed) == 1
    assert "Kesavananda" in report.sanitised_text
    assert FAKE_CASE not in report.sanitised_text
    assert 0.0 < report.integrity_score < 1.0


def test_empty_input_is_safe():
    reg = offline_registry()
    v = CitationVerifier(reg, strict=True)
    report = v.verify("")
    assert report.total == 0
    assert report.integrity_score == 1.0
    assert report.clean


# ---------------------------------------------------------------- IK indicator
def test_ik_status_levels():
    """The per-result Indian Kanoon badge must report the truth in each state."""
    from engine import IKStatus

    # No token at all.
    s = IKStatus(configured=False)
    assert s.level == "unavailable" and not s.used
    assert "No API token" in s.detail

    # Token present, but this answer needed no lookup.
    s = IKStatus(configured=True)
    assert s.level == "unused" and not s.used

    # Budget ran out before any call.
    s = IKStatus(configured=True, budget_exhausted=True)
    assert s.level == "unavailable"
    assert "budget" in s.detail.lower()

    # Every verified citation came from Indian Kanoon.
    s = IKStatus(configured=True, calls_this_run=4, spend_this_run_inr=1.6,
                 prefetch_hits=3, citations_verified_total=3,
                 citations_verified_via_ik=3)
    assert s.level == "full" and s.used
    assert "3/3" in s.detail and "1.60" in s.detail

    # Mixed provenance -> partial, not full.
    s = IKStatus(configured=True, calls_this_run=2, spend_this_run_inr=1.0,
                 citations_verified_total=4, citations_verified_via_ik=1)
    assert s.level == "partial"
    assert "1/4" in s.detail


def test_ik_budget_stops_calls():
    """The prepaid budget guard must halt calls rather than silently overspend."""
    from legal.knowledge_bases import IndianKanoonKB

    kb = IndianKanoonKB(token="dummy-token", budget_inr=1.0)
    assert kb.available

    kb.spend_inr = 0.60                              # simulate prior spend
    assert kb._would_exceed_budget("search")         # 0.60 + 0.50 = 1.10 > 1.00
    assert not kb._would_exceed_budget("docmeta")    # 0.60 + 0.02 = 0.62 <= 1.00
    assert not kb._would_exceed_budget("docfragment")  # 0.65 <= 1.00

    summary = kb.spend_summary()
    assert summary["budget_inr"] == 1.0
    assert abs(summary["remaining_inr"] - 0.40) < 1e-9

    # Once the ceiling trips, the adapter must report itself unavailable so the
    # verifier falls through to web search instead of silently returning nothing.
    kb.budget_exhausted = True
    assert not kb.available


def test_ik_cost_table_matches_published_tariff():
    """Guards against silent drift from api.indiankanoon.org/pricing."""
    from legal.knowledge_bases import IndianKanoonKB

    assert IndianKanoonKB.COST == {
        "search": 0.50, "origdoc": 0.50, "doc": 0.20,
        "docfragment": 0.05, "docmeta": 0.02,
    }


# ---------------------------------------------------------------- runner
if __name__ == "__main__":
    import traceback

    fns = [(n, o) for n, o in sorted(globals().items())
           if n.startswith("test_") and callable(o)]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except AssertionError as exc:
            print(f"  FAIL  {name}\n        {exc}")
            failed += 1
        except Exception:
            print(f"  ERROR {name}")
            traceback.print_exc()
            failed += 1

    print(f"\n{passed} passed, {failed} failed, {len(fns)} total")
    sys.exit(1 if failed else 0)
