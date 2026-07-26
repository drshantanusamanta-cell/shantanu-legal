"""
Pluggable adapters for authentic Indian legal knowledge bases.

Each adapter implements `search()` and (where possible) `get_document()`,
returning `KBDocument` objects. The verifier treats a citation as real ONLY if
some adapter returns a document that matches it.

Adapters, in order of authority:

  IndianKanoonKB   api.indiankanoon.org      Official API, ~3 crore docs, token auth.
                                             Free Rs 500 dev credit; Rs 10,000/mo free
                                             for verified non-commercial use.
  ECourtsIndiaKB   ecourtsindia.com/api      27.5 crore case records, digitally signed
                                             certified-copy PDFs. API key auth.
  DigiSCRKB        digiscr.sci.gov.in        OFFICIAL Supreme Court Reports. Free, no
                                             API -> resolved via the LLM's web_fetch.
  IndiaCodeKB      indiacode.nic.in          Official bare acts / statutes. Free.
  LocalCorpusKB    your own PDFs             Optional offline vector index.

All network adapters degrade gracefully: if no key is configured they report
`available == False` and the verifier falls back to the next source.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from config import IK_DOCTYPES, get_secret


REQUEST_TIMEOUT = 30


@dataclass
class KBDocument:
    """A real document retrieved from a real database."""

    doc_id: str
    title: str
    url: str
    source: str                       # adapter name
    court: str = ""
    date: str = ""
    citation: str = ""
    snippet: str = ""
    full_text: str = ""
    cites: list[str] = field(default_factory=list)
    cited_by: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "court": self.court,
            "date": self.date,
            "citation": self.citation,
            "snippet": self.snippet[:500],
        }


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return re.sub(r"[ \t]+", " ", text).strip()


class BaseKB:
    name = "base"
    authority = "primary"

    @property
    def available(self) -> bool:
        return False

    def search(self, query: str, **kwargs) -> list[KBDocument]:
        return []

    def get_document(self, doc_id: str) -> KBDocument | None:
        return None


# --------------------------------------------------------------------------
# 1. Indian Kanoon  --  the workhorse
# --------------------------------------------------------------------------
class IndianKanoonKB(BaseKB):
    """
    Official Indian Kanoon API client.

    Endpoints (all POST, per the official docs):
        /search/?formInput=<q>&pagenum=<n>
        /doc/<docid>/
        /docmeta/<docid>/
        /docfragment/<docid>/?formInput=<q>
        /origdoc/<docid>/        (court copy PDF)

    Auth: header  Authorization: Token <token>
    """

    name = "indiankanoon"
    authority = "primary"
    BASE = "https://api.indiankanoon.org"

    # Official tariff, INR per request (api.indiankanoon.org/pricing).
    # Used to pick the cheapest endpoint that answers the question, and to
    # show the user a running spend figure.
    COST = {
        "search": 0.50,
        "origdoc": 0.50,
        "doc": 0.20,
        "docfragment": 0.05,
        "docmeta": 0.02,
    }

    def __init__(self, token: str | None = None, budget_inr: float | None = None):
        self.token = token or get_secret("INDIAN_KANOON_API_TOKEN")
        self._session = requests.Session()
        # Per-session accounting so the UI can show cost in real time.
        self.spend_inr: float = 0.0
        self.call_counts: dict[str, int] = {k: 0 for k in self.COST}
        self._cache: dict[str, Any] = {}

        # Hard spend ceiling per session. Indian Kanoon is PREPAID: when the
        # balance hits zero the API simply stops returning results, which would
        # silently degrade verification. Better to stop deliberately.
        try:
            self.budget_inr = float(
                budget_inr if budget_inr is not None
                else (get_secret("INDIAN_KANOON_SESSION_BUDGET_INR") or 25.0)
            )
        except (TypeError, ValueError):
            self.budget_inr = 25.0
        self.budget_exhausted = False

    @property
    def available(self) -> bool:
        return bool(self.token) and not self.budget_exhausted

    def reset_spend(self) -> None:
        self.spend_inr = 0.0
        self.call_counts = {k: 0 for k in self.COST}
        self.budget_exhausted = False

    def _would_exceed_budget(self, kind: str) -> bool:
        return (self.spend_inr + self.COST.get(kind, 0.0)) > self.budget_inr

    def spend_summary(self) -> dict[str, Any]:
        return {
            "total_inr": round(self.spend_inr, 2),
            "budget_inr": self.budget_inr,
            "remaining_inr": round(max(0.0, self.budget_inr - self.spend_inr), 2),
            "budget_exhausted": self.budget_exhausted,
            "calls": dict(self.call_counts),
            "cached_hits": len(self._cache),
        }

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self.token}",
            "Accept": "application/json",
        }

    @staticmethod
    def _kind(path: str) -> str:
        if path.startswith("/search"):
            return "search"
        if path.startswith("/origdoc"):
            return "origdoc"
        if path.startswith("/docfragment"):
            return "docfragment"
        if path.startswith("/docmeta"):
            return "docmeta"
        return "doc"

    def _post(self, path: str, params: dict | None = None) -> dict | None:
        if not self.token or self.budget_exhausted:
            return None

        # Cache aggressively: every avoided call is real money.
        cache_key = f"{path}::{sorted((params or {}).items())}"
        if cache_key in self._cache:
            return self._cache[cache_key]

        kind_pre = self._kind(path)
        if self._would_exceed_budget(kind_pre):
            self.budget_exhausted = True
            return None

        url = f"{self.BASE}{path}"
        try:
            r = self._session.post(
                url, headers=self._headers(), params=params or {}, timeout=REQUEST_TIMEOUT
            )
            if r.status_code == 403:
                raise PermissionError(
                    "Indian Kanoon rejected the token (403). Check INDIAN_KANOON_API_TOKEN "
                    "and that your prepaid balance is not exhausted."
                )
            r.raise_for_status()
            data = r.json()
        except PermissionError:
            raise
        except Exception:
            return None

        kind = self._kind(path)
        self.call_counts[kind] = self.call_counts.get(kind, 0) + 1
        self.spend_inr += self.COST.get(kind, 0.0)
        self._cache[cache_key] = data
        return data

    def search(
        self,
        query: str,
        court: str = "All courts",
        from_year: int | None = None,
        to_year: int | None = None,
        page: int = 0,
        max_results: int = 10,
        fetch_full_text: bool = False,
    ) -> list[KBDocument]:
        form_input = query
        doctype = IK_DOCTYPES.get(court, "")
        if doctype:
            form_input += f" doctypes:{doctype}"
        if from_year:
            form_input += f" fromdate:1-1-{from_year}"
        if to_year:
            form_input += f" todate:31-12-{to_year}"

        data = self._post("/search/", {"formInput": form_input, "pagenum": page, "maxcites": 10})
        if not data:
            return []

        docs: list[KBDocument] = []
        for d in (data.get("docs") or [])[:max_results]:
            tid = str(d.get("tid", ""))
            doc = KBDocument(
                doc_id=tid,
                title=_strip_html(d.get("title", "")),
                url=f"https://indiankanoon.org/doc/{tid}/",
                source=self.name,
                court=d.get("docsource", ""),
                date=d.get("publishdate", "") or d.get("date", ""),
                citation=_strip_html(d.get("citation", "") or ""),
                snippet=_strip_html(d.get("headline", "")),
                raw=d,
            )
            if fetch_full_text and tid:
                full = self.get_document(tid)
                if full:
                    doc.full_text = full.full_text
                    doc.cites = full.cites
                    doc.cited_by = full.cited_by
            docs.append(doc)
        return docs

    def get_document(self, doc_id: str, max_cites: int = 20, max_cited_by: int = 20) -> KBDocument | None:
        data = self._post(
            f"/doc/{doc_id}/", {"maxcites": max_cites, "maxcitedby": max_cited_by}
        )
        if not data:
            return None
        return KBDocument(
            doc_id=str(doc_id),
            title=_strip_html(data.get("title", "")),
            url=f"https://indiankanoon.org/doc/{doc_id}/",
            source=self.name,
            court=data.get("docsource", ""),
            date=data.get("publishdate", "") or data.get("date", ""),
            citation=_strip_html(data.get("citation", "") or ""),
            full_text=_strip_html(data.get("doc", "")),
            cites=[_strip_html(c.get("title", "")) for c in (data.get("citeList") or [])],
            cited_by=[_strip_html(c.get("title", "")) for c in (data.get("citedbyList") or [])],
            raw=data,
        )

    def get_meta(self, doc_id: str) -> dict | None:
        return self._post(f"/docmeta/{doc_id}/")

    def get_fragment(self, doc_id: str, query: str) -> str:
        """Return the passage of a judgment matching a query -- used for quote checks."""
        data = self._post(f"/docfragment/{doc_id}/", {"formInput": query})
        if not data:
            return ""
        return _strip_html(data.get("headline", ""))

    def find_by_citation(self, reporter_cite: str, title_hint: str = "") -> list[KBDocument]:
        """Targeted lookup using the API's `cite:` and `title:` filters."""
        parts = []
        if reporter_cite:
            parts.append(f"cite: {reporter_cite}")
        if title_hint:
            head = re.split(r"\s+v\.?\s+|\s+vs\.?\s+", title_hint, maxsplit=1)[0]
            head = re.sub(r"[^\w\s]", " ", head).strip()
            if head:
                parts.append(f"title: {head}")
        if not parts:
            return []
        data = self._post("/search/", {"formInput": " ".join(parts), "pagenum": 0})
        if not data:
            return []
        out = []
        for d in (data.get("docs") or [])[:10]:
            tid = str(d.get("tid", ""))
            out.append(
                KBDocument(
                    doc_id=tid,
                    title=_strip_html(d.get("title", "")),
                    url=f"https://indiankanoon.org/doc/{tid}/",
                    source=self.name,
                    court=d.get("docsource", ""),
                    date=d.get("publishdate", "") or "",
                    citation=_strip_html(d.get("citation", "") or ""),
                    snippet=_strip_html(d.get("headline", "")),
                    raw=d,
                )
            )
        return out


# --------------------------------------------------------------------------
# 2. eCourts India  --  certified copies & case status
# --------------------------------------------------------------------------
class ECourtsIndiaKB(BaseKB):
    """
    ecourtsindia.com REST API: case status by CNR, cause lists, order PDFs
    (digitally signed certified true copies), 27.5 crore case records.

    Set ECOURTS_API_KEY and optionally ECOURTS_API_BASE.
    Endpoint names vary by plan, so this adapter is defensive: it probes a few
    common shapes and returns [] rather than raising.
    """

    name = "ecourts"
    authority = "primary"

    def __init__(self, api_key: str | None = None, base: str | None = None):
        self.api_key = api_key or get_secret("ECOURTS_API_KEY")
        self.base = (base or get_secret("ECOURTS_API_BASE") or "https://api.ecourtsindia.com").rstrip("/")
        self._session = requests.Session()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}", "X-API-Key": self.api_key or "",
                "Accept": "application/json"}

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        if not self.available:
            return None
        try:
            r = self._session.get(
                f"{self.base}{path}", headers=self._headers(), params=params or {},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code >= 400:
                return None
            return r.json()
        except Exception:
            return None

    def case_by_cnr(self, cnr: str) -> KBDocument | None:
        """CNR = the 16-char Case Number Record identifier used across eCourts."""
        data = self._get("/v1/case", {"cnr": cnr}) or self._get(f"/case/{cnr}")
        if not data:
            return None
        d = data.get("data", data)
        return KBDocument(
            doc_id=cnr,
            title=d.get("case_title") or d.get("title") or cnr,
            url=d.get("url", f"https://ecourtsindia.com/case/{cnr}"),
            source=self.name,
            court=d.get("court_name") or d.get("court", ""),
            date=d.get("registration_date") or d.get("date", ""),
            snippet=json.dumps(d)[:800],
            raw=d,
        )

    def search(self, query: str, **kwargs) -> list[KBDocument]:
        data = self._get("/v1/search", {"q": query, "limit": kwargs.get("max_results", 10)})
        if not data:
            return []
        rows = data.get("data") or data.get("results") or []
        out = []
        for d in rows[:kwargs.get("max_results", 10)]:
            out.append(
                KBDocument(
                    doc_id=str(d.get("cnr") or d.get("id", "")),
                    title=d.get("case_title") or d.get("title", ""),
                    url=d.get("url", ""),
                    source=self.name,
                    court=d.get("court_name", ""),
                    date=d.get("date", ""),
                    snippet=d.get("summary", "")[:800],
                    raw=d,
                )
            )
        return out


# --------------------------------------------------------------------------
# 3. Digital SCR  --  the OFFICIAL Supreme Court law report
# --------------------------------------------------------------------------
class DigiSCRKB(BaseKB):
    """
    digiscr.sci.gov.in is the Supreme Court's own official report series, free
    and open. It has no documented public API, so we do a polite, low-volume
    GET against its public search page. Best-effort by design: if the markup
    changes we return [] and the verifier falls through to Indian Kanoon.

    For heavy use prefer Indian Kanoon; treat this as a corroborating source
    for SCR citations specifically.
    """

    name = "digiscr"
    authority = "primary"
    BASE = "https://digiscr.sci.gov.in"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._session = requests.Session()
        self._session.headers.update(
            {"User-Agent": "LegalResearchAssistant/1.0 (contact: your-email@example.com)"}
        )

    @property
    def available(self) -> bool:
        return self.enabled

    def search(self, query: str, **kwargs) -> list[KBDocument]:
        if not self.enabled:
            return []
        try:
            r = self._session.get(
                f"{self.BASE}/search", params={"q": query}, timeout=REQUEST_TIMEOUT
            )
            if r.status_code >= 400:
                return []
            html = r.text
        except Exception:
            return []

        out: list[KBDocument] = []
        for m in re.finditer(
            r'href="(/view_judgment\?id=[^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I
        ):
            href, label = m.group(1), _strip_html(m.group(2))
            if not label:
                continue
            out.append(
                KBDocument(
                    doc_id=href,
                    title=label,
                    url=f"{self.BASE}{href}",
                    source=self.name,
                    court="Supreme Court of India",
                )
            )
            if len(out) >= kwargs.get("max_results", 10):
                break
        return out


# --------------------------------------------------------------------------
# 4. India Code  --  official bare acts
# --------------------------------------------------------------------------
class IndiaCodeKB(BaseKB):
    """
    indiacode.nic.in is the Government of India's official repository of Central
    and State Acts. Used to confirm that a statutory provision actually exists
    and is in force -- e.g. that a BNS section number is real.
    """

    name = "indiacode"
    authority = "primary"
    BASE = "https://www.indiacode.nic.in"

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "LegalResearchAssistant/1.0"})

    @property
    def available(self) -> bool:
        return self.enabled

    def search(self, query: str, **kwargs) -> list[KBDocument]:
        if not self.enabled:
            return []
        try:
            r = self._session.get(
                f"{self.BASE}/simple-search",
                params={"query": query},
                timeout=REQUEST_TIMEOUT,
            )
            if r.status_code >= 400:
                return []
            html = r.text
        except Exception:
            return []

        out: list[KBDocument] = []
        for m in re.finditer(r'href="(/handle/[^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
            href, label = m.group(1), _strip_html(m.group(2))
            if not label or len(label) < 4:
                continue
            out.append(
                KBDocument(
                    doc_id=href, title=label, url=f"{self.BASE}{href}",
                    source=self.name, court="Statute",
                )
            )
            if len(out) >= kwargs.get("max_results", 10):
                break
        return out


# --------------------------------------------------------------------------
# 5. Local corpus  --  your own judgment PDFs, offline
# --------------------------------------------------------------------------
class LocalCorpusKB(BaseKB):
    """
    Optional TF-IDF index over judgment PDFs/text you own. No network, no cost,
    and it lets the assistant quote verbatim from your firm's own library.

    Build with `build_index(paths)`; persists to disk as a pickle.
    """

    name = "local_corpus"
    authority = "primary"

    def __init__(self, index_path: str = "corpus_index.pkl"):
        self.index_path = index_path
        self._vectorizer = None
        self._matrix = None
        self._docs: list[KBDocument] = []
        self._load()

    @property
    def available(self) -> bool:
        return bool(self._docs)

    def _load(self) -> None:
        import os
        import pickle

        if not os.path.exists(self.index_path):
            return
        try:
            with open(self.index_path, "rb") as fh:
                blob = pickle.load(fh)
            self._vectorizer = blob["vectorizer"]
            self._matrix = blob["matrix"]
            self._docs = blob["docs"]
        except Exception:
            self._docs = []

    def build_index(self, docs: list[KBDocument]) -> None:
        import pickle

        from sklearn.feature_extraction.text import TfidfVectorizer

        self._docs = docs
        corpus = [f"{d.title}\n{d.full_text}" for d in docs]
        self._vectorizer = TfidfVectorizer(
            stop_words="english", max_features=60000, ngram_range=(1, 2)
        )
        self._matrix = self._vectorizer.fit_transform(corpus)
        with open(self.index_path, "wb") as fh:
            pickle.dump(
                {"vectorizer": self._vectorizer, "matrix": self._matrix, "docs": docs}, fh
            )

    def search(self, query: str, **kwargs) -> list[KBDocument]:
        if not self.available or self._vectorizer is None:
            return []
        from sklearn.metrics.pairwise import cosine_similarity

        qv = self._vectorizer.transform([query])
        sims = cosine_similarity(qv, self._matrix).ravel()
        top = sims.argsort()[::-1][: kwargs.get("max_results", 8)]
        out = []
        for i in top:
            if sims[i] <= 0.01:
                continue
            d = self._docs[int(i)]
            out.append(
                KBDocument(
                    doc_id=d.doc_id, title=d.title, url=d.url, source=self.name,
                    court=d.court, date=d.date, citation=d.citation,
                    snippet=d.full_text[:600], full_text=d.full_text,
                )
            )
        return out


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------
class KnowledgeBaseRegistry:
    """Holds every configured adapter and reports which are live."""

    def __init__(self, enable_scraping_sources: bool = True, local_index_path: str = "corpus_index.pkl"):
        self.indian_kanoon = IndianKanoonKB()
        self.ecourts = ECourtsIndiaKB()
        self.digiscr = DigiSCRKB(enabled=enable_scraping_sources)
        self.indiacode = IndiaCodeKB(enabled=enable_scraping_sources)
        self.local = LocalCorpusKB(index_path=local_index_path)

    @property
    def all(self) -> list[BaseKB]:
        return [self.indian_kanoon, self.ecourts, self.local, self.digiscr, self.indiacode]

    @property
    def live(self) -> list[BaseKB]:
        return [kb for kb in self.all if kb.available]

    def status(self) -> list[dict[str, Any]]:
        labels = {
            "indiankanoon": "Indian Kanoon API",
            "ecourts": "eCourts India API",
            "local_corpus": "Local judgment corpus",
            "digiscr": "Digital SCR (Supreme Court)",
            "indiacode": "India Code (bare acts)",
        }
        return [
            {"name": labels.get(kb.name, kb.name), "key": kb.name, "available": kb.available}
            for kb in self.all
        ]

    def search_case_law(self, query: str, court: str = "All courts", max_results: int = 8,
                        fetch_full_text: bool = False) -> list[KBDocument]:
        """Case-law search across whichever adapters are live, best source first."""
        results: list[KBDocument] = []
        if self.indian_kanoon.available:
            results += self.indian_kanoon.search(
                query, court=court, max_results=max_results, fetch_full_text=fetch_full_text
            )
        if len(results) < max_results and self.local.available:
            results += self.local.search(query, max_results=max_results - len(results))
        if len(results) < max_results and self.digiscr.available:
            results += self.digiscr.search(query, max_results=max_results - len(results))
        return results[:max_results]
