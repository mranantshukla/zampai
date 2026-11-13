# ASA Invoice Intelligence

An accounts payable case study that reads PDF or image bills, extracts invoice fields, checks vendors and purchase orders, reconciles amounts, detects duplicates, and records `APPROVED`, `REJECTED`, or `PENDING_REVIEW`. Document approval does not authorise payment.

## Architecture

- `asa_invoice_processor.py`: embedded PDF text, scanned-page rendering, RapidOCR, classification, extraction, GSTIN and financial checks.
- `decision_policy.py`: deterministic three-outcome policy and configurable PO tolerance/OCR evidence grade.
- `app.py`: Flask dashboard, live stage stream, preview, history, review and sample evaluation.
- `storage.py`: SQLAlchemy persistence for document bytes, runs, stages and reviews. PostgreSQL on Render; SQLite locally.
- Successful OCR text is cached in the same database by document hash and OCR settings. Repeated uploads reuse OCR but always rerun vendor, PO, duplicate and financial decisions. Failed OCR is never cached.
- `asa_dashboard.py`: preserved legacy `Handler` for compatibility tests. Running the file starts the new Flask dashboard. `backend/` is an unused older prototype.

The AjitRawat original images are in `dataset_images/`. `dataset_cache.json` holds public reference labels. Labels enter only `evaluate_dataset()` after OCR extraction and the AP decision. They never populate production extraction or decisions.

## Run locally

Use Python 3.12. From this directory:

```powershell
# Use the local SQLite database if a placeholder PostgreSQL URL is set in this shell.
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
python -m pip install -r requirements.txt
python asa_dashboard.py --port 8766
```

Open http://127.0.0.1:8766/. The local database is `asa_local.db`. Set `DATABASE_URL` for PostgreSQL. CLI compatibility remains:

```powershell
python asa_invoice_processor.py --invoice samples/invoice_happy.txt --po-data samples/pos.csv --history samples/history.json
```

The old text sample lacks subtotal and routes to review under the current policy. Dashboard uploads accept valid PDF, PNG and JPEG up to 8 MB.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string. Defaults to local SQLite. |
| `APP_SECRET` | Flask signing secret, required on Render. |
| `DEMO_USERNAME`, `DEMO_PASSWORD` | Demo login, required on Render. |
| `PO_TOLERANCE_PCT` | Fractional overage allowance, default `0`, maximum `0.05`. |
| `OCR_MIN_RELIABILITY` | `DIRECT_LABEL` (default) or `MANUALLY_VERIFIED`; ordinal evidence grade, not calibrated confidence. |
| `PORT` | Production port from Render; local command accepts `--port`. |
| `AI_REVIEW_API_URL`, `AI_REVIEW_MODEL`, `AI_REVIEW_API_KEY` | Persistent AI provider configuration via server environment. The dashboard settings form only changes the running process. |

## Decision policy

Confirmed violations outrank uncertainty. Missing data is blocked or uncertain, never a pass. An approved or paid invoice with the same normalized vendor and invoice number is a business duplicate and is rejected. Repeated pending submissions remain pending with a separate warning. Partial invoices consume PO balance cumulatively. OCR does not automatically force review: critical OCR fields need direct source-label evidence and checks must pass. This structural evidence check is not a calibrated probability or proof of extraction correctness.

Non-tax-invoice documents receive a separate routing status (`NON_INVOICE`) and bypass AI invoice reconciliation. They remain visible in history but are excluded from invoice approval and exception-rate metrics. The run monitor streams database-backed events. Manual corrections are separate from original automated decisions. “Clear history” archives the visible list while retaining duplicate and PO audit records.

Completed runs, stage history, reviews and OCR cache survive server restarts through the database. Login sessions use signed cookies valid for seven days when `APP_SECRET` is stable. A server restart loses AI settings entered only in the dashboard; set the environment variables above for persistent settings. The run trace shows provider HTTP errors without exposing the configured API key.

## Demo and tests

Run `python make_demo_assets.py` to recreate six fictional files in `samples/demo/`. Process in filename order against empty history: 2 approved, 2 rejected, 2 pending. The scanned file uses PO-1004 independently of the first PDF. See `DEMO_SCRIPT.md`.

```powershell
python -m pip install pytest
python -m pytest -q test_regressions.py test_app.py
```

Optional, idempotent legacy JSON import: set `DATABASE_URL`, then run `python migrate_legacy.py`. It reads `run_history.json` and `manual_verification.json` without deleting them. Legacy checks retain no invented reviewer identity.

## Render

`render.yaml` defines a Python web service, PostgreSQL, `/healthz`, generated secret, and demo login variables. Connect this repository through Render Blueprint, supply `DEMO_USERNAME` and `DEMO_PASSWORD`, and test the resulting URL. See `DEPLOYMENT.md`.

## Known limits

The workspace supports batch intake (up to 25 files per request), an operational Inbox, searchable records, Excel and CSV exports, vendor summaries derived from run history, a dedicated exception-review workspace, and review notes for selected invoices. A batch review preserves the automated decision and records the supplied reviewer identity and note. The interface sections share the Flask application; they are not separate server deployments. Email ingestion, accounting sync, scheduled notifications, team roles, and PDF coordinate highlighting are not implemented; they require provider credentials, account mapping, and document location evidence. Vendor summaries do not replace vendor-master approval.

The heuristic extractor remains weak on real bills: the current 22-image AjitRawat benchmark is **30/90 exact comparable fields (33.3%)**. OCR evidence grades are structural, not calibrated confidence. GST checks do not establish tax-law compliance. Interrupted runs are marked `FAILED` after a worker restart. Schema version 1 is initialized additively; future changes need a migration framework. Render free web services spin down and free Postgres expires after 30 days.

First-time OCR can still be slow on CPU-limited hosting. On one local sample (`invoice_07.jpg`), recognition batch size 1 took 3.3–3.4 seconds and batch size 6 took 5.8–5.9 seconds, so batch size 1 remains the default. These local timings do not predict Render latency. Exact-document cache hits avoid OCR on subsequent runs; cold documents still require OCR.

The live monitor checks for stage updates once per second for up to 15 minutes. A slow OCR run can therefore finish while the monitor is still connected, with less database polling than before.
