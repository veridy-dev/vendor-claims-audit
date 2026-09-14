#!/usr/bin/env python3
"""Vendor Claims Audit — public-claim screening report."""

from __future__ import annotations

import html
import json
import re
from datetime import date
from typing import Any

import httpx
import streamlit as st

XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
DEFAULT_MODEL = "grok-4.6"
STATUSES = (
    "Substantiated",
    "Unsubstantiated",
    "Needs Substantiation",
    "Inconsistent",
    "Contradicted",
)

SYSTEM_PROMPT = """You are a rigorous, skeptical vendor-claims auditor.
You specialize in ITAD (IT Asset Disposition) companies, but you can audit any firm's public claims.

Method:
1. Use web search to find the company's website, About/legal pages, sustainability pages, security/destruction pages, press releases, LinkedIn company page, and relevant certification registries.
   For ITAD, check NAID AAA / i-SIGMA, R2 / R2v3 / SERI, e-Stewards, ISO 9001 / 14001 / 45001, SOC 2 when claimed.
2. Extract 4 to 7 specific, checkable public claims. Priority order:
   a) environmental / sustainability (zero landfill, 100% recycled, carbon-neutral)
   b) data security / media destruction method and standard
   c) certification claims — distinguish company-level marketing from a named facility listing
   d) legal / liability / indemnification wording
   e) inconsistencies across the company's own pages (legal entity names, dates, cert numbers, facility lists)
3. For each claim, search for evidence that substantiates or contradicts it. For certifications, look for a registry listing — not only the vendor's badge.
4. Rate each claim as EXACTLY one of: Substantiated, Unsubstantiated, Needs Substantiation, Inconsistent, Contradicted.
5. Do not invent certifications, registry results, URLs, or sources you did not find. If a registry is paywalled or blocked, that is a gap — use Needs Substantiation, not Unsubstantiated.
6. Write a 2–4 sentence summary that is direct and non-promotional.

Return STRICT JSON only. No markdown fences. Schema:
{
  "summary": "string",
  "claims": [
    {
      "title": "short label",
      "category": "Environment | Security | Certification | Legal | Entity | Other",
      "status": "one of the five exact values",
      "claim": "the public claim, quoted or closely paraphrased",
      "source": "URL or page description where the claim appears",
      "finding": "what the check showed"
    }
  ],
  "gaps": "what could not be verified and why"
}
"""


def get_secret(name: str, default: str = "") -> str:
    try:
        val = st.secrets.get(name)
        if val:
            return str(val)
    except Exception:
        pass
    import os

    return os.environ.get(name, default)


def extract_json(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    first = text.find("{")
    last = text.rfind("}")
    if first == -1 or last == -1 or last < first:
        raise ValueError("No JSON object found in model output.")
    return json.loads(text[first : last + 1])


def extract_output_text(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str) and payload["output_text"].strip():
        return payload["output_text"]
    chunks: list[str] = []
    for item in payload.get("output") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"message", "output_text"}:
            content = item.get("content")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        if part.get("type") in {"output_text", "text"} and part.get("text"):
                            chunks.append(str(part["text"]))
                        elif part.get("text"):
                            chunks.append(str(part["text"]))
                    elif isinstance(part, str):
                        chunks.append(part)
        if item.get("type") == "output_text" and item.get("text"):
            chunks.append(str(item["text"]))
    if chunks:
        return "\n".join(chunks)
    # Last-ditch walk
    dumped = json.dumps(payload)
    return dumped


def run_xai_audit(company: str, website: str, linkedin: str, api_key: str, model: str) -> dict[str, Any]:
    user_prompt = (
        f"Audit this company's public claims.\n"
        f"Company name: {company}\n"
        f"Website: {website or 'not provided — search for it'}\n"
        f"LinkedIn: {linkedin or 'not provided — search for it'}\n"
        "Search thoroughly before extracting claims. Return only the JSON object."
    )
    body = {
        "model": model,
        "input": [
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
        "tools": [{"type": "web_search"}],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=180.0) as client:
        resp = client.post(XAI_RESPONSES_URL, headers=headers, json=body)
    if resp.status_code >= 400:
        detail = resp.text[:600]
        raise RuntimeError(f"xAI API error {resp.status_code}: {detail}")
    payload = resp.json()
    raw = extract_output_text(payload)
    return extract_json(raw)


def pill_class(status: str) -> str:
    s = (status or "").lower()
    if "unsubstantiated" in s or "contradicted" in s:
        return "pill-red"
    if "substantiated" in s and "needs" not in s and "un" not in s:
        return "pill-green"
    return "pill-amber"


def render_report_html(company: str, data: dict[str, Any]) -> str:
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    cards = []
    if not claims:
        cards.append('<div class="claim-finding">No specific claims could be extracted for review.</div>')
    for c in claims:
        if not isinstance(c, dict):
            continue
        status = str(c.get("status") or "Needs Substantiation")
        title = html.escape(str(c.get("title") or "Untitled claim"))
        claim = html.escape(str(c.get("claim") or ""))
        finding = html.escape(str(c.get("finding") or ""))
        source = html.escape(str(c.get("source") or ""))
        category = html.escape(str(c.get("category") or ""))
        quote = f'<div class="claim-quote">“{claim}”</div>' if claim else ""
        finding_h = f'<div class="claim-finding">{finding}</div>' if finding else ""
        source_h = f'<div class="claim-source">Source: {source}</div>' if source else ""
        cat_h = f'<div class="claim-source">{category}</div>' if category else ""
        cards.append(
            f"""
            <div class="claim-card">
              <div class="claim-head">
                <div class="claim-title">{title}</div>
                <div class="pill {pill_class(status)}">{html.escape(status)}</div>
              </div>
              {cat_h}
              {quote}
              {finding_h}
              {source_h}
            </div>
            """
        )
    today = date.today().strftime("%B %d, %Y")
    summary = html.escape(str(data.get("summary") or "No summary provided."))
    gaps = html.escape(str(data.get("gaps") or "No gaps noted."))
    company_e = html.escape(company)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Vendor Claims Audit — {company_e}</title>
<style>
  :root {{
    --navy: #0f1c2e; --navy-soft: #1c2f47; --teal: #2ec4a5; --teal-deep: #1f9c83;
    --paper: #faf7f1; --paper-line: #e4ddcf; --ink: #1a1e24; --muted: #6b6f76;
    --red: #b3362c; --red-bg: #f8e9e7; --amber: #9a6b06; --amber-bg: #faf1dc;
    --green: #1f7a58; --green-bg: #e7f3ec;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--paper); color: var(--ink);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.55;
  }}
  .page {{ max-width: 740px; margin: 0 auto; padding: 56px 28px 90px; }}
  .letterhead {{ border-bottom: 3px solid var(--navy); padding-bottom: 20px; margin-bottom: 34px; position: relative; }}
  .letterhead::after {{ content: ""; position: absolute; left: 0; bottom: -6px; width: 64px; height: 3px; background: var(--teal); }}
  .kicker {{ text-transform: uppercase; letter-spacing: 0.14em; font-size: 11.5px; color: var(--teal-deep); font-weight: 600; margin-bottom: 10px; }}
  h1 {{ font-family: Georgia, "Times New Roman", serif; font-size: 34px; font-weight: 400; color: var(--navy); margin: 0 0 6px; }}
  .subtitle {{ color: var(--muted); font-size: 14.5px; }}
  .report-meta {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 22px; border-bottom: 1px solid var(--paper-line); padding-bottom: 12px; }}
  .section-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--teal-deep); font-weight: 700; margin: 30px 0 10px; }}
  .summary-text {{ font-family: Georgia, "Times New Roman", serif; font-size: 17px; line-height: 1.65; }}
  .claim-card {{ border-top: 1px solid var(--paper-line); padding: 22px 0; }}
  .claim-card:last-child {{ border-bottom: 1px solid var(--paper-line); }}
  .claim-head {{ display: flex; align-items: baseline; justify-content: space-between; gap: 14px; margin-bottom: 8px; }}
  .claim-title {{ font-family: Georgia, "Times New Roman", serif; font-size: 18px; color: var(--navy); font-weight: 700; }}
  .pill {{ flex-shrink: 0; font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; padding: 3px 10px; border-radius: 20px; white-space: nowrap; }}
  .pill-red {{ background: var(--red-bg); color: var(--red); }}
  .pill-amber {{ background: var(--amber-bg); color: var(--amber); }}
  .pill-green {{ background: var(--green-bg); color: var(--green); }}
  .claim-quote {{ font-size: 14.5px; background: #fff; border-left: 2px solid var(--paper-line); padding: 8px 14px; margin: 8px 0 10px; font-style: italic; }}
  .claim-finding {{ font-size: 14.5px; margin-bottom: 6px; }}
  .claim-source {{ font-size: 12.5px; color: var(--muted); }}
  .gaps-block {{ margin-top: 30px; background: #fff; border: 1px solid var(--paper-line); border-radius: 3px; padding: 16px 18px; font-size: 14px; color: var(--muted); }}
  .gaps-block strong {{ color: var(--navy); display: block; font-size: 12px; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 6px; }}
  .disclaimer {{ margin-top: 26px; font-size: 11.5px; color: #a39d8f; }}
  @media print {{
    body {{ background: #fff; }}
    .page {{ padding: 12px; }}
  }}
</style>
</head>
<body>
<div class="page">
  <div class="letterhead">
    <div class="kicker">Public claims screening</div>
    <h1>Vendor Claims Audit</h1>
    <div class="subtitle">Sustainability, security, certification &amp; liability statements — preliminary review</div>
  </div>
  <div class="report-meta">
    <span>Subject: {company_e}</span>
    <span>{html.escape(today)}</span>
  </div>
  <div class="section-label">Summary</div>
  <div class="summary-text">{summary}</div>
  <div class="section-label">Claims reviewed</div>
  {''.join(cards)}
  <div class="gaps-block">
    <strong>Gaps &amp; limitations</strong>
    {gaps}
  </div>
  <div class="disclaimer">
    This review is generated by an AI system using public web search. It is a preliminary screening aid, not a certified compliance audit, not independent assurance, and not a substitute for registry confirmation or on-site verification. Confirm high-stakes findings with the issuing registry and the vendor.
  </div>
</div>
</body>
</html>
"""


SAMPLE_REPORT = {
    "summary": "This is a sample report so you can see the layout before an API key is configured. Live audits replace this with searched public claims for the company you enter.",
    "claims": [
        {
            "title": "Zero-landfill claim",
            "category": "Environment",
            "status": "Needs Substantiation",
            "claim": "100% of retired assets diverted from landfill.",
            "source": "Sample vendor sustainability page",
            "finding": "Marketing page states the claim. No third-party diversion report or facility-level evidence was attached in this sample.",
        },
        {
            "title": "NAID AAA certification",
            "category": "Certification",
            "status": "Substantiated",
            "claim": "NAID AAA certified for physical destruction.",
            "source": "Sample i-SIGMA / NAID listing (demo)",
            "finding": "In a live run, this status is used only when a current registry listing is actually retrieved for the named legal entity or facility.",
        },
        {
            "title": "Blanket indemnification",
            "category": "Legal",
            "status": "Inconsistent",
            "claim": "Full indemnification for any data breach arising from disposition.",
            "source": "Sample homepage vs. sample terms page",
            "finding": "Homepage language is broader than the contract/terms limitation of liability. Flagged as inconsistent wording across public pages.",
        },
    ],
    "gaps": "Sample mode does not search the web. Configure XAI_API_KEY to run a live audit.",
}


def gate_password() -> bool:
    required = get_secret("APP_PASSWORD")
    if not required:
        return True
    if st.session_state.get("authed"):
        return True
    st.markdown("### Access")
    entered = st.text_input("Password", type="password")
    if st.button("Continue"):
        if entered == required:
            st.session_state.authed = True
            st.rerun()
        st.error("Incorrect password.")
    return False


def main() -> None:
    st.set_page_config(page_title="Vendor Claims Audit", page_icon="🗂️", layout="centered")
    st.markdown(
        """
        <style>
          .block-container { max-width: 760px; padding-top: 2rem; }
          h1 { font-family: Georgia, serif; font-weight: 400 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if not gate_password():
        return

    api_key = get_secret("XAI_API_KEY")
    model = get_secret("XAI_MODEL", DEFAULT_MODEL)

    st.caption("PUBLIC CLAIMS SCREENING")
    st.title("Vendor Claims Audit")
    st.write(
        "Enter a company. The app searches public pages and certification registries, "
        "then rates specific marketing claims. This is a screening aid — not a certified audit."
    )

    if not api_key:
        st.warning(
            "No `XAI_API_KEY` is configured. You can preview the report layout, "
            "but live audits need an xAI key in Streamlit secrets."
        )

    with st.form("audit_form"):
        company = st.text_input("Company name *", placeholder="e.g. Acme ITAD Solutions")
        c1, c2 = st.columns(2)
        with c1:
            website = st.text_input("Website (optional)", placeholder="acme-itad.com")
        with c2:
            linkedin = st.text_input("LinkedIn URL (optional)", placeholder="linkedin.com/company/acme-itad")
        submitted = st.form_submit_button("Run audit", type="primary")

    if submitted:
        if not company.strip():
            st.error("Please enter a company name.")
            return
        if not api_key:
            st.session_state.report = SAMPLE_REPORT
            st.session_state.company = company.strip() + " (sample layout)"
            st.session_state.live = False
        else:
            with st.spinner("Searching public pages, registries, and company materials…"):
                try:
                    parsed = run_xai_audit(company.strip(), website.strip(), linkedin.strip(), api_key, model)
                    st.session_state.report = parsed
                    st.session_state.company = company.strip()
                    st.session_state.live = True
                except Exception as exc:
                    st.error(str(exc))
                    return

    report = st.session_state.get("report")
    subject = st.session_state.get("company")
    if report and subject:
        html_doc = render_report_html(subject, report)
        st.components.v1.html(html_doc, height=2200, scrolling=True)
        st.download_button(
            "Download HTML report",
            data=html_doc.encode("utf-8"),
            file_name=f"vendor-claims-audit-{re.sub(r'[^a-z0-9]+', '-', subject.lower()).strip('-')}.html",
            mime="text/html",
        )
        if st.button("Run another audit"):
            st.session_state.pop("report", None)
            st.session_state.pop("company", None)
            st.rerun()

    with st.expander("How to read the ratings"):
        st.markdown(
            """
- **Substantiated** — a public source independent of (or stronger than) the marketing page supports the claim as written.
- **Needs substantiation** — the claim is public, but the supporting evidence was not found or is behind a registry wall.
- **Unsubstantiated** — searched sources do not support the claim.
- **Inconsistent** — the company's own pages disagree with each other.
- **Contradicted** — a public source conflicts with the claim.
            """
        )


if __name__ == "__main__":
    main()
