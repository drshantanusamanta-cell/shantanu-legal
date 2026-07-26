"""
Senior Counsel — Indian Legal Research & Drafting Assistant
===========================================================

Streamlit entry point.

Run:
    streamlit run app.py

Every generation passes through the hard-block verifier in legal/verifier.py.
The UI cannot bypass it — that is deliberate.
"""

from __future__ import annotations

import json
from datetime import datetime

import streamlit as st

from config import (
    ANTHROPIC_MODELS,
    APP_ICON,
    APP_TITLE,
    COPYRIGHT_HOLDER,
    COPYRIGHT_LONG,
    COPYRIGHT_NOTICE,
    DISCLAIMER,
    GEMINI_MODELS,
    IK_DOCTYPES,
    AppSettings,
)

st.set_page_config(
    page_title="Senior Counsel — Legal Assistant",
    page_icon=APP_ICON,
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------- auth gate
import auth  # noqa: E402

if not auth.require_login("Senior Counsel"):
    st.stop()

# Imports below the gate so nothing heavy loads for an unauthenticated visitor.
import exporters  # noqa: E402
from engine import EngineResult, LegalEngine  # noqa: E402
from ingest.documents import all_attachments, combined_context, ingest  # noqa: E402
from legal.prompts import DRAFT_TYPES  # noqa: E402


# ---------------------------------------------------------------- styling
st.markdown(
    """
    <style>
      .main .block-container { padding-top: 2rem; max-width: 1280px; }
      h1, h2, h3 { font-family: Georgia, 'Times New Roman', serif; }
      .opinion-body { font-family: Georgia, serif; font-size: 1.02rem; line-height: 1.75;
                      text-align: justify; }
      .verified-pill { display:inline-block; background:#ecfdf5; color:#065f46;
                       border:1px solid #a7f3d0; border-radius:999px;
                       padding:2px 10px; font-size:0.78rem; margin:2px 4px 2px 0; }
      .suppressed-pill { display:inline-block; background:#fef2f2; color:#991b1b;
                         border:1px solid #fecaca; border-radius:999px;
                         padding:2px 10px; font-size:0.78rem; margin:2px 4px 2px 0; }
      .kb-on  { color:#059669; font-weight:600; }
      .kb-off { color:#94a3b8; }
      .stTabs [data-baseweb="tab"] { font-family: Georgia, serif; font-size: 0.98rem; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- state
def _init_state() -> None:
    st.session_state.setdefault("settings", AppSettings())
    st.session_state.setdefault("results", {})        # tab_key -> EngineResult
    st.session_state.setdefault("chat", [])           # research conversation
    st.session_state.setdefault("docs", [])           # ingested documents
    st.session_state.setdefault("graph", None)
    st.session_state.setdefault("matter_name", "")


_init_state()
settings: AppSettings = st.session_state.settings


@st.cache_resource(show_spinner=False)
def _engine_for(sig: str) -> LegalEngine:
    return LegalEngine(AppSettings())


engine = _engine_for("singleton")
engine.settings = settings  # keep the live settings object


# ---------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown(f"### {APP_ICON} Senior Counsel")
    st.caption(f"Signed in as **{auth.current_user()}**")
    auth.render_logout_button()
    st.divider()

    st.markdown("#### Engine")
    prov_status = engine.provider_status()
    available_provs = [p for p in prov_status if p["available"]]

    if not available_provs:
        st.error("No LLM key configured. Add `ANTHROPIC_API_KEY` or `GEMINI_API_KEY`.")
    else:
        labels = {p["key"]: p["name"] for p in prov_status}
        choices = [p["key"] for p in available_provs]
        idx = choices.index(settings.provider) if settings.provider in choices else 0
        settings.provider = st.radio(
            "Provider", choices, index=idx, format_func=lambda k: labels[k],
            horizontal=True,
        )

        if settings.provider == "anthropic":
            settings.anthropic_model = st.selectbox(
                "Model", list(ANTHROPIC_MODELS),
                index=list(ANTHROPIC_MODELS).index(settings.anthropic_model)
                if settings.anthropic_model in ANTHROPIC_MODELS else 1,
                format_func=lambda m: m,
                help="\n".join(f"{k}: {v}" for k, v in ANTHROPIC_MODELS.items()),
            )
        else:
            settings.gemini_model = st.selectbox(
                "Model", list(GEMINI_MODELS),
                index=list(GEMINI_MODELS).index(settings.gemini_model)
                if settings.gemini_model in GEMINI_MODELS else 0,
            )

    for p in prov_status:
        cls = "kb-on" if p["available"] else "kb-off"
        mark = "●" if p["available"] else "○"
        st.markdown(
            f"<span class='{cls}'>{mark} {p['name']}</span>", unsafe_allow_html=True
        )

    st.divider()
    st.markdown("#### Knowledge bases")
    for kb in engine.registry.status():
        cls = "kb-on" if kb["available"] else "kb-off"
        mark = "●" if kb["available"] else "○"
        st.markdown(f"<span class='{cls}'>{mark} {kb['name']}</span>", unsafe_allow_html=True)

    if not engine.registry.indian_kanoon.available:
        st.warning(
            "Indian Kanoon token not set — citation verification is running on grounded "
            "web search alone, which is materially weaker.",
            icon="⚠️",
        )
    else:
        ik = engine.registry.indian_kanoon
        spend = ik.spend_summary()
        used, budget = spend["total_inr"], spend["budget_inr"]
        st.caption(f"Indian Kanoon spend: **₹{used:.2f}** of ₹{budget:.2f} session budget")
        st.progress(min(1.0, used / budget) if budget else 0.0)

        if spend["budget_exhausted"]:
            st.error(
                "Session budget reached — Indian Kanoon calls stopped. Verification has "
                "fallen back to web search. Raise the budget or reset.",
                icon="🛑",
            )
        new_budget = st.number_input(
            "Session budget (₹)", min_value=1.0, max_value=2000.0,
            value=float(budget), step=5.0,
            help="Indian Kanoon is prepaid — when your balance hits zero the API silently "
                 "stops returning results. This cap stops deliberately instead.",
        )
        if abs(new_budget - budget) > 0.001:
            ik.budget_inr = float(new_budget)
            ik.budget_exhausted = ik.spend_inr > new_budget
        if st.button("Reset session spend", use_container_width=True):
            ik.reset_spend()
            st.rerun()

    st.divider()
    st.markdown("#### Verification")
    settings.strict_mode = st.toggle(
        "Hard-block unverified citations", value=settings.strict_mode,
        help="Delete any authority that cannot be matched to a real retrieved document. "
             "Turning this off is not recommended for anything you intend to file.",
    )
    settings.require_quote_match = st.toggle(
        "Verify quoted passages", value=settings.require_quote_match,
        help="Every quotation is checked against the source judgment's actual text.",
    )
    settings.restrict_to_primary = st.toggle(
        "Restrict search to primary sources", value=settings.restrict_to_primary,
        help="Confine web search to courts, Indian Kanoon, India Code and official portals.",
    )
    settings.use_indian_kanoon = st.toggle(
        "Pre-retrieve from Indian Kanoon", value=settings.use_indian_kanoon,
        help="Fetch real judgments before generating, so the model has correct citations "
             "in front of it. Costs ₹0.50 per search.",
    )

    if not settings.strict_mode:
        st.error("Hard-block is OFF. Unverified authorities will be shown.", icon="🚨")

    st.divider()
    st.markdown("#### Scope")
    settings.court_filter = st.selectbox(
        "Court filter", list(IK_DOCTYPES),
        index=list(IK_DOCTYPES).index(settings.court_filter)
        if settings.court_filter in IK_DOCTYPES else 0,
    )
    settings.max_searches = st.slider("Max searches per query", 2, 15, settings.max_searches)
    settings.max_tokens = st.slider("Max output tokens", 2000, 16000, settings.max_tokens, 1000)
    settings.temperature = st.slider("Temperature", 0.0, 1.0, settings.temperature, 0.05,
                                     help="Low values are appropriate for legal work.")

    st.divider()
    st.session_state.matter_name = st.text_input(
        "Matter reference", value=st.session_state.matter_name,
        placeholder="e.g. Sharma v. Verma — arbitration",
    )


# ---------------------------------------------------------------- helpers
IK_BADGE_STYLE = {
    "full":        ("#065f46", "#ecfdf5", "#a7f3d0", "●"),
    "partial":     ("#92400e", "#fffbeb", "#fde68a", "◐"),
    "unused":      ("#475569", "#f8fafc", "#e2e8f0", "○"),
    "unavailable": ("#991b1b", "#fef2f2", "#fecaca", "✕"),
}


def render_ik_badge(result: EngineResult) -> None:
    """Per-result indicator: did Indian Kanoon actually back THIS answer?"""
    ik = result.ik
    fg, bg, border, mark = IK_BADGE_STYLE[ik.level]

    st.markdown(
        f"""
        <div style="display:flex; align-items:center; gap:10px; background:{bg};
                    border:1px solid {border}; border-radius:8px;
                    padding:8px 14px; margin:4px 0 14px 0;">
          <span style="color:{fg}; font-size:1rem; line-height:1;">{mark}</span>
          <div style="line-height:1.45;">
            <span style="color:{fg}; font-weight:600; font-size:0.88rem;">
              {ik.headline}
            </span>
            <div style="color:#64748b; font-size:0.78rem;">{ik.detail}</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_verification_panel(result: EngineResult) -> None:
    """Show precisely what survived verification and what was killed."""
    render_ik_badge(result)

    report = result.report
    if report is None:
        st.info("Verification was skipped for this output.")
        return

    # Per-citation provenance: which source proved which authority.
    if report.verified:
        via_counts: dict[str, int] = {}
        for c in report.verified:
            via_counts[c.verified_via] = via_counts.get(c.verified_via, 0) + 1
        pretty = {
            "indiankanoon": "Indian Kanoon API",
            "web_search": "Grounded web search",
            "digiscr": "Digital SCR",
            "local_corpus": "Local corpus",
            "ecourts": "eCourts",
        }
        st.caption(
            "Verified via — "
            + " · ".join(f"**{pretty.get(k, k)}**: {v}" for k, v in sorted(via_counts.items()))
        )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Authorities asserted", report.total)
    c2.metric("Verified", len(report.verified))
    c3.metric("Suppressed", len(report.suppressed),
              delta=None if not report.suppressed else f"-{len(report.suppressed)}",
              delta_color="inverse")
    c4.metric("Integrity", f"{report.integrity_score * 100:.0f}%")

    if report.suppressed:
        st.error(
            f"**{len(report.suppressed)} authority(ies) could not be verified and were "
            "removed from the text above.** This is the tool working as intended — the "
            "model asserted something it could not support.",
            icon="🚫",
        )
        with st.expander("What was suppressed, and why", expanded=True):
            for c in report.suppressed:
                st.markdown(f"- **`{c.label()}`** — {c.failure_reason}")

    if report.quotes_failed:
        st.warning(
            f"{len(report.quotes_failed)} quoted passage(s) could not be found in the "
            "cited judgment and were removed.",
            icon="❝",
        )
        with st.expander("Failed quotations"):
            for q in report.quotes_failed:
                st.markdown(f"> {q[:400]}…")

    if report.verified:
        st.success(f"{len(report.verified)} authority(ies) verified against primary sources.")
        for c in report.verified:
            with st.container(border=True):
                st.markdown(f"**{c.verified_title or c.title}**")
                meta = " · ".join(
                    x for x in (c.reporter, c.verified_court, c.verified_date) if x
                )
                if meta:
                    st.caption(meta)
                st.caption(f"Verified via `{c.verified_via}`")
                if c.verified_url:
                    st.markdown(f"[Open source document]({c.verified_url})")
                if c.snippet:
                    st.markdown(f"> {c.snippet[:400]}…")

    if report.notes:
        for n in report.notes:
            st.info(n, icon="ℹ️")

    if result.llm and result.llm.searches_performed:
        with st.expander(f"Searches run ({len(result.llm.searches_performed)})"):
            for q in result.llm.searches_performed:
                st.markdown(f"- `{q}`")

    if result.cost_note:
        with st.expander("Cost this run"):
            st.json(result.cost_note)


def render_result(result: EngineResult, title: str, key: str) -> None:
    if not result.ok:
        st.error(result.error)
        return

    tab_out, tab_verify, tab_raw = st.tabs(
        ["📄 Output", "🛡️ Verification", "🔬 Raw model output"]
    )

    with tab_out:
        render_ik_badge(result)
        st.markdown(result.text)

        st.divider()
        name = st.session_state.matter_name or title
        c1, c2, c3 = st.columns(3)
        try:
            c1.download_button(
                "⬇️ Word (.docx)",
                data=exporters.to_docx(title, result.text, result.report),
                file_name=exporters.safe_filename(name, "docx"),
                mime="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                use_container_width=True, key=f"docx_{key}",
            )
        except Exception as exc:  # noqa: BLE001
            c1.caption(f"DOCX unavailable: {exc}")
        try:
            c2.download_button(
                "⬇️ PDF",
                data=exporters.to_pdf(title, result.text, result.report),
                file_name=exporters.safe_filename(name, "pdf"),
                mime="application/pdf",
                use_container_width=True, key=f"pdf_{key}",
            )
        except Exception as exc:  # noqa: BLE001
            c2.caption(f"PDF unavailable: {exc}")
        c3.download_button(
            "⬇️ Markdown",
            data=exporters.to_markdown(title, result.text, result.report),
            file_name=exporters.safe_filename(name, "md"),
            mime="text/markdown",
            use_container_width=True, key=f"md_{key}",
        )

    with tab_verify:
        render_verification_panel(result)

    with tab_raw:
        st.caption(
            "Unsanitised model output, before suppression. Shown so you can see exactly "
            "what was removed and judge the model's reliability yourself."
        )
        st.code(result.raw_text or "", language="markdown")


def uploader(key: str, label: str = "Upload documents"):
    files = st.file_uploader(
        label,
        type=["pdf", "png", "jpg", "jpeg", "webp", "docx", "txt", "md"],
        accept_multiple_files=True,
        key=f"up_{key}",
    )
    docs = []
    if files:
        for f in files:
            with st.spinner(f"Reading {f.name}…"):
                d = ingest(f.getvalue(), f.name)
                docs.append(d)
        for d in docs:
            icon = "📄" if d.has_text else "🖼️"
            st.caption(
                f"{icon} **{d.filename}** — {d.page_count or '?'} pages, "
                f"{len(d.text):,} chars extracted"
                + (" · sent to vision model" if d.needs_vision else "")
            )
            for w in d.warnings:
                st.caption(f"⚠️ {w}")
    return docs


# ---------------------------------------------------------------- header
head_l, head_r = st.columns([3, 1])
with head_l:
    st.markdown(f"# {APP_ICON} Senior Counsel")
    st.caption(
        "Indian legal research, drafting and case analysis — with every citation "
        "verified against a primary source before you see it."
    )
with head_r:
    st.markdown(
        f"""
        <div style="text-align:right; padding-top:1.4rem; line-height:1.5;">
          <div style="font-family:Georgia,serif; font-size:0.92rem; color:#1e293b;">
            {COPYRIGHT_NOTICE}
          </div>
          <div style="font-size:0.76rem; color:#94a3b8; margin-top:2px;">
            Developed by {COPYRIGHT_HOLDER}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
st.divider()

if not engine.any_provider_available():
    st.error(
        "No LLM provider is configured. Add `ANTHROPIC_API_KEY` (recommended) or "
        "`GEMINI_API_KEY` to your secrets, then reload.",
        icon="🔑",
    )
    st.stop()

tabs = st.tabs(
    [
        "🔍 Research & Opinion",
        "✍️ Drafting",
        "📑 Document Analysis",
        "⚖️ Judgment Summary",
        "🔗 Authority Check",
        "🕸️ Citation Map",
    ]
)


# ================================================================ RESEARCH
with tabs[0]:
    st.markdown("### Legal research and written opinion")
    st.caption(
        "State the facts and the question. The assistant retrieves real judgments first, "
        "then reasons — rather than recalling citations from memory."
    )

    q = st.text_area(
        "Facts and question",
        height=180,
        placeholder=(
            "Our client, a Delhi-based private limited company, entered into a supply "
            "agreement dated 12.03.2022 containing an arbitration clause seated in Mumbai. "
            "The counterparty has filed a civil suit in Delhi instead. Can we get the suit "
            "referred to arbitration under s.8, and what is the limitation for that application?"
        ),
        key="research_q",
    )
    docs = uploader("research", "Attach case papers (optional)")

    c1, c2 = st.columns([1, 3])
    go = c1.button("Run research", type="primary", use_container_width=True, key="go_research")
    deep = c2.toggle("Deep mode (more searches, longer opinion)", key="deep_research")

    if go and q.strip():
        if deep:
            settings.max_searches = max(settings.max_searches, 12)
            settings.max_tokens = max(settings.max_tokens, 12000)
        with st.spinner("Retrieving authorities and preparing the opinion…"):
            res = engine.run(
                "research",
                q,
                attachments=all_attachments(docs),
                document_context=combined_context(docs),
                prefetch_query=q,
            )
        st.session_state.results["research"] = res

    if "research" in st.session_state.results:
        render_result(st.session_state.results["research"], "Legal Opinion", "research")


# ================================================================ DRAFTING
with tabs[1]:
    st.markdown("### Drafting")
    st.caption(
        "Produces a filing-ready draft with every unknown marked "
        "`[TO BE INSERTED: …]` — never invented."
    )

    c1, c2 = st.columns([2, 1])
    draft_type = c1.selectbox("Document type", DRAFT_TYPES, key="draft_type")
    forum = c2.text_input("Court / forum", placeholder="e.g. High Court of Delhi at New Delhi",
                          key="draft_forum")

    facts = st.text_area(
        "Facts, parties and instructions",
        height=200,
        placeholder=(
            "Parties: ABC Pvt Ltd (Plaintiff) v. XYZ Enterprises (Defendant).\n"
            "Cheque no. 004521 dated 05.01.2025 for Rs. 12,50,000/- returned unpaid "
            "'funds insufficient' on 09.01.2025. Bank memo received 11.01.2025.\n"
            "Draft the statutory demand notice under s.138."
        ),
        key="draft_facts",
    )
    docs_d = uploader("drafting", "Attach related documents (optional)")

    if st.button("Generate draft", type="primary", key="go_draft"):
        if facts.strip():
            instruction = (
                f"Draft a **{draft_type}**"
                + (f" to be filed before the {forum}" if forum.strip() else "")
                + f".\n\nInstructions and facts:\n{facts}"
            )
            with st.spinner(f"Drafting {draft_type}…"):
                res = engine.run(
                    "drafting",
                    instruction,
                    attachments=all_attachments(docs_d),
                    document_context=combined_context(docs_d),
                    prefetch_query=f"{draft_type} {facts[:200]}",
                )
            st.session_state.results["drafting"] = res
        else:
            st.warning("Enter the facts and instructions first.")

    if "drafting" in st.session_state.results:
        render_result(
            st.session_state.results["drafting"],
            st.session_state.get("draft_type", "Draft"),
            "drafting",
        )


# ================================================================ ANALYSIS
with tabs[2]:
    st.markdown("### Document analysis")
    st.caption(
        "Upload a notice, FIR, contract, order or award. Scanned documents are read by "
        "the vision model — no separate OCR install needed."
    )

    docs_a = uploader("analysis", "Upload the document(s)")
    focus = st.text_input(
        "Specific concern (optional)",
        placeholder="e.g. Is the arbitration clause enforceable? Any limitation problem?",
        key="analysis_focus",
    )

    if st.button("Analyse", type="primary", key="go_analysis"):
        if docs_a:
            instruction = (
                "Analyse the attached document(s) in full."
                + (f"\n\nPay particular attention to: {focus}" if focus.strip() else "")
            )
            with st.spinner("Reading and analysing…"):
                res = engine.run(
                    "analysis",
                    instruction,
                    attachments=all_attachments(docs_a),
                    document_context=combined_context(docs_a),
                    prefetch_query=focus or "",
                )
            st.session_state.results["analysis"] = res
        else:
            st.warning("Upload at least one document.")

    if "analysis" in st.session_state.results:
        render_result(st.session_state.results["analysis"], "Document Analysis", "analysis")


# ================================================================ SUMMARY
with tabs[3]:
    st.markdown("### Judgment summary")
    st.caption(
        "Paste a citation or upload the judgment. Produces a practitioner's headnote with "
        "ratio, obiter and quotable paragraphs — every quotation checked against the source."
    )

    cite = st.text_input(
        "Case citation or name",
        placeholder="e.g. Vidya Drolia v. Durga Trading Corporation, (2021) 2 SCC 1",
        key="summary_cite",
    )
    docs_s = uploader("summary", "…or upload the judgment PDF")

    if st.button("Summarise", type="primary", key="go_summary"):
        if cite.strip() or docs_s:
            instruction = (
                f"Prepare a full practitioner's summary of: {cite}"
                if cite.strip()
                else "Prepare a full practitioner's summary of the attached judgment."
            )
            with st.spinner("Retrieving and summarising the judgment…"):
                res = engine.run(
                    "summary",
                    instruction,
                    attachments=all_attachments(docs_s),
                    document_context=combined_context(docs_s),
                    prefetch_query=cite,
                )
            st.session_state.results["summary"] = res
        else:
            st.warning("Enter a citation or upload the judgment.")

    if "summary" in st.session_state.results:
        render_result(st.session_state.results["summary"], "Judgment Summary", "summary")


# ================================================================ AUTHORITY
with tabs[4]:
    st.markdown("### Authority check — is this still good law?")
    st.caption(
        "Checks later treatment: overruled, doubted, referred to a larger Bench, or "
        "superseded by statute. Reports UNVERIFIED rather than guessing."
    )

    cases = st.text_area(
        "Case(s) to check — one per line",
        height=130,
        placeholder=(
            "Kesavananda Bharati v. State of Kerala, (1973) 4 SCC 225\n"
            "Bhatia International v. Bulk Trading SA, (2002) 4 SCC 105"
        ),
        key="authority_cases",
    )

    if st.button("Check status", type="primary", key="go_authority"):
        if cases.strip():
            with st.spinner("Checking subsequent history…"):
                res = engine.run(
                    "authority",
                    f"Determine the current standing of the following authority(ies):\n{cases}",
                    prefetch_query=cases.split("\n")[0],
                )
            st.session_state.results["authority"] = res
        else:
            st.warning("Enter at least one case.")

    if "authority" in st.session_state.results:
        render_result(st.session_state.results["authority"], "Authority Check", "authority")


# ================================================================ MAP
with tabs[5]:
    st.markdown("### Citation map")
    st.caption("A precedent graph built only from authorities verified in this session.")

    available = {
        k: v for k, v in st.session_state.results.items()
        if v.ok and v.report and v.report.verified
    }

    if not available:
        st.info(
            "Run a research query, judgment summary or authority check first — the map is "
            "built from verified authorities, so there is nothing to plot yet."
        )
    else:
        src = st.selectbox(
            "Build from", list(available),
            format_func=lambda k: k.replace("_", " ").title(), key="map_src",
        )
        if st.button("Build map", type="primary", key="go_map"):
            base = available[src]
            ctx = base.text + "\n\nVERIFIED AUTHORITIES:\n" + "\n".join(
                f"- {c.verified_title or c.title} | {c.reporter} | {c.verified_court} "
                f"| {c.verified_date} | {c.verified_url}"
                for c in base.report.verified
            )
            with st.spinner("Mapping precedent relationships…"):
                st.session_state.graph = engine.citation_map(ctx)

        graph = st.session_state.graph
        if graph and graph.get("nodes"):
            try:
                lines = ["graph LR"]
                ids = {}
                for i, n in enumerate(graph["nodes"]):
                    nid = f"N{i}"
                    ids[n.get("id", f"n{i}")] = nid
                    label = str(n.get("label") or n.get("id") or "?")[:40].replace('"', "'")
                    shape = f'{nid}(["{label}"])' if n.get("type") == "main" else f'{nid}["{label}"]'
                    lines.append(f"    {shape}")
                style = {
                    "supports": "-->|supports|",
                    "distinguishes": "-.->|distinguishes|",
                    "overrules": "==>|overrules|",
                    "binds": "==>|binds|",
                    "refers": "-.->|refers|",
                }
                for l in graph.get("links", []):
                    s, t = ids.get(l.get("source")), ids.get(l.get("target"))
                    if s and t:
                        lines.append(f"    {s} {style.get(l.get('relation'), '-->')} {t}")
                st.code("\n".join(lines), language="mermaid")
            except Exception:
                pass

            for n in graph["nodes"]:
                if n.get("type") == "main":
                    continue
                with st.container(border=True):
                    st.markdown(f"**{n.get('label') or n.get('id')}**")
                    meta = " · ".join(str(n.get(k, "")) for k in ("court", "year") if n.get(k))
                    if meta:
                        st.caption(meta)
                    if n.get("summary"):
                        st.write(n["summary"])
                    if n.get("url"):
                        st.markdown(f"[Open judgment]({n['url']})")

            st.download_button(
                "⬇️ Graph JSON",
                data=json.dumps(graph, indent=2).encode(),
                file_name=exporters.safe_filename("citation_map", "json"),
                mime="application/json",
            )
        elif graph is not None:
            st.warning("No verified relationships could be mapped.")


# ---------------------------------------------------------------- footer
st.divider()
st.caption(DISCLAIMER)
st.caption(
    f"Session started {datetime.now().strftime('%d %B %Y')} · "
    "Hard-block verification "
    + ("**ON**" if settings.strict_mode else "**OFF — review every citation manually**")
)

st.markdown(
    f"""
    <div style="text-align:center; margin-top:2rem; padding:1.2rem 1rem;
                border-top:1px solid #e2e8f0;">
      <div style="font-family:Georgia,serif; font-size:1rem; color:#1e293b;
                  font-weight:600; letter-spacing:0.02em;">
        {COPYRIGHT_NOTICE}
      </div>
      <div style="font-size:0.8rem; color:#64748b; margin-top:6px; max-width:640px;
                  margin-left:auto; margin-right:auto; line-height:1.6;">
        {COPYRIGHT_LONG}
      </div>
      <div style="font-size:0.74rem; color:#94a3b8; margin-top:10px;">
        Senior Counsel — Indian Legal Research &amp; Drafting Assistant ·
        Designed and developed by {COPYRIGHT_HOLDER}
      </div>
    </div>
    """,
    unsafe_allow_html=True,
)
