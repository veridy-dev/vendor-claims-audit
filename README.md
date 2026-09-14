# Vendor Claims Audit

Shareable screening app: a visitor enters a company name (optional website / LinkedIn) and gets a public-claims report covering sustainability, security/destruction, certifications, and liability wording.

This is a **preliminary screening aid**, not independent assurance and not a certified ITAD audit.

## Why this is not a single HTML file

The Claude prototype called Anthropic from the browser. That cannot work:

- no API key in a page you share
- browser CORS blocks `api.anthropic.com`
- web search is a server-side tool; one `fetch` does not run the search loop

This app keeps the report design and runs the search **on the server** through the xAI Responses API (`web_search` on Grok).

## What you need for a link you can send

1. An [xAI API key](https://console.x.ai/)
2. A GitHub repo containing this folder
3. A free [Streamlit Community Cloud](https://share.streamlit.io) account

Resulting URL looks like:

`https://vendor-claims-audit.streamlit.app`

Anyone with the link can run an audit. You pay xAI for tokens plus web-search tool calls.

## Deploy

```bash
# from this folder
git init
git add app.py requirements.txt README.md .gitignore .streamlit/config.toml
git commit -m "Vendor Claims Audit"
# create a GitHub repo, then:
git remote add origin git@github.com:YOU/vendor-claims-audit.git
git push -u origin main
```

Then:

1. Open [share.streamlit.io](https://share.streamlit.io) → **Create app**
2. Point it at the repo, branch `main`, file `app.py`
3. Optional: set the subdomain to `vendor-claims-audit`
4. **Advanced settings → Secrets** — paste:

```toml
XAI_API_KEY = "xai-..."
XAI_MODEL = "grok-4.6"
# Optional gate so the public link is not an open tab on your bill:
# APP_PASSWORD = "change-me"
```

5. Deploy. Share the `*.streamlit.app` URL.

If the repo is public, set the app to public under **Share**. If you want only named viewers, keep the app private or set `APP_PASSWORD`.

## Local run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# edit secrets.toml and add your key
streamlit run app.py
```

Without a key, the form still renders a **sample** report so you can check layout.

## Cost and limits

Each live audit uses Grok plus one or more server-side `web_search` calls. Check current xAI pricing before sending the link widely. Use `APP_PASSWORD` if you do not want strangers running audits on your key.

## What the report will not do

- It will not log into NAID / SERI / e-Stewards member portals.
- It will not treat a logo on a vendor homepage as a current facility listing.
- It will not replace chain-of-custody evidence or a Veridy-style outcome check.

Confirm anything material against the registry and the vendor.
