#!/usr/bin/env python3
"""Veridy public-claims screening report."""

from __future__ import annotations

import base64
import html
import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import streamlit as st
from fpdf import FPDF

XAI_RESPONSES_URL = "https://api.x.ai/v1/responses"
DEFAULT_MODEL = "grok-4.6"
ROOT = Path(__file__).resolve().parent
LOGO_CANDIDATES = (
    ROOT / "assets" / "veridy-logo.png",
    ROOT / "assets" / "veridy-logo.jpg",
    ROOT / "assets" / "veridy-logo.svg",
    ROOT / "veridy-logo.png",
    ROOT / "veridy-logo.jpg",
)

SYSTEM_PROMPT = """You are a rigorous, skeptical vendor-claims auditor working for a Veridy public-claims screening tool.
You specialize in ITAD (IT Asset Disposition). You may audit adjacent recycling / destruction / remarketing firms.

Method:
1. Use web search to find the company's website (homepage, About, services, sustainability, security, chain of custody / process, legal/terms), press pages, LinkedIn, and certification registries (NAID AAA / i-SIGMA, R2 / R2v3 / SERI, e-Stewards, ISO, SOC 2) when claimed.
2. Extract the vendor's own headline marketing claims from the website — short slogans and value promises as the company writes them (e.g. "Maximum Value", "Minimum Risk", "Unbroken Chain of Custody"). List 4 to 10 if present.
3. Extract 4 to 7 specific checkable claims and rate each. Priority:
   a) chain of custody (always include at least one CoC-related claim if any CoC language exists)
   b) environmental / sustainability
   c) data security / destruction method
   d) certification — company marketing vs a named facility listing
   e) legal / liability / indemnification
   f) entity-name or date inconsistencies
4. ALWAYS complete the chain_of_custody section, even if the company barely mentions CoC.
   Chain of custody is a PROCESS. Separate:
   - process claim ("we maintain / have an unbroken chain of custody")
   - evidence claim ("we provide chain-of-custody records / serial reports / photos / GPS / manifests")
   - verification claim ("we verify" or "we validate" chain of custody)
   Ask, from public pages only:
   - Do they claim CoC is achieved, or that CoC evidence is provided?
   - Do they claim they will notify the client of discrepancies?
   - Do they use verify, validate, maintain, unbroken, or provide?
   Rule: a vendor saying they "verify" their own chain of custody, without an independent party, is a TRUST claim. Treat that wording as potentially misleading. Status should not be Substantiated unless an independent verification mechanism is actually described and evidenced. Certification of a process is not the same as independent verification of a specific chain of custody.
5. Rate each claim as EXACTLY one of: Substantiated, Unsubstantiated, Needs Substantiation, Inconsistent, Contradicted.
6. Do not invent URLs, registry results, or quotes. Paywalled registries = gap + Needs Substantiation.
7. Summary: 2–4 sentences, direct, non-promotional.

Return STRICT JSON only. No markdown fences.
{
  "summary": "string",
  "headline_claims": ["short slogan or value claim as written on the site"],
  "chain_of_custody": {
    "status": "one of the five exact values",
    "quotes": ["verbatim or close CoC sentences found"],
    "claims_process": true,
    "claims_evidence": true,
    "claims_verification": true,
    "independent_verification_stated": false,
    "notify_discrepancies": "Yes | No | Unclear",
    "verbs": ["verify", "validate", "maintain", "unbroken", "provide"],
    "finding": "what the public pages actually say and why it is process, evidence, trust, or independent",
    "trust_note": "If they claim verification without an independent party, state that it is trust-based and the verification wording is misleading. Otherwise a short note."
  },
  "claims": [
    {
      "title": "short label",
      "category": "Chain of custody | Environment | Security | Certification | Legal | Entity | Other",
      "status": "one of the five exact values",
      "claim": "quoted or closely paraphrased public claim",
      "source": "URL or page description",
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
    return os.environ.get(name, default)


def find_logo() -> Path | None:
    for path in LOGO_CANDIDATES:
        if path.is_file():
            return path
    return None


def logo_data_uri() -> str | None:
    path = find_logo()
    if not path:
        return None
    raw = path.read_bytes()
    suffix = path.suffix.lower()
    mime = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".svg": "image/svg+xml"}.get(
        suffix, "image/png"
    )
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


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
    return json.dumps(payload)


def run_xai_audit(company: str, website: str, linkedin: str, api_key: str, model: str) -> dict[str, Any]:
    user_prompt = (
        f"Audit this company's public claims for a Veridy screening report.\n"
        f"Company name: {company}\n"
        f"Website: {website or 'not provided — search for it'}\n"
        f"LinkedIn: {linkedin or 'not provided — search for it'}\n"
        "Always extract headline slogans from the website and always complete the chain_of_custody section.\n"
        "Return only the JSON object."
    )
    body = {
        "model": model,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
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
        raise RuntimeError(f"xAI API error {resp.status_code}: {resp.text[:600]}")
    return extract_json(extract_output_text(resp.json()))


def pill_class(status: str) -> str:
    s = (status or "").lower()
    if "unsubstantiated" in s or "contradicted" in s:
        return "pill-red"
    if "substantiated" in s and "needs" not in s and "un" not in s:
        return "pill-green"
    return "pill-amber"


def yn(val: Any) -> str:
    if val is True:
        return "Yes"
    if val is False:
        return "No"
    return "Unclear"


def pdf_text(value: Any) -> str:
    text = str(value or "")
    replacements = {
        "\u2014": "-",
        "\u2013": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2022": "-",
        "\u00a0": " ",
        "\u2026": "...",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text.encode("latin-1", "replace").decode("latin-1")


NAVY = (15, 28, 46)
TEAL = (31, 156, 131)
INK = (26, 30, 36)
MUTED = (107, 111, 118)
PILL = {
    "pill-red": ((248, 233, 231), (179, 54, 44)),
    "pill-amber": ((250, 241, 220), (154, 107, 6)),
    "pill-green": ((231, 243, 236), (31, 122, 88)),
}


class ClaimsPDF(FPDF):
    def header(self) -> None:
        pass

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*MUTED)
        self.cell(0, 8, f"Veridy public claims screening  |  Page {self.page_no()}", align="C")


def _section_label(pdf: ClaimsPDF, label: str) -> None:
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*TEAL)
    pdf.cell(0, 7, pdf_text(label).upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(*INK)


def _wrapped(pdf: ClaimsPDF, text: str, size: int = 11, style: str = "", color=INK, after: float = 2) -> None:
    pdf.set_font("Helvetica", style, size)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, 5.2, pdf_text(text))
    pdf.ln(after)


def _pill(pdf: ClaimsPDF, status: str) -> None:
    key = pill_class(status)
    bg, fg = PILL[key]
    label = pdf_text(status).upper()
    pdf.set_font("Helvetica", "B", 7)
    w = pdf.get_string_width(label) + 6
    x, y = pdf.get_x(), pdf.get_y()
    pdf.set_fill_color(*bg)
    pdf.set_text_color(*fg)
    pdf.rect(x, y, w, 5.5, style="F")
    pdf.set_xy(x, y + 0.6)
    pdf.cell(w, 4.4, label, align="C")
    pdf.set_xy(x + w + 2, y)
    pdf.set_text_color(*INK)


def build_report_pdf(company: str, data: dict[str, Any]) -> bytes:
    pdf = ClaimsPDF(format="Letter", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.add_page()
    pdf.set_left_margin(18)
    pdf.set_right_margin(18)

    logo = find_logo()
    if logo and logo.suffix.lower() in {".png", ".jpg", ".jpeg"}:
        try:
            pdf.image(str(logo), x=18, y=14, h=12)
            pdf.set_y(30)
        except Exception:
            pdf.set_y(16)
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_text_color(*NAVY)
            pdf.cell(0, 6, "VERIDY VERIFICATION", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_y(16)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*NAVY)
        pdf.cell(0, 6, "VERIDY VERIFICATION", new_x="LMARGIN", new_y="NEXT")

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*TEAL)
    pdf.cell(0, 5, "VERIDY  ·  PUBLIC CLAIMS SCREENING", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Times", "", 22)
    pdf.set_text_color(*NAVY)
    pdf.cell(0, 10, "Vendor Claims Audit", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(
        0,
        5,
        pdf_text(
            "Headline promises, chain of custody, sustainability, security, certification & liability - not a Verified ITAD opinion"
        ),
    )
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.7)
    y = pdf.get_y() + 2
    pdf.line(18, y, 198, y)
    pdf.set_draw_color(*TEAL)
    pdf.set_line_width(0.9)
    pdf.line(18, y + 1.6, 42, y + 1.6)
    pdf.set_y(y + 5)

    today = date.today().strftime("%B %d, %Y")
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(90, 6, pdf_text(f"SUBJECT: {company}"))
    pdf.cell(0, 6, pdf_text(today), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_draw_color(228, 221, 207)
    pdf.set_line_width(0.2)
    pdf.line(18, pdf.get_y(), 198, pdf.get_y())
    pdf.ln(3)

    _section_label(pdf, "Summary")
    _wrapped(pdf, str(data.get("summary") or "No summary provided."), size=12, style="")

    _section_label(pdf, "Headline claims on the website")
    headlines = data.get("headline_claims") if isinstance(data.get("headline_claims"), list) else []
    if headlines:
        for h in headlines:
            if str(h).strip():
                _wrapped(pdf, f"- {h}", size=11, after=0.5)
        pdf.ln(2)
    else:
        _wrapped(pdf, "No headline slogans were extracted from public pages.", size=10, color=MUTED)

    coc = data.get("chain_of_custody") if isinstance(data.get("chain_of_custody"), dict) else {}
    _section_label(pdf, "Chain of custody review")
    row_y = pdf.get_y()
    pdf.set_font("Times", "B", 14)
    pdf.set_text_color(*NAVY)
    pdf.cell(110, 7, "Chain of custody")
    pdf.set_xy(130, row_y + 1)
    _pill(pdf, str(coc.get("status") or "Needs Substantiation"))
    pdf.set_y(row_y + 9)
    _wrapped(
        pdf,
        "Chain of custody is a process. Public pages were checked for whether the vendor claims the process exists, whether evidence is offered, and whether verification is the vendor's own word or an independent act.",
        size=9,
        color=MUTED,
    )
    quotes = coc.get("quotes") if isinstance(coc.get("quotes"), list) else []
    for q in quotes:
        if str(q).strip():
            _wrapped(pdf, f'"{q}"', size=10, style="I")
    rows = [
        ("Claims the process is achieved / maintained", yn(coc.get("claims_process"))),
        ("Claims CoC evidence is provided to the client", yn(coc.get("claims_evidence"))),
        ("Claims to verify or validate CoC", yn(coc.get("claims_verification"))),
        ("Independent verification described", yn(coc.get("independent_verification_stated"))),
        ("Will notify client of discrepancies", str(coc.get("notify_discrepancies") or "Unclear")),
        (
            "Verbs used",
            ", ".join(str(v) for v in (coc.get("verbs") or []) if str(v).strip()) or "none found",
        ),
    ]
    pdf.set_font("Helvetica", "", 10)
    for label, val in rows:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*NAVY)
        pdf.cell(118, 6, pdf_text(label))
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK)
        pdf.cell(0, 6, pdf_text(val), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    if coc.get("finding"):
        _wrapped(pdf, str(coc.get("finding")), size=10)
    if coc.get("trust_note"):
        pdf.set_fill_color(250, 241, 220)
        pdf.set_text_color(109, 78, 4)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, pdf_text(str(coc.get("trust_note"))), fill=True)
        pdf.ln(2)

    _section_label(pdf, "Other claims reviewed")
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    if not claims:
        _wrapped(pdf, "No specific claims could be extracted for review.", size=10, color=MUTED)
    for c in claims:
        if not isinstance(c, dict):
            continue
        if pdf.get_y() > 250:
            pdf.add_page()
        status = str(c.get("status") or "Needs Substantiation")
        y0 = pdf.get_y()
        pdf.set_font("Times", "B", 13)
        pdf.set_text_color(*NAVY)
        title = pdf_text(str(c.get("title") or "Untitled claim"))
        pdf.multi_cell(120, 6, title)
        title_bottom = pdf.get_y()
        pdf.set_xy(140, y0 + 1)
        _pill(pdf, status)
        pdf.set_y(max(title_bottom, y0 + 8))
        if c.get("category"):
            _wrapped(pdf, str(c.get("category")), size=8, color=MUTED, after=1)
        if c.get("claim"):
            _wrapped(pdf, f'"{c.get("claim")}"', size=10, style="I", after=1)
        if c.get("finding"):
            _wrapped(pdf, str(c.get("finding")), size=10, after=1)
        if c.get("source"):
            _wrapped(pdf, f"Source: {c.get('source')}", size=8, color=MUTED, after=3)
        pdf.set_draw_color(228, 221, 207)
        pdf.line(18, pdf.get_y(), 198, pdf.get_y())
        pdf.ln(3)

    _section_label(pdf, "Gaps & limitations")
    _wrapped(pdf, str(data.get("gaps") or "No gaps noted."), size=10, color=MUTED)

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MUTED)
    pdf.multi_cell(
        0,
        4.2,
        pdf_text(
            "Prepared as a Veridy public-claims screening aid using AI and public web search. "
            "It is not a Verified ITAD determination, not independent assurance of the vendor, "
            "not a certified compliance audit, and not a substitute for registry confirmation or job-level evidence. "
            "A vendor that verifies its own chain of custody is asking to be trusted."
        ),
    )

    return bytes(pdf.output())


def render_report_html(company: str, data: dict[str, Any], logo_uri: str | None) -> str:
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    headlines = data.get("headline_claims") if isinstance(data.get("headline_claims"), list) else []
    coc = data.get("chain_of_custody") if isinstance(data.get("chain_of_custody"), dict) else {}

    headline_items = ""
    if headlines:
        lis = "".join(f"<li>{html.escape(str(h))}</li>" for h in headlines if str(h).strip())
        headline_items = f"<ul class='headlines'>{lis}</ul>"
    else:
        headline_items = "<div class='claim-finding'>No headline slogans were extracted from public pages.</div>"

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
              {cat_h}{quote}{finding_h}{source_h}
            </div>
            """
        )

    coc_status = str(coc.get("status") or "Needs Substantiation")
    quotes = coc.get("quotes") if isinstance(coc.get("quotes"), list) else []
    quote_html = "".join(
        f'<div class="claim-quote">“{html.escape(str(q))}”</div>' for q in quotes if str(q).strip()
    )
    verbs = coc.get("verbs") if isinstance(coc.get("verbs"), list) else []
    verbs_s = html.escape(", ".join(str(v) for v in verbs if str(v).strip()) or "none found")
    coc_block = f"""
    <div class="coc-block">
      <div class="claim-head">
        <div class="claim-title">Chain of custody</div>
        <div class="pill {pill_class(coc_status)}">{html.escape(coc_status)}</div>
      </div>
      <p class="coc-lead">Chain of custody is a process. Public pages were checked for whether the vendor claims the process exists, whether evidence is offered, and whether “verification” is the vendor’s own word or an independent act.</p>
      {quote_html}
      <table class="coc-table">
        <tr><th>Claims the process is achieved / maintained</th><td>{html.escape(yn(coc.get("claims_process")))}</td></tr>
        <tr><th>Claims CoC evidence is provided to the client</th><td>{html.escape(yn(coc.get("claims_evidence")))}</td></tr>
        <tr><th>Claims to verify or validate CoC</th><td>{html.escape(yn(coc.get("claims_verification")))}</td></tr>
        <tr><th>Independent verification described</th><td>{html.escape(yn(coc.get("independent_verification_stated")))}</td></tr>
        <tr><th>Will notify client of discrepancies</th><td>{html.escape(str(coc.get("notify_discrepancies") or "Unclear"))}</td></tr>
        <tr><th>Verbs used</th><td>{verbs_s}</td></tr>
      </table>
      <div class="claim-finding">{html.escape(str(coc.get("finding") or ""))}</div>
      <div class="trust-note">{html.escape(str(coc.get("trust_note") or ""))}</div>
    </div>
    """

    brand = ""
    if logo_uri:
        brand = f'<img class="brand-logo" src="{logo_uri}" alt="Veridy">'
    else:
        brand = '<div class="brand-word">VERIDY</div>'

    today = date.today().strftime("%B %d, %Y")
    summary = html.escape(str(data.get("summary") or "No summary provided."))
    gaps = html.escape(str(data.get("gaps") or "No gaps noted."))
    company_e = html.escape(company)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Veridy Claims Screening — {company_e}</title>
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
  .page {{ max-width: 740px; margin: 0 auto; padding: 40px 28px 90px; }}
  .brand-logo {{ height: 36px; width: auto; display: block; margin-bottom: 18px; }}
  .brand-word {{
    font-size: 13px; letter-spacing: 0.28em; font-weight: 700; color: var(--navy);
    margin-bottom: 18px;
  }}
  .letterhead {{ border-bottom: 3px solid var(--navy); padding-bottom: 20px; margin-bottom: 28px; position: relative; }}
  .letterhead::after {{ content: ""; position: absolute; left: 0; bottom: -6px; width: 64px; height: 3px; background: var(--teal); }}
  .kicker {{ text-transform: uppercase; letter-spacing: 0.14em; font-size: 11.5px; color: var(--teal-deep); font-weight: 600; margin-bottom: 10px; }}
  h1 {{ font-family: Georgia, "Times New Roman", serif; font-size: 32px; font-weight: 400; color: var(--navy); margin: 0 0 6px; }}
  .subtitle {{ color: var(--muted); font-size: 14.5px; }}
  .report-meta {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 22px; border-bottom: 1px solid var(--paper-line); padding-bottom: 12px; }}
  .section-label {{ font-size: 12px; text-transform: uppercase; letter-spacing: 0.1em; color: var(--teal-deep); font-weight: 700; margin: 30px 0 10px; }}
  .summary-text {{ font-family: Georgia, "Times New Roman", serif; font-size: 17px; line-height: 1.65; }}
  .headlines {{ margin: 0; padding-left: 18px; }}
  .headlines li {{ margin: 4px 0; font-size: 15.5px; }}
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
  .coc-block {{ background: #fff; border: 1px solid var(--paper-line); border-radius: 3px; padding: 18px 18px 14px; }}
  .coc-lead {{ font-size: 14px; color: var(--muted); margin-top: 0; }}
  .coc-table {{ width: 100%; border-collapse: collapse; margin: 10px 0 14px; font-size: 13.5px; }}
  .coc-table th {{ text-align: left; font-weight: 600; color: var(--navy); padding: 6px 10px 6px 0; width: 62%; }}
  .coc-table td {{ padding: 6px 0; color: var(--ink); }}
  .trust-note {{ font-size: 14px; background: var(--amber-bg); color: #6d4e04; padding: 10px 12px; border-radius: 2px; margin-top: 8px; }}
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
  {brand}
  <div class="letterhead">
    <div class="kicker">Veridy · Public claims screening</div>
    <h1>Vendor Claims Audit</h1>
    <div class="subtitle">Headline promises, chain of custody, sustainability, security, certification &amp; liability — not a Verified ITAD opinion</div>
  </div>
  <div class="report-meta">
    <span>Subject: {company_e}</span>
    <span>{html.escape(today)}</span>
  </div>
  <div class="section-label">Summary</div>
  <div class="summary-text">{summary}</div>
  <div class="section-label">Headline claims on the website</div>
  {headline_items}
  <div class="section-label">Chain of custody review</div>
  {coc_block}
  <div class="section-label">Other claims reviewed</div>
  {''.join(cards)}
  <div class="gaps-block">
    <strong>Gaps &amp; limitations</strong>
    {gaps}
  </div>
  <div class="disclaimer">
    Prepared as a Veridy public-claims screening aid using AI and public web search. It is not a Verified ITAD determination, not independent assurance of the vendor, not a certified compliance audit, and not a substitute for registry confirmation or job-level evidence. A vendor that “verifies” its own chain of custody is asking to be trusted. Confirm material findings with the issuing registry and with primary records.
  </div>
</div>
</body>
</html>
"""


SAMPLE_REPORT = {
    "summary": "Sample layout only. A live run replaces this with searched public claims for the company you enter.",
    "headline_claims": [
        "Maximum Value",
        "Minimum Risk",
        "Unbroken Chain of Custody",
    ],
    "chain_of_custody": {
        "status": "Needs Substantiation",
        "quotes": ["Unbroken chain of custody from pickup to final disposition."],
        "claims_process": True,
        "claims_evidence": False,
        "claims_verification": True,
        "independent_verification_stated": False,
        "notify_discrepancies": "Unclear",
        "verbs": ["unbroken", "verify"],
        "finding": "The sample vendor asserts an unbroken process and uses “verify” about its own handling. No independent party or job-level evidence offer is described.",
        "trust_note": "Claiming to verify your own chain of custody, without an independent verifier, is a trust claim and the verification wording is misleading.",
    },
    "claims": [
        {
            "title": "Zero-landfill claim",
            "category": "Environment",
            "status": "Needs Substantiation",
            "claim": "100% of retired assets diverted from landfill.",
            "source": "Sample sustainability page",
            "finding": "Marketing page only in this sample.",
        }
    ],
    "gaps": "Sample mode does not search the web.",
}


def gate_password() -> bool:
    required = get_secret("APP_PASSWORD")
    if not required:
        return True
    if st.session_state.get("authed"):
        return True
    logo = find_logo()
    if logo:
        st.image(str(logo), width=140)
    else:
        st.markdown("**VERIDY**")
    st.markdown("### Claims screening")
    st.caption("Enter the access password you were given.")
    entered = st.text_input("Password", type="password")
    if st.button("Continue"):
        if entered == required:
            st.session_state.authed = True
            st.rerun()
        st.error("Incorrect password.")
    return False


def main() -> None:
    st.set_page_config(page_title="Veridy Claims Screening", page_icon="🗂️", layout="centered")
    st.markdown(
        """
        <style>
          header[data-testid="stHeader"] {
            background: #faf7f1;
          }
          .block-container {
            max-width: 760px;
            padding-top: 4.75rem !important;
          }
          div[data-testid="stImage"] { margin-top: 0.25rem; }
          h1 { font-family: Georgia, serif; font-weight: 400 !important; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if not gate_password():
        return

    api_key = get_secret("XAI_API_KEY")
    model = get_secret("XAI_MODEL", DEFAULT_MODEL)
    logo = find_logo()
    logo_uri = logo_data_uri()

    if logo:
        st.image(str(logo), width=160)
    else:
        st.markdown("**VERIDY**")

    st.caption("PUBLIC CLAIMS SCREENING")
    st.title("Vendor Claims Audit")
    st.write(
        "Enter a company. The report lists headline promises from the website, "
        "always reviews chain-of-custody language (process vs evidence vs self-verification), "
        "and rates other public claims. This is a screening aid — not a Verified ITAD opinion."
    )

    if not api_key:
        st.warning("No `XAI_API_KEY` in Streamlit secrets. Sample layout only.")

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
        else:
            with st.spinner("Reading public pages and chain-of-custody language…"):
                try:
                    st.session_state.report = run_xai_audit(
                        company.strip(), website.strip(), linkedin.strip(), api_key, model
                    )
                    st.session_state.company = company.strip()
                except Exception as exc:
                    st.error(str(exc))
                    return

    report = st.session_state.get("report")
    subject = st.session_state.get("company")
    if report and subject:
        html_doc = render_report_html(subject, report, logo_uri)
        st.components.v1.html(html_doc, height=2800, scrolling=True)
        slug = re.sub(r"[^a-z0-9]+", "-", subject.lower()).strip("-")
        try:
            pdf_bytes = build_report_pdf(subject, report)
            st.download_button(
                "Download PDF report",
                data=pdf_bytes,
                file_name=f"veridy-claims-{slug}.pdf",
                mime="application/pdf",
            )
        except Exception as exc:
            st.warning(f"PDF export failed ({exc}). HTML download is available instead.")
            st.download_button(
                "Download HTML report",
                data=html_doc.encode("utf-8"),
                file_name=f"veridy-claims-{slug}.html",
                mime="text/html",
            )
        if st.button("Run another audit"):
            st.session_state.pop("report", None)
            st.session_state.pop("company", None)
            st.rerun()

    with st.expander("How chain of custody is read"):
        st.markdown(
            """
Chain of custody is a **process**. This review splits four things vendors often conflate:

- **Process claim** — “we maintain / have an unbroken chain of custody.”
- **Evidence claim** — they say they will give the client records (manifests, serials, photos, GPS, seals).
- **Self-verification** — they say they *verify* or *validate* chain of custody. If no independent party is named, that is a **trust** claim. Calling it verification is misleading.
- **Independent verification** — a party other than the vendor checks the chain. Certification of a management system is not the same as verifying a specific chain.

Also checked: whether they say they will **notify the client of discrepancies**.
            """
        )


if __name__ == "__main__":
    main()
