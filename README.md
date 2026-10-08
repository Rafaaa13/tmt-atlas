# TMT Atlas｜Local-first TMT research workbench

> Evidence first · Local first · LLM optional · Not investment advice

TMT Atlas is a local-first workbench for TMT research. It turns public-source links and user-selected local materials into a reviewable chain of evidence, facts, deterministic financial calculations, sector comparisons, event analysis, and research memo exports.

It is intentionally not an auto-trading bot, an investment-advice system, or a claim of real-time full-market coverage.

## Run it in under one minute

Requirements: Python 3.10+; no Docker, Node, database server, or API key required for the core workflow.

```bash
cd TMT-金融Agent
python3 server.py
```

Open <http://127.0.0.1:8765>.

Or:

```bash
./run.sh
```

Run tests:

```bash
python3 -m unittest discover -s tests -v
```

## A fast product tour

Use the top-level navigation to try the following:

1. `公司快照` shows a company profile, illustrative demo metrics, data status, and source entry points.
2. `赛道扫描` shows a cross-company matrix while explicitly warning that periods, accounting bases, units, and segment definitions still require review.
3. `事件雷达` separates direct facts, possible transmission paths, inferences, and open questions.
4. `证据审核` imports a local PDF/CSV/JSON/Markdown/TXT file into SQLite evidence chunks. CSV rows matching the fact schema create `pending` facts only.
5. `金融计算` runs auditable growth, margin, FCF, enterprise-value, multiples, and simplified DCF calculations locally, with formulas and warnings.
6. `研究备忘录` creates Markdown/HTML drafts from the selected demo objects.
7. `授权收集` configures an explicit RSS/Atom/JSON Feed allowlist. The collector is disabled by default and stages metadata in a review inbox.

## The core workflow

```text
Research question
  → source or local file
  → evidence with page/line/row locator
  → pending fact
  → human review and audit trail
  → deterministic calculation
  → sector/event analysis
  → memo draft and export
```

The local evidence path does not call an LLM. The optional LLM path only runs after the user explicitly enables it and selects evidence in the UI. It uses an OpenAI-compatible `/chat/completions` endpoint and reads `TMT_LLM_API_KEY` from the process environment; the key is never saved to local settings.

## Current demo scope

The bundled illustrative workspace covers nine companies across three TMT themes:

- AI infrastructure / semiconductors: NVIDIA and SMIC.
- Cloud / SaaS: Microsoft, Alibaba, and Baidu.
- Platform / e-commerce / advertising: Amazon, Alphabet, Meta, and Tencent.

`data/demo_seed.json` is explicitly marked `illustrative_demo`. Its normalized values are for testing the UI and workflow; they are not live or investment-grade data. The product shows that status throughout the UI and memo output.

The separate `data/verified_facts.json` contains a small manually curated official-source corpus for Baidu FY2024, Alibaba FY2025, and Meta FY2024. It is deliberately not merged into the demo metrics. Read `docs/DATA-SOURCES.md` and `docs/IMPLEMENTATION-STATUS.md` before using it.

## Local evidence ingestion

The evidence review screen accepts PDF, CSV, JSON, Markdown, and TXT files. The server accepts file bytes rather than a caller-supplied filesystem path. It enforces a 12 MiB size cap, SHA-256 deduplication, safe generated storage names, and evidence locators.

For PDFs, the optional local stack uses a text layer when available. If OCR is requested, it checks installed Tesseract languages, uses `eng` or `chi_sim` when available, bounds pages and execution time, and marks OCR output for human verification. No OCR result is treated as a financial fact automatically.

A fact CSV must contain:

```text
company_id,metric_key,value,unit,currency,period_start,period_end,period_type,accounting_basis,scope
```

The bundled template is `data/example-facts.csv`; it is labeled `DEMO_ONLY`. Import creates `pending` facts linked to CSV row evidence. Use the review table to accept or reject them and supply a reason.

## Deterministic finance engine

Available operations through `POST /api/calculate` and the `金融计算` page:

- `growth`: conventional `current / previous - 1`; zero or negative prior periods are blocked rather than silently normalized.
- `margin`: numerator / positive denominator.
- `cagr`: positive start, non-negative end, bounded period count.
- `unit_conversion`: same-currency unit conversion only.
- `fcf`: CFO minus positive CapEx outflow.
- `enterprise_value`: market cap + debt + preferred equity + minority interest − cash.
- `ev_revenue` / `pe` and generic multiples.
- `dcf`: simplified educational model with explicit growth, WACC, terminal growth, years, net debt, shares, scenarios, and sensitivity grid.
- `comparability`: checks metric, unit, currency, period, accounting basis, and scope; currency independence requires an explicit flag.

Every result includes `operation`, `value`, `unit`, `formula`, `inputs`, `warnings`, and `status`. DCF output is a mechanical scenario calculation, not a price target or recommendation.

## Authorized collection

The collector supports RSS, Atom, and JSON Feed metadata only. It is disabled by default. Enabling it requires both a global authorization flag and per-feed authorization. It rejects non-HTTPS URLs, credentials, fragments, redirects, private IP targets, unsafe XML entities, oversized responses, and more than five feeds per run. It stores title/date/link/source metadata in a review inbox and never marks facts approved.

The app must remain running for the in-process interval tick. This is a local beta scheduler, not a hosted background service.

## Optional LLM setup

1. Open `来源与设置`.
2. Enter an OpenAI-compatible Base URL and model.
3. Set `TMT_LLM_API_KEY` in the terminal that starts the server.
4. Enable the external-send toggle.
5. Go to `证据审核`, select the evidence rows, review the consent dialog, and confirm.

If the model is unavailable or disabled, local evidence, calculation, review, and memo paths remain available.

## API quick reference

| Endpoint | Purpose |
|---|---|
| `GET /api/health` | Local health check |
| `GET /api/workspace` | Sources, evidence, facts, audit, stats |
| `POST /api/ingest` | Strict base64 local-file upload |
| `GET /api/evidence?q=` | Evidence search |
| `POST /api/facts` | Propose a pending fact |
| `POST /api/facts/review` | Accept/reject with a reason |
| `GET /api/verified-facts` | Separate curated official-source corpus |
| `POST /api/calculate` | Deterministic finance engine |
| `GET/POST /api/collection/*` | Authorized collector and review inbox |
| `POST /api/llm/draft` | Optional selected-evidence draft |

## Repository map

```text
TMT-金融Agent/
├── server.py                 # local HTTP API + static server
├── evidence_engine.py        # local PDF/CSV/JSON/text extraction
├── workspace_db.py           # SQLite sources/evidence/facts/audit
├── finance_engine.py         # deterministic financial calculations
├── connectors.py             # authorized RSS/Atom/JSON collector
├── data/
│   ├── demo_seed.json        # clearly illustrative demo workspace
│   ├── verified_facts.json   # separate curated official facts
│   ├── source_registry.json  # official source entry points
│   └── example-facts.csv     # DEMO_ONLY import template
├── frontend/
│   ├── app.js                # main workbench
│   ├── workbench.js          # evidence/finance/automation screens
│   ├── styles.css
│   └── workbench.css
├── tests/                    # 82+ local unit/integration tests
└── docs/                     # PRD, architecture, evaluation, interview kit
```

## What is implemented versus planned

See [`docs/IMPLEMENTATION-STATUS.md`](docs/IMPLEMENTATION-STATUS.md) for the honest boundary. In particular, the project does not claim real-time full-market coverage, production high availability, external user metrics, investment performance, or fully autonomous multi-agent research.

## Interview-ready framing

> I built TMT Atlas as a local-first TMT research workbench rather than a finance chatbot. The user can import a local filing, inspect page/line evidence, propose a pending fact, review it with an audit trail, run deterministic financial calculations, compare companies, analyze events, and export a memo without an LLM. If the user explicitly enables an OpenAI-compatible model, it can draft language from selected evidence only. The main product decision was to keep financial semantics and approval boundaries outside the model.

Detailed STAR answers, follow-up questions, limitations, and resume versions are in [`docs/INTERVIEW-GUIDE.md`](docs/INTERVIEW-GUIDE.md) and [`docs/RESUME-VERSIONS.md`](docs/RESUME-VERSIONS.md).

## Data and license boundary

The code is MIT-licensed. Public filings, company IR pages, RSS feeds, and third-party materials remain subject to their own terms. The repository stores source links and small curated facts, not a redistribution of full restricted reports. Do not commit private internship materials, credentials, personal information, or generated local state.

TMT Atlas is a research assistance tool, not financial advice.
