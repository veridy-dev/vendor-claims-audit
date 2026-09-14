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

SYSTEM_PROMPT = """You are preparing a Veridy Claims Readiness Review from PUBLIC sources only.
This is not a verdict on whether the vendor is trustworthy. It is a map of:
- What they claim
- What public evidence supports it
- What cannot be verified from public sources
- What appears inconsistent
- What evidence would resolve the question

Never turn an unanswered question into an accusation. Absence of public evidence is not evidence that the claim is false.

Role of the subject company:
- Distinguish ITAD *manager* / broker / consultant from the *processing facility*.
- A manager that says it “provides recycling” or “certified destruction” often means it arranges those services through partners. That is not automatically a contradiction.
- Certification claims about R2, e-Stewards, NAID AAA must be tested against the *named facility* that would perform the work. If partners are unnamed, status is Unable to Verify — not Contradicted and not a finding against the manager.
- Distinguish brand name, legal entity, DBA, subsidiary, and parent. An acquisition does not erase the acquired LLC. Do not call a remaining brand/entity “Contradicted” just because a parent bought the company.

Independence / Veridy / third-party verification:
- Ownership, software provenance, branding, who sells the service, and who *performs* verification are different questions.
- Association (shared brand, FAQ, Glassdoor, founder) raises a question. It does not prove the vendor verifies itself.
- If independence is not established from public evidence, status is Unable to Verify or Ambiguous. Finding must say “Independence not established from public evidence.” Do NOT use Contradicted unless a reliable source shows the same party both performs the ITAD work and issues the verification of that work.

Outsourcing / partner network (always complete operating_model):
- Detect whether the vendor processes in its own facilities, manages work through contracted partners, or both. “Global coverage,” “partner network,” “audited partners,” “nationwide/worldwide footprint” usually means outsourcing.
- If work is outsourced, HOW the network is managed is the claim that matters — more than the coverage slogan. Look for: named partners vs unnamed; contracts; how partners are selected and audited; whether the vendor or the partner holds the asset; who can release an equipment hold; segregation of duties between operations and verification; verification tags or equivalent; independent verification of partner work.
- Outsourcing without described controls is not automatically Contradicted. It is Unable to Verify / Not Publicly Substantiated, with a specific evidence request (partner list or category list, audit protocol, hold-and-release rules, who verifies partner output).
- Independent verification, SoD, tags, and verification holds matter MORE when processing is outsourced. Say that in the finding when a network model is present.

Chain of custody:
- CoC is a process. Separate process claims, evidence-offered claims, and verify/validate wording.
- Always ask, from public pages: Are exception / discrepancy reports offered to the client? How are inventory discrepancies resolved? Who is notified, and who can close an exception?
- Absolute words (guaranteed, unbroken, irrefutable, 100%) should be flagged as language that needs a tighter public formulation — not as proof the process fails.
- Do not infer that the vendor “verifies its own chain of custody” unless they say that. If unclear, ask for evidence rather than accuse.

Legal conclusions:
- Words like “illegal” on a vendor site, without a cited statute, are unsourced legal conclusions. Rate Ambiguous and request a source. Do not treat that as proof they are breaking the law.

Statuses — use EXACTLY one of these five strings:
- Supported — sufficient independent public evidence supports the claim as written.
- Contradicted — a reliable public source affirmatively conflicts with the claim.
- Not Publicly Substantiated — the vendor makes the claim; supporting public evidence was not found. This is not a finding that the claim is false.
- Unable to Verify — checking the claim requires nonpublic items (contracts, unnamed downstream facilities, certificates, project records, paywalled registries).
- Ambiguous — the wording is unclear, manager vs facility is mixed, or brand/entity/parent is mixed. State what clarification would resolve it.

Method:
1. Search the website, About/services/security/CoC/legal pages, press, LinkedIn, and relevant registries.
2. List 4–10 headline slogans as written.
3. Extract 4–7 checkable claims (CoC first, then environment, security, certs, legal, entity).
4. Always complete chain_of_custody AND operating_model.
5. For every claim include evidence_needed: the specific public or private item that would move the status to Supported or Contradicted.
6. Do not invent URLs, registry hits, or quotes.
7. Summary: 2–4 sentences plus a count in this shape: “Supported N · Contradicted N · Not publicly substantiated N · Unable to verify N · Ambiguous N.”

Return STRICT JSON only. No markdown fences.
{
  "summary": "string",
  "readiness": {
    "supported": 0,
    "contradicted": 0,
    "not_publicly_substantiated": 0,
    "unable_to_verify": 0,
    "ambiguous": 0
  },
  "headline_claims": ["short slogan as written"],
  "chain_of_custody": {
    "status": "one of the five statuses",
    "quotes": ["verbatim or close CoC sentences"],
    "claims_process": true,
    "claims_evidence": true,
    "claims_verification": true,
    "independent_verification_stated": false,
    "notify_discrepancies": "Yes | No | Unclear",
    "exception_reports_offered": "Yes | No | Unclear",
    "discrepancy_resolution_described": "Yes | No | Unclear",
    "verbs": ["verify", "validate", "maintain", "unbroken", "provide"],
    "finding": "what public pages say; include exception reports and how discrepancies are closed",
    "evidence_needed": "what would resolve the CoC question"
  },
  "operating_model": {
    "status": "one of the five statuses",
    "model": "Own facilities | Partner network | Hybrid | Unclear",
    "outsources_processing": true,
    "partners_named": false,
    "partner_audit_described": false,
    "segregation_of_duties_described": false,
    "verification_tags_described": false,
    "equipment_holds_described": false,
    "independent_verification_of_partner_work": false,
    "quotes": ["coverage or partner sentences"],
    "finding": "how public pages describe network management; if outsourced, state that controls matter more",
    "evidence_needed": "partner management protocol, named facilities or categories, hold/release rules, who verifies partner output"
  },
  "claims": [
    {
      "title": "short label",
      "category": "Chain of custody | Environment | Security | Certification | Legal | Entity | Independence | Other",
      "status": "one of the five statuses",
      "claim": "quoted or closely paraphrased public claim",
      "source": "URL or page description",
      "finding": "what the public check showed",
      "evidence_needed": "specific item that would resolve it"
    }
  ],
  "gaps": "what could not be verified from public sources and why"
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
    last_err: Exception | None = None
    for attempt in range(2):
        try:
            with httpx.Client(timeout=httpx.Timeout(300.0, connect=20.0)) as client:
                resp = client.post(XAI_RESPONSES_URL, headers=headers, json=body)
            if resp.status_code >= 400:
                raise RuntimeError(f"xAI API error {resp.status_code}: {resp.text[:600]}")
            return extract_json(extract_output_text(resp.json()))
        except httpx.TimeoutException as exc:
            last_err = exc
            if attempt == 0:
                continue
            raise RuntimeError(
                "The read timed out after 5 minutes. Grok was still searching public pages. "
                "Wait a few seconds and run the same company again. "
                "If it keeps failing, set XAI_MODEL to grok-4.1-fast in Streamlit Secrets."
            ) from exc
        except Exception as exc:
            last_err = exc
            raise
    raise RuntimeError(str(last_err) if last_err else "Audit failed.")


def pill_class(status: str) -> str:
    s = (status or "").lower()
    if "contradicted" in s:
        return "pill-red"
    if s.startswith("supported") or s in {"substantiated", "verified"}:
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
        ("Exception / discrepancy reports offered", str(coc.get("exception_reports_offered") or "Unclear")),
        ("How discrepancies are resolved is described", str(coc.get("discrepancy_resolution_described") or "Unclear")),
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

    ops = data.get("operating_model") if isinstance(data.get("operating_model"), dict) else {}
    _section_label(pdf, "Operating model and partner network")
    oy = pdf.get_y()
    pdf.set_font("Times", "B", 14)
    pdf.set_text_color(*NAVY)
    pdf.cell(110, 7, pdf_text(str(ops.get("model") or "Operating model")))
    pdf.set_xy(130, oy + 1)
    _pill(pdf, str(ops.get("status") or "Unable to Verify"))
    pdf.set_y(oy + 9)
    _wrapped(
        pdf,
        "If processing is outsourced, how the network is managed matters more than a coverage slogan. Independent verification, segregation of duties, verification tags, and equipment holds matter more when another party holds the asset.",
        size=9,
        color=MUTED,
    )
    for q in ops.get("quotes") or []:
        if str(q).strip():
            _wrapped(pdf, f'"{q}"', size=10, style="I")
    for label, val in [
        ("Outsources processing", yn(ops.get("outsources_processing"))),
        ("Partners named in public materials", yn(ops.get("partners_named"))),
        ("Partner audit method described", yn(ops.get("partner_audit_described"))),
        ("Segregation of duties described", yn(ops.get("segregation_of_duties_described"))),
        ("Verification tags described", yn(ops.get("verification_tags_described"))),
        ("Equipment verification holds described", yn(ops.get("equipment_holds_described"))),
        ("Independent verification of partner work", yn(ops.get("independent_verification_of_partner_work"))),
    ]:
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(*NAVY)
        pdf.cell(118, 6, pdf_text(label))
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*INK)
        pdf.cell(0, 6, pdf_text(val), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    if ops.get("finding"):
        _wrapped(pdf, str(ops.get("finding")), size=10)
    if ops.get("evidence_needed"):
        pdf.set_fill_color(250, 241, 220)
        pdf.set_text_color(109, 78, 4)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, pdf_text("Evidence that would resolve this: " + str(ops.get("evidence_needed"))), fill=True)
        pdf.ln(2)
    need = coc.get("evidence_needed") or coc.get("trust_note")
    if need:
        pdf.set_fill_color(250, 241, 220)
        pdf.set_text_color(109, 78, 4)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, pdf_text("Evidence that would resolve this: " + str(need)), fill=True)
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
            _wrapped(pdf, f"Source: {c.get('source')}", size=8, color=MUTED, after=1)
        if c.get("evidence_needed"):
            _wrapped(pdf, f"Evidence that would resolve this: {c.get('evidence_needed')}", size=9, after=3)
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
            "An unanswered public question is an evidence request, not an accusation."
        ),
    )

    return bytes(pdf.output())


def render_report_html(company: str, data: dict[str, Any], logo_uri: str | None) -> str:
    claims = data.get("claims") if isinstance(data.get("claims"), list) else []
    headlines = data.get("headline_claims") if isinstance(data.get("headline_claims"), list) else []
    coc = data.get("chain_of_custody") if isinstance(data.get("chain_of_custody"), dict) else {}
    ops = data.get("operating_model") if isinstance(data.get("operating_model"), dict) else {}

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
        status = str(c.get("status") or "Unable to Verify")
        title = html.escape(str(c.get("title") or "Untitled claim"))
        claim = html.escape(str(c.get("claim") or ""))
        finding = html.escape(str(c.get("finding") or ""))
        source = html.escape(str(c.get("source") or ""))
        category = html.escape(str(c.get("category") or ""))
        needed = html.escape(str(c.get("evidence_needed") or ""))
        quote = f'<div class="claim-quote">“{claim}”</div>' if claim else ""
        finding_h = f'<div class="claim-finding">{finding}</div>' if finding else ""
        source_h = f'<div class="claim-source">Source: {source}</div>' if source else ""
        cat_h = f'<div class="claim-source">{category}</div>' if category else ""
        need_h = f'<div class="trust-note">Evidence that would resolve this: {needed}</div>' if needed else ""
        cards.append(
            f"""
            <div class="claim-card">
              <div class="claim-head">
                <div class="claim-title">{title}</div>
                <div class="pill {pill_class(status)}">{html.escape(status)}</div>
              </div>
              {cat_h}{quote}{finding_h}{source_h}{need_h}
            </div>
            """
        )

    coc_status = str(coc.get("status") or "Unable to Verify")
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
      <p class="coc-lead">Chain of custody is a process. This block records what the vendor claims in public and what evidence would be needed to support or contradict it. An unanswered question is not an accusation.</p>
      {quote_html}
      <table class="coc-table">
        <tr><th>Claims the process is achieved / maintained</th><td>{html.escape(yn(coc.get("claims_process")))}</td></tr>
        <tr><th>Claims CoC evidence is provided to the client</th><td>{html.escape(yn(coc.get("claims_evidence")))}</td></tr>
        <tr><th>Claims to verify or validate CoC</th><td>{html.escape(yn(coc.get("claims_verification")))}</td></tr>
        <tr><th>Independent verification described</th><td>{html.escape(yn(coc.get("independent_verification_stated")))}</td></tr>
        <tr><th>Will notify client of discrepancies</th><td>{html.escape(str(coc.get("notify_discrepancies") or "Unclear"))}</td></tr>
        <tr><th>Exception / discrepancy reports offered</th><td>{html.escape(str(coc.get("exception_reports_offered") or "Unclear"))}</td></tr>
        <tr><th>How discrepancies are resolved is described</th><td>{html.escape(str(coc.get("discrepancy_resolution_described") or "Unclear"))}</td></tr>
        <tr><th>Verbs used</th><td>{verbs_s}</td></tr>
      </table>
      <div class="claim-finding">{html.escape(str(coc.get("finding") or ""))}</div>
      <div class="trust-note">Evidence that would resolve this: {html.escape(str(coc.get("evidence_needed") or coc.get("trust_note") or "Primary CoC records, exception reports, and how discrepancies are closed."))}</div>
    </div>
    """
    ops_quotes = "".join(
        f'<div class="claim-quote">“{html.escape(str(q))}”</div>'
        for q in (ops.get("quotes") or [])
        if str(q).strip()
    )
    ops_status = str(ops.get("status") or "Unable to Verify")
    ops_block = f"""
    <div class="coc-block">
      <div class="claim-head">
        <div class="claim-title">{html.escape(str(ops.get("model") or "Operating model"))}</div>
        <div class="pill {pill_class(ops_status)}">{html.escape(ops_status)}</div>
      </div>
      <p class="coc-lead">If processing is outsourced, how the network is managed matters more than a coverage slogan. Independent verification, segregation of duties, verification tags, and equipment holds matter more when another party holds the asset.</p>
      {ops_quotes}
      <table class="coc-table">
        <tr><th>Outsources processing</th><td>{html.escape(yn(ops.get("outsources_processing")))}</td></tr>
        <tr><th>Partners named in public materials</th><td>{html.escape(yn(ops.get("partners_named")))}</td></tr>
        <tr><th>Partner audit method described</th><td>{html.escape(yn(ops.get("partner_audit_described")))}</td></tr>
        <tr><th>Segregation of duties described</th><td>{html.escape(yn(ops.get("segregation_of_duties_described")))}</td></tr>
        <tr><th>Verification tags described</th><td>{html.escape(yn(ops.get("verification_tags_described")))}</td></tr>
        <tr><th>Equipment verification holds described</th><td>{html.escape(yn(ops.get("equipment_holds_described")))}</td></tr>
        <tr><th>Independent verification of partner work</th><td>{html.escape(yn(ops.get("independent_verification_of_partner_work")))}</td></tr>
      </table>
      <div class="claim-finding">{html.escape(str(ops.get("finding") or ""))}</div>
      <div class="trust-note">Evidence that would resolve this: {html.escape(str(ops.get("evidence_needed") or "Partner management protocol, named facilities or categories, hold/release rules, and who verifies partner output."))}</div>
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
    <div class="subtitle">Claims Readiness Review — public evidence map, not a Verified ITAD opinion</div>
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
  <div class="section-label">Operating model and partner network</div>
  {ops_block}
  <div class="section-label">Other claims reviewed</div>
  {''.join(cards)}
  <div class="gaps-block">
    <strong>Gaps &amp; limitations</strong>
    {gaps}
  </div>
  <div class="disclaimer">
    Prepared as a Veridy Claims Readiness Review from public sources. It is not a Verified ITAD determination, not independent assurance of the vendor, and not a finding that an unverified claim is false. Absence of public evidence is not evidence the claim is false. An unanswered question is an evidence request. Confirm material items with registries, named facilities, contracts, and primary records.
  </div>
</div>
</body>
</html>
"""


SAMPLE_REPORT = {
    "summary": "Sample layout only. Supported 0 · Contradicted 0 · Not publicly substantiated 1 · Unable to verify 1 · Ambiguous 0.",
    "headline_claims": [
        "Maximum Value",
        "Minimum Risk",
        "Unbroken Chain of Custody",
    ],
    "chain_of_custody": {
        "status": "Unable to Verify",
        "quotes": ["Unbroken chain of custody from pickup to final disposition."],
        "claims_process": True,
        "claims_evidence": False,
        "claims_verification": True,
        "independent_verification_stated": False,
        "notify_discrepancies": "Unclear",
        "verbs": ["unbroken", "verify"],
        "finding": "Public pages claim an unbroken process and use verify. Who performs any verification, and whether job-level records exist, is not established from public evidence.",
        "exception_reports_offered": "Unclear",
        "discrepancy_resolution_described": "Unclear",
        "evidence_needed": "A sample chain-of-custody packet, an exception report, and who can close a discrepancy.",
    },
    "operating_model": {
        "status": "Unable to Verify",
        "model": "Partner network",
        "outsources_processing": True,
        "partners_named": False,
        "partner_audit_described": False,
        "segregation_of_duties_described": False,
        "verification_tags_described": False,
        "equipment_holds_described": False,
        "independent_verification_of_partner_work": False,
        "quotes": ["Global coverage through audited, contracted partners."],
        "finding": "Coverage is claimed through partners. Public pages do not describe how partners are managed, who holds the asset, or who verifies partner output.",
        "evidence_needed": "Partner management protocol, hold/release rules, and who verifies partner work.",
    },
    "claims": [
        {
            "title": "Zero-landfill claim",
            "category": "Environment",
            "status": "Not Publicly Substantiated",
            "claim": "100% of retired assets diverted from landfill.",
            "source": "Sample sustainability page",
            "finding": "The claim appears on a marketing page. No public diversion report was found. That is not a finding that the claim is false.",
            "evidence_needed": "Named downstream facilities and a recent diversion or residual-waste report.",
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
        "Enter a company for a Claims Readiness Review. The report lists what the website claims, "
        "what public evidence supports, what cannot be verified from public sources, and what "
        "evidence would resolve each question. It is not a Verified ITAD opinion and does not "
        "treat missing public proof as proof the claim is false."
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

    with st.expander("How ratings work"):
        st.markdown(
            """
- **Supported** — independent public evidence supports the claim as written.
- **Contradicted** — a reliable public source affirmatively conflicts with the claim.
- **Not publicly substantiated** — the claim is on the site; supporting public evidence was not found. Not a finding that it is false.
- **Unable to verify** — the check needs contracts, unnamed facilities, certificates, or other nonpublic items.
- **Ambiguous** — wording, manager vs facility, or brand vs legal entity needs clarification.

Chain of custody is a process. The review also asks whether exception reports exist and how inventory discrepancies are closed.

If the vendor outsources processing, the review treats network management as the real claim: named vs unnamed partners, audit method, segregation of duties, verification tags, equipment holds, and independent verification of partner work. Coverage slogans are not a substitute.
            """
        )


if __name__ == "__main__":
    main()
