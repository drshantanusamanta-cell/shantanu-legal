"""
Central configuration and secret loading.

Secrets resolve in this order (first hit wins):
    1. st.secrets            -> Streamlit Community Cloud / local .streamlit/secrets.toml
    2. os.environ            -> .env file or real environment variables
    3. default               -> usually None

This means the SAME code runs locally (.env) and on Streamlit Cloud (Secrets UI)
with no changes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

try:
    import streamlit as st
except Exception:  # pragma: no cover - allows importing config outside Streamlit
    st = None  # type: ignore

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


# --------------------------------------------------------------------------
# Secret plumbing
# --------------------------------------------------------------------------
def get_secret(name: str, default: Any = None) -> Any:
    """Fetch a secret from Streamlit secrets, then env, then default."""
    if st is not None:
        try:
            if name in st.secrets:
                val = st.secrets[name]
                if val not in ("", None):
                    return val
        except Exception:
            # st.secrets raises if no secrets.toml exists at all
            pass
    val = os.environ.get(name)
    if val not in ("", None):
        return val
    return default


def has_secret(name: str) -> bool:
    return bool(get_secret(name))


# --------------------------------------------------------------------------
# Provider / model registry
# --------------------------------------------------------------------------
ANTHROPIC_MODELS = {
    "claude-opus-5": "Claude Opus 5 — deepest reasoning, best for opinions & long drafts",
    "claude-sonnet-5": "Claude Sonnet 5 — balanced speed/quality (recommended default)",
    "claude-haiku-4-5-20251001": "Claude Haiku 4.5 — fast & cheap, for extraction/triage",
}

GEMINI_MODELS = {
    "gemini-2.5-pro": "Gemini 2.5 Pro — strong long-context reasoning",
    "gemini-2.5-flash": "Gemini 2.5 Flash — fast & cheap",
}

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5"
DEFAULT_GEMINI_MODEL = "gemini-2.5-pro"

# Anthropic server-tool versions (see platform.claude.com tool reference).
WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
WEB_FETCH_TOOL_TYPE = "web_fetch_20250910"


# --------------------------------------------------------------------------
# Authority whitelist  --  THE TRUST BOUNDARY
# --------------------------------------------------------------------------
# A citation is only ever treated as authority if it was retrieved from one of
# these domains (or from the Indian Kanoon API). Everything else is suppressed.
# Ordered roughly by authoritativeness.
PRIMARY_SOURCE_DOMAINS: list[str] = [
    # Judgments
    "indiankanoon.org",
    "api.indiankanoon.org",
    "main.sci.gov.in",          # Supreme Court of India
    "digiscr.sci.gov.in",       # Digital SCR (official reports)
    "judgments.ecourts.gov.in",  # eCourts judgment portal
    "ecourts.gov.in",
    "phhc.gov.in",
    "delhihighcourt.nic.in",
    "bombayhighcourt.nic.in",
    "calcuttahighcourt.gov.in",
    "hcmadras.tn.gov.in",
    "allahabadhighcourt.in",
    "karnatakajudiciary.kar.nic.in",
    "keralahighcourt.nic.in",
    "gujarathighcourt.nic.in",
    "hcraj.nic.in",
    "patnahighcourt.gov.in",
    "highcourtchd.gov.in",
    # Bare acts / statutes / subordinate legislation
    "indiacode.nic.in",
    "legislative.gov.in",
    "egazette.gov.in",
    "egazette.nic.in",
    "prsindia.org",
    "lawmin.gov.in",
    "mha.gov.in",
    # Tribunals & regulators
    "nclat.nic.in",
    "ibbi.gov.in",
    "sebi.gov.in",
    "rbi.org.in",
    "cci.gov.in",
    "incometaxindia.gov.in",
    "gst.gov.in",
    "cbic.gov.in",
    "tdsat.gov.in",
    "greentribunal.gov.in",
]

# Domains that look legal but are commentary/aggregators, NOT primary authority.
# Allowed for context but can never satisfy the hard-block verifier alone.
SECONDARY_SOURCE_DOMAINS: list[str] = [
    "livelaw.in",
    "barandbench.com",
    "scconline.com",
    "casemine.com",
    "manupatra.com",
    "lawbeat.in",
    "theleaflet.in",
]


@dataclass
class AppSettings:
    """Runtime settings, mutated by the sidebar."""

    provider: str = "anthropic"          # "anthropic" | "gemini"
    anthropic_model: str = DEFAULT_ANTHROPIC_MODEL
    gemini_model: str = DEFAULT_GEMINI_MODEL
    max_tokens: int = 8000
    temperature: float = 0.2             # low: legal work rewards determinism
    max_searches: int = 8
    strict_mode: bool = True             # HARD BLOCK unverified citations
    require_quote_match: bool = True     # quoted passages must exist in source
    restrict_to_primary: bool = True     # confine search to whitelist
    use_indian_kanoon: bool = True
    court_filter: str = "All courts"
    user_location_city: str = "New Delhi"
    user_location_region: str = "Delhi"

    @property
    def model(self) -> str:
        return self.anthropic_model if self.provider == "anthropic" else self.gemini_model


# --------------------------------------------------------------------------
# Indian Kanoon doctype filters (from the official API docs)
# --------------------------------------------------------------------------
IK_DOCTYPES = {
    "All courts": "",
    "Supreme Court": "supremecourt",
    "All High Courts": "highcourts",
    "Delhi HC": "delhi",
    "Bombay HC": "bombay",
    "Calcutta HC": "kolkata",
    "Madras HC": "chennai",
    "Allahabad HC": "allahabad",
    "Karnataka HC": "karnataka",
    "Kerala HC": "kerala",
    "Gujarat HC": "gujarat",
    "Punjab & Haryana HC": "punjab",
    "Judgments (SC + HC + District)": "judgments",
    "Tribunals": "tribunals",
    "Central Acts & Rules": "laws",
    "NCLAT / Insolvency": "nclat",
    "Income Tax (ITAT)": "itat",
    "Consumer": "consumer",
    "SEBI (SAT)": "sebisat",
    "Competition (CCI)": "cci",
    "NGT": "greentribunal",
}


APP_TITLE = "Senior Counsel — Indian Legal Research & Drafting Assistant"
APP_ICON = "⚖️"

# --------------------------------------------------------------------------
# Attribution
# --------------------------------------------------------------------------
COPYRIGHT_HOLDER = "Dr Shantanu Samanta"
COPYRIGHT_YEAR = "2026"
COPYRIGHT_NOTICE = f"© {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER}. All rights reserved."
COPYRIGHT_LONG = (
    f"© {COPYRIGHT_YEAR} {COPYRIGHT_HOLDER}. All rights reserved. "
    "Unauthorised reproduction, redistribution or commercial exploitation of this "
    "application or its output is prohibited."
)

DISCLAIMER = (
    "This tool produces **research assistance and drafts**, not legal advice, and does "
    "not create an advocate–client relationship. Every citation must be independently "
    "verified against the certified report before filing. AI output can be wrong even "
    "when it is confident."
)
