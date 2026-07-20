# Uniquely — Codex Handoff

This document records the working state of this repository so a new Codex session can continue without rediscovering the architecture.

## Project

**Uniquely** is a synthetic-data demo for automated accounts-receivable cash reconciliation. It accepts messy bank payments and an Open AR ledger, produces safe posting instructions, and records every decision in an append-only audit journal.

The central rule is deliberately conservative: GPT-5.6 may resolve ambiguous identity evidence and explain/rout a case, but it never creates invoice allocations or overrides a safety policy.

## Architecture: five-stage pipeline

`POST /analyze` in `backend/main.py` starts `run_pipeline()` in `backend/reconciliation.py`. The pipeline is an async generator; FastAPI streams its stage and posting events to the Next.js dashboard as Server-Sent Events (SSE).

1. **Normalize payments**
   - Deterministically normalizes currency, amounts (using `Decimal`), payer strings, and remittance fields.
   - GPT-5.6 performs payer/entity resolution only against the ledger-supplied entity catalog. It can judge truncated SWIFT names, DBA aliases, parent/subsidiary relationships, and factoring intermediaries, then returns a relationship, confidence, and short rationale.
2. **Index open AR**
   - Deterministically normalizes invoice amounts and payer names, then builds the entity/alias/relationship catalog from the ledger.
3. **Verify matching candidates**
   - Deterministic code verifies exact-reference, amount, partial-payment, multi-invoice, FIFO, early-discount, credit-memo-net, wire-fee-writeoff, PO/legacy-reference, and FX candidates.
   - All financial arithmetic uses Python `Decimal`; only verified allocations can be posted.
4. **Reason about exceptions**
   - GPT-5.6 receives the normalized payment, grounded entity result, deterministic amount facts, and verified candidates. It returns structured JSON: `auto_post`, `review`, `dispute`, or `compliance_hold`, plus a calibrated confidence score and analyst-readable rationale.
   - Deterministic policy then hard-blocks explicit compliance/legal holds, disputes, duplicate payments, NSF returns, post-dated checks, and stale checks.
5. **Create posting instructions**
   - The backend emits one posting instruction per payment and persists the event. Invoice IDs are included only for an allowed verified allocation.

### Safety split

| Responsibility | Implementation |
| --- | --- |
| Allocation totals, discounts, partials, multi-invoice sums, write-off thresholds, duplicate detection, FX calculations | Deterministic `Decimal`-based Python code |
| Truncated names, aliases/DBAs, parent payments, factoring, vague evidence, analyst rationale | GPT-5.6 via the standard OpenAI Python SDK |
| Compliance, legal, dispute, timing, and posting controls | Deterministic hard gates after the model response |

`enforce_auto_post_safety()` is the final posting guard: an `auto_post` recommendation becomes `review` unless a code-verified candidate has confidence of at least 0.95. This prevents the model from auto-posting a payment with no verified invoice allocation.

## Tech stack

- **Backend:** Python 3.11+, FastAPI, Uvicorn, async functions
- **Model:** standard `openai` Python SDK with `AsyncOpenAI`, calling `gpt-5.6`; `OPENAI_API_KEY` is loaded from `backend/.env`
- **Audit:** local SQLite at `backend/data/audit.sqlite3`; database triggers reject `UPDATE` and `DELETE` on `audit_events`
- **Frontend:** Next.js 14, React, plain CSS, SSE streaming
- **Data:** ten synthetic sample datasets in `backend/data/samples/sample_01` through `sample_10`; no real financial data

## Implemented and verified

- GPT-5.6 ledger-grounded entity resolution, including confidence calibration and per-payment analyst rationale.
- Deterministic allocation verification and routing safety gates; unresolved/no-match payments cannot be auto-posted.
- Coverage data and handling paths for the 34 enumerated AR edge cases across amount, identity, multi-entity, timing, remittance, FX, and compliance/legal categories. Note: the original request called this 35 cases, but its category counts total 34.
- Deterministic compliance/dispute/legal blocking, duplicate detection, post-dated/stale-check controls, partial invoice matching, and FX arithmetic/facts.
- Append-only SQLite audit events for normalization, ledger indexing, matching, routing, and posting.
- Dashboard stage badges, SSE progress updates, responsive posting-result grid, visible entity confidence/rationale, and side-by-side Bank Payments/Open AR ledger panels.
- Default dashboard sample is **sample 04**. It has the strongest mix of factoring, DBA/relationship, dispute/compliance, and ambiguous scenarios. Add `?sample=NN` to the dashboard URL to load another sample.
- Backend unit tests were last run successfully with 9 passing tests, including auto-post safety, candidate strategies, policy hard gates, duplicate handling, and FX facts. The frontend production build was also last run successfully.

## Known issues and cleanup backlog

1. **Uncommitted work is present now.** Do not discard it: it contains ongoing edge-case coverage work.
   - `backend/reconciliation.py`
   - `backend/tests/test_reconciliation.py`
   - `backend/data/samples/sample_05/bank_statement.json`
   - `backend/scripts/generate_samples.py`
   The generator has a staged/unstaged split: its branding docstring was committed, while the sample-05 FX-note correction remains unstaged.
2. **Naming is inconsistent.** The chosen project name is Uniquely, but `backend/main.py` and the frontend header still display/API-label the older “AR Reconciliation Copilot” name. Align these in a dedicated naming pass.
3. **README sample-default claim is stale.** `README.md` says the dashboard loads sample 01; the frontend actually loads sample 04.
4. **Legacy documentation needs replacement.** Files under `docs/` are not the current source of truth and contain obsolete architecture/deployment descriptions. A fresh `docs/ARCHITECTURE.md` should supersede them.
5. A live all-scenario GPT-5.6 run has not been repeated after the current uncommitted edge-case expansion. Unit tests pass, but do not claim a full live 34-scenario model verification until that run is performed.
6. A demo video and final README polish are not yet done.
7. A case-insensitive working-tree scan (excluding Git metadata and dependency directories) has zero occurrences of the prior project branding. Historical Git commit messages naturally still preserve old text inside `.git`.

## Key files

| File | Responsibility |
| --- | --- |
| `backend/main.py` | FastAPI app, CORS, sample-data endpoints, `/analyze` SSE endpoint, `.env` loading |
| `backend/reconciliation.py` | All five pipeline stages, OpenAI calls, deterministic matching/policy, audit log, safety gate |
| `backend/tests/test_reconciliation.py` | Regression tests for allocation, policy, safety, duplicate, and FX behavior |
| `backend/data/samples/sample_*/` | Synthetic bank statement, Open AR ledger, and sample metadata |
| `backend/scripts/generate_samples.py` | Deterministic generator for the synthetic samples |
| `frontend/app/page.js` | Dashboard state, SSE client, tables, stage indicators, output cards |
| `frontend/app/globals.css` | Responsive dashboard visual system and transitions |
| `README.md` | Concise project overview and Quick Start; update its sample-default statement during polish |

## Local runbook

Prerequisites: Python 3.11+, Node.js 18+, and an OpenAI API key for live GPT-5.6 reasoning.

Start the backend from a fresh clone:

```bash
git clone https://github.com/vinaygangidi/cash-reconciliation-codex.git
cd cash-reconciliation-codex/backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env and set OPENAI_API_KEY=...
uvicorn main:app --reload --port 8000
```

In a second terminal, start the frontend:

```bash
cd cash-reconciliation-codex/frontend
npm install
NEXT_PUBLIC_API_URL=http://localhost:8000 npm run dev
```

Open <http://localhost:3000>. The frontend uses sample 04 unless a `?sample=NN` URL parameter is supplied.

Without `OPENAI_API_KEY`, deterministic cases still run; model-assisted identity/routing falls back safely to review. With a key, use the dashboard’s **Run synthetic demo** button or `POST /analyze` to stream the full pipeline. Inspect a completed audit trail at `GET /audit/{run_id}`.

## Suggested next session sequence

1. Inspect and commit the existing uncommitted edge-case changes after a test review.
2. Run the full live GPT-5.6 path against representative samples and document results.
3. Align product naming and correct the README sample-default statement.
4. Write `docs/ARCHITECTURE.md` from the actual code, then retire or clearly label obsolete docs.
5. Record the demo video and complete final README polish.
