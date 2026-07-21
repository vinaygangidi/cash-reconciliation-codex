"""Audit live Ledger Sense sample decisions for safety and UI consistency.

Run against a deployed backend (or a local uvicorn server):
    python scripts/audit_samples.py --base-url http://localhost:8000

The script intentionally invokes each synthetic payment independently, in
parallel, so it can complete within constrained CI and shell environments.
"""

import argparse
import concurrent.futures
import json
import re
import sys
import urllib.request
from datetime import date
from decimal import Decimal
from pathlib import Path

# Allow `python scripts/audit_samples.py` from the backend directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from reconciliation import amount, candidates, deterministic_policy, name

DEFAULT_BASE_URL = "https://cash-reconciliation-codex-production.up.railway.app"


def decimal(value):
    return Decimal(str(value))


def fetch_json(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        return json.load(response)


def run_transaction(base_url, data, transaction):
    bank = {**data["bank_statement"], "transactions": [transaction]}
    request = urllib.request.Request(
        f"{base_url}/analyze",
        data=json.dumps({"bank_data": bank, "ar_data": data["open_ar"]}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        for line in response:
            event_line = line.decode().strip()
            if not event_line.startswith("data: ") or event_line == "data: [DONE]":
                continue
            event = json.loads(event_line[6:])
            if event.get("stage") == "posting":
                return event["output"]
    raise RuntimeError(f"No posting event for {transaction['txn_id']}")


def run_sample(base_url, number):
    data = fetch_json(f"{base_url}/demo-data?sample={number:02d}")
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        postings = list(
            executor.map(
                lambda transaction: run_transaction(base_url, data, transaction),
                data["bank_statement"]["transactions"],
            )
        )
    return number, data, postings


def audit_sample(number, data, postings):
    """Return failures only; deterministic source data is the ground truth."""
    failures = []
    bank, ledger = data["bank_statement"], data["open_ar"]
    transactions = bank["transactions"]
    by_id = {transaction["txn_id"]: transaction for transaction in transactions}
    invoices = [
        {
            **invoice,
            "open_amount": amount(invoice["open_amount"]),
            "currency": invoice.get("currency", "USD"),
            "payer": name(invoice["customer_name"]),
        }
        for invoice in ledger["invoices"]
    ]
    seen = set()
    if len(postings) != len(transactions):
        failures.append((number, "ALL", f"received {len(postings)} posting events for {len(transactions)} transactions"))

    for output in postings:
        transaction = by_id[output["transaction_id"]]
        entity = output.get("entity_resolution", {})
        resolved = entity.get("resolved_entity")
        entity_confidence = decimal(entity.get("confidence", 0))
        entity_rationale = (entity.get("rationale") or "").lower()
        unresolved = not resolved or entity.get("relationship") == "unresolved"
        resolution_terms = (
            "exactly matches", "resolved to", "documented alias",
            "parent payer for", "factoring agent for", "alias registry documents",
        )
        if unresolved and entity_confidence > decimal(".05"):
            failures.append((number, transaction["txn_id"], f"entity badge is unresolved at {entity_confidence} confidence"))
        if unresolved and any(term in entity_rationale for term in resolution_terms):
            failures.append((number, transaction["txn_id"], "unresolved entity badge contradicts entity rationale"))
        if not unresolved and entity_confidence <= decimal(".05"):
            failures.append((number, transaction["txn_id"], f"resolved entity {resolved!r} has unresolved-level confidence {entity_confidence}"))

        payment = {
            **transaction,
            "statement_date": bank["statement_date"],
            "amount": amount(transaction["amount"]),
            "currency": transaction.get("currency", "USD"),
            "payer": name(transaction.get("payer_raw", "")),
            "remittance": transaction.get("remittance_text", ""),
        }
        if resolved:
            payment["payer"] = name(resolved)
        verified = candidates(payment, invoices, ledger)
        policy = deterministic_policy(payment, invoices, ledger, seen)
        route = output.get("route")
        confidence = decimal(output.get("confidence", 0))
        rationale = output.get("reason", "")

        if not decimal(0) <= confidence <= decimal(1):
            failures.append((number, transaction["txn_id"], f"invalid route confidence {confidence}"))
        if policy and (route != policy["route"] or confidence != decimal(1)):
            failures.append((number, transaction["txn_id"], f"route/confidence {route}/{confidence} conflicts with deterministic policy {policy['route']}/1.00"))
        if route == "auto_post":
            if not verified:
                failures.append((number, transaction["txn_id"], "unsafe auto_post without a verified invoice match"))
            else:
                _, group, verified_confidence = verified[0]
                expected_ids = [invoice["invoice_id"] for invoice in group]
                if output.get("invoice_ids") != expected_ids:
                    failures.append((number, transaction["txn_id"], f"auto_post invoice IDs {output.get('invoice_ids')} differ from verified {expected_ids}"))
                if decimal(verified_confidence) < decimal(".95") or confidence < decimal(".95"):
                    failures.append((number, transaction["txn_id"], f"unsafe auto_post below .95 verification/confidence ({verified_confidence}/{confidence})"))
                strategy = verified[0][0]
                exact_strategies = {"exact_reference", "amount_match", "multi_invoice", "fifo_amount_match", "fx_verified"}
                if strategy in exact_strategies and sum((invoice["open_amount"] for invoice in group), Decimal(0)) != payment["amount"]:
                    failures.append((number, transaction["txn_id"], "auto_post invoice total does not equal payment amount"))
        elif not policy and (not verified or decimal(verified[0][2]) < decimal(".95")) and route not in ("review", "dispute", "compliance_hold"):
            failures.append((number, transaction["txn_id"], f"low-evidence route {route}"))

        candidate_confidences = {decimal(candidate[2]) for candidate in verified}
        for cited in re.findall(r"(?<![\d.])(0\.\d+|1\.0+)\s+confidence", rationale):
            if decimal(cited) not in candidate_confidences:
                failures.append((number, transaction["txn_id"], f"rationale cites unsupported candidate confidence {cited}"))
        if "post-dated" in rationale.lower() and not (payment["payment_type"] == "CHECK" and date.fromisoformat(payment["date"]) > date.fromisoformat(payment["statement_date"])):
            failures.append((number, transaction["txn_id"], "post-dated date citation conflicts with source data"))
        if "stale check exceeds the 180-day" in rationale.lower() and (date.fromisoformat(payment["statement_date"]) - date.fromisoformat(payment["date"])).days <= 180:
            failures.append((number, transaction["txn_id"], "180-day threshold citation conflicts with source data"))

        source_amounts = {payment["amount"]} | {invoice["open_amount"] for invoice in invoices}
        for invoice in invoices:
            discount = decimal(invoice.get("discount_pct", 0)) / decimal(100)
            if discount:
                source_amounts.add((invoice["open_amount"] * discount).quantize(decimal(".01")))
            source_amounts.add(abs(invoice["open_amount"] - payment["amount"]))
        for text in (transaction.get("note", ""), transaction.get("remittance_text", "")):
            source_amounts.update(decimal(value.replace(",", "")) for value in re.findall(r"\$([0-9][0-9,]*(?:\.\d{1,2})?)", text))
        for cited in re.findall(r"\$([0-9][0-9,]*(?:\.\d{1,2})?)", rationale):
            if decimal(cited.replace(",", "")) not in source_amounts:
                failures.append((number, transaction["txn_id"], f"rationale cites unsupported amount ${cited}"))
    return failures


def main():
    parser = argparse.ArgumentParser(description="Audit live sample pipeline decisions.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--samples", nargs="*", type=int, default=list(range(1, 11)))
    args = parser.parse_args()
    base_url = args.base_url.rstrip("/")

    failures, checked = [], 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(run_sample, base_url, number) for number in args.samples]
        for future in concurrent.futures.as_completed(futures):
            number, data, postings = future.result()
            checked += len(data["bank_statement"]["transactions"])
            failures.extend(audit_sample(number, data, postings))

    for number, transaction, detail in failures:
        print(f"FAIL sample_{number:02d}/{transaction}: {detail}")
    print(f"CHECKED {checked} transactions across {len(args.samples)} samples; FAILURES {len(failures)}")
    raise SystemExit(1 if failures else 0)


if __name__ == "__main__":
    main()
