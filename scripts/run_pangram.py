"""
Score the extracted Arizona candidate statements with the Pangram bulk API.

Flow (Pangram v3+ async API, https://docs.pangram.com):
    POST /bulk                  -> bulk_id
    GET  /bulk/{bulk_id}        -> poll until status is terminal
    GET  /bulk/{bulk_id}/results -> paginated per-item results

All 198 statements fit in one bulk request (402 billable units, limit 1000),
so there is a single job to submit and poll.

The script is idempotent and crash-safe:
  * the bulk_id is written to disk immediately after submission, so an
    interrupted poll resumes against the same job instead of paying twice;
  * once data/pangram_raw.json exists nothing is resubmitted (use --force).

Results are only retained by Pangram for 48 hours, so raw responses are written
to disk before any processing.

API key is read from $PANGRAM_API_KEY, else ~/.config/pangram/api_key.
Stdlib only - no virtualenv needed.

Usage:
    python3 run_pangram.py --dry-run     # validate + cost estimate, no network
    python3 run_pangram.py               # submit, poll, write results
    python3 run_pangram.py --force       # resubmit even if results exist
    python3 run_pangram.py --dataset control_2022 [--dry-run]
                                         # 2022 negative control, own folder
"""

import argparse
import csv
import json
import math
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

os.chdir(os.path.dirname(os.path.abspath(__file__)))

BASE = "https://text.external-api.pangram.com"
MODEL = "pangram-4"
WORDS_PER_UNIT = 100          # Pangram 4 billable block; Pangram 3 is 1000
PRICE_PER_UNIT_BULK = 0.04    # $0.05/unit less the 20% bulk discount

# Each dataset keeps its inputs, receipt and results in its own data folder,
# so a control run can never overwrite or "resume" the 2026 run of record.
DATASETS = {
    "2026": "../data",                      # run of record, Sept 20 2026 (model 4.0)
    "control_2022": "../data/control_2022", # pre-ChatGPT negative control
    # 2026 statements rescored on the model version the control got (4.1);
    # "pangram-4" floated from 4.0 to 4.1 between Sept and Oct 2026.
    "2026_rescore": "../data/rescore_2026",
}


def set_dataset(name):
    global IN_JSON, RAW_JSON, BULK_ID_FILE, OUT_JSON, OUT_CSV
    d = DATASETS[name]
    IN_JSON = f"{d}/candidate_statements.json"
    RAW_JSON = f"{d}/pangram_raw.json"
    BULK_ID_FILE = f"{d}/pangram_bulk_id.json"
    OUT_JSON = f"{d}/candidate_statements_scored.json"
    OUT_CSV = f"{d}/candidate_statements_scored.csv"


set_dataset("2026")

KEY_FILE = os.path.expanduser("~/.config/pangram/api_key")

POLL_SECONDS = 10
POLL_TIMEOUT = 3600
TERMINAL = {"succeeded", "failed", "partial"}
RETRY_STATUS = {429, 500, 502, 503, 504}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def api_key():
    key = os.environ.get("PANGRAM_API_KEY", "").strip()
    if not key and os.path.exists(KEY_FILE):
        with open(KEY_FILE) as f:
            key = f.read().strip()
    if not key:
        sys.exit(
            "No API key. Set $PANGRAM_API_KEY or write it to " + KEY_FILE
        )
    return key


def request(method, path, key, body=None, params=None, retries=5):
    """One API call, with backoff on rate limits and transient failures."""
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    ctx = ssl.create_default_context()

    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("x-api-key", key)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            # Never retry a client error we cannot fix by waiting - especially
            # 402 (out of credits) and 413 (batch too large).
            if e.code not in RETRY_STATUS or attempt == retries:
                sys.exit(f"HTTP {e.code} on {method} {path}: {detail}")
            print(f"  HTTP {e.code}, retrying in {delay:.0f}s ({detail[:120]})")
        except (urllib.error.URLError, TimeoutError, ssl.SSLError) as e:
            if attempt == retries:
                sys.exit(f"Network failure on {method} {path}: {e}")
            print(f"  network error, retrying in {delay:.0f}s ({e})")
        time.sleep(delay)
        delay = min(delay * 2, 60)


# --------------------------------------------------------------------------- #
# Job
# --------------------------------------------------------------------------- #

def load_items():
    with open(IN_JSON) as f:
        records = json.load(f)
    scored = [r for r in records if r["statement"].strip()]
    items = [{"id": r["candidate_id"], "text": r["statement"]} for r in scored]
    return records, items


def estimate(items):
    units = sum(
        max(1, math.ceil(len(i["text"].split()) / WORDS_PER_UNIT)) for i in items
    )
    return units, units * PRICE_PER_UNIT_BULK


def submit(items, key):
    print(f"Submitting {len(items)} statements as one bulk job ({MODEL})...")
    resp = request("POST", "/bulk", key, body={"items": items, "model": MODEL})
    bulk_id = resp.get("bulk_id")
    if not bulk_id:
        sys.exit(f"No bulk_id in response: {json.dumps(resp)[:500]}")

    # Persist before anything else can fail - this is the receipt for a job
    # that has already been billed.
    with open(BULK_ID_FILE, "w") as f:
        json.dump({"bulk_id": bulk_id, "model": MODEL,
                   "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                   "submit_response": resp}, f, indent=2)

    failed = resp.get("failed_items") or []
    print(f"  bulk_id {bulk_id}  accepted={resp.get('accepted_items')} "
          f"failed={len(failed)}")
    if failed:
        print("  rejected at submit:", json.dumps(failed)[:500])
    return bulk_id


def poll(bulk_id, key):
    print("Polling...")
    waited = 0
    while True:
        s = request("GET", f"/bulk/{bulk_id}", key)
        status = (s.get("status") or "").lower()
        print(f"  [{waited:>4}s] {status}  "
              f"succeeded={s.get('succeeded')} failed={s.get('failed')} "
              f"of {s.get('total_items')}")
        if status in TERMINAL:
            return s
        if waited >= POLL_TIMEOUT:
            sys.exit(f"Timed out after {POLL_TIMEOUT}s. Job {bulk_id} may still "
                     f"finish - rerun to resume (results expire 48h).")
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def fetch_results(bulk_id, key, expected):
    print("Fetching results...")
    items, failed, offset = [], [], 0
    while True:
        page = request("GET", f"/bulk/{bulk_id}/results", key,
                       params={"offset": offset, "limit": 1000})
        batch = page.get("items") or []
        items.extend(batch)
        failed.extend(page.get("failed_items") or [])
        total = page.get("total_items")
        offset += len(batch)
        print(f"  {len(items)} / {total}")
        if not batch or (total is not None and offset >= total):
            break
    if expected is not None and len(items) != expected:
        print(f"  WARN: expected {expected} items, got {len(items)}")
    return items, failed


# --------------------------------------------------------------------------- #
# Join and write
# --------------------------------------------------------------------------- #

FLAT = ["prediction_short", "fraction_ai", "fraction_ai_assisted",
        "fraction_human", "headline", "prediction", "num_ai_segments",
        "num_ai_assisted_segments", "num_human_segments", "version"]


def join(records, items):
    by_id = {}
    for it in items:
        if it.get("id"):
            by_id[it["id"]] = it

    out = []
    for r in records:
        rec = dict(r)
        it = by_id.get(r["candidate_id"])
        if it is None:
            rec["pangram_status"] = "not_submitted" if not r["statement"].strip() \
                else "missing"
            out.append(rec)
            continue

        rec["pangram_status"] = (it.get("stage") or "").lower()
        rec["pangram_error"] = it.get("error")
        result = it.get("result") or {}
        for k in FLAT:
            rec[f"pangram_{k}"] = result.get(k)
        # Window detail is large; keep it in the JSON only.
        rec["pangram_windows"] = result.get("windows") or []
        out.append(rec)
    return out


def write(joined):
    with open(OUT_JSON, "w") as f:
        json.dump(joined, f, indent=2, ensure_ascii=False)

    cols = ["candidate_id", "name", "party", "funding", "office", "district",
            "office_full", "pdf_page", "statement_word_count", "website",
            "pangram_status"] + [f"pangram_{k}" for k in FLAT] + ["statement"]
    with open(OUT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(joined)
    print(f"\nWrote {OUT_JSON}\n      {OUT_CSV}")


def summarize(joined):
    scored = [r for r in joined if r.get("pangram_prediction_short")]
    print(f"\nScored {len(scored)} statements.")
    counts = {}
    for r in scored:
        counts[r["pangram_prediction_short"]] = \
            counts.get(r["pangram_prediction_short"], 0) + 1
    for k, v in sorted(counts.items(), key=lambda x: -x[1]):
        print(f"  {k:<8} {v:>4}  ({v / max(len(scored), 1):.1%})")

    by_party = {}
    for r in scored:
        d = by_party.setdefault(r["party"], [0, 0])
        d[0] += 1
        if r["pangram_prediction_short"] in ("AI", "Mixed"):
            d[1] += 1
    print("\n  AI or Mixed, by party:")
    for party, (n, flagged) in sorted(by_party.items(), key=lambda x: -x[1][0]):
        print(f"    {party:<12} {flagged:>3} / {n:<3} ({flagged / n:.1%})")

    print("\n  NOTE: statements are 75-214 words. Aggregate rates are the "
          "reliable signal;\n  individual calls, especially under 100 words, "
          "are weaker evidence.")


def main():
    global MODEL
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="validate payload and estimate cost, no API calls")
    ap.add_argument("--force", action="store_true",
                    help="resubmit even if raw results already exist")
    ap.add_argument("--dataset", choices=DATASETS, default="2026",
                    help="which extracted corpus to score (default: 2026)")
    ap.add_argument("--model", default=MODEL,
                    help=f"Pangram model selector (default: {MODEL}); GET /models lists them")
    args = ap.parse_args()
    set_dataset(args.dataset)
    MODEL = args.model
    print(f"dataset: {args.dataset}  ({IN_JSON})  model: {MODEL}")

    records, items = load_items()
    units, cost = estimate(items)
    print(f"{len(records)} candidates, {len(items)} with statements")
    print(f"{units} billable units @ ${PRICE_PER_UNIT_BULK}/unit "
          f"= ${cost:,.2f} estimated ({MODEL}, bulk)")
    if units > 1000:
        sys.exit("Over the 1000-unit bulk limit - batching needed.")

    if args.dry_run:
        print("\n--dry-run: nothing submitted. Sample payload item:")
        s = dict(items[0])
        s["text"] = s["text"][:120] + "..."
        print(json.dumps(s, indent=2, ensure_ascii=False))
        return

    # Reuse rather than pay twice. Checked before the key is needed, so the
    # results can be re-joined after the key has been removed from the machine.
    if os.path.exists(RAW_JSON) and not args.force:
        print(f"\n{RAW_JSON} exists - reusing it (--force to resubmit).")
        with open(RAW_JSON) as f:
            raw = json.load(f)
        result_items = raw["items"]
    else:
        key = api_key()
        bulk_id = None
        if os.path.exists(BULK_ID_FILE) and not args.force:
            with open(BULK_ID_FILE) as f:
                bulk_id = json.load(f).get("bulk_id")
            print(f"\nResuming existing job {bulk_id} (--force for a new one).")
        if bulk_id is None:
            bulk_id = submit(items, key)

        status = poll(bulk_id, key)
        if (status.get("status") or "").lower() == "failed":
            print("WARN: job reported status 'failed'; fetching anyway.")

        result_items, failed = fetch_results(bulk_id, key, status.get("total_items"))
        with open(RAW_JSON, "w") as f:
            json.dump({"bulk_id": bulk_id, "model": MODEL, "status": status,
                       "items": result_items, "failed_items": failed},
                      f, indent=2, ensure_ascii=False)
        print(f"Raw responses saved to {RAW_JSON}")
        if failed:
            print(f"WARN: {len(failed)} failed items - see raw JSON.")

    joined = join(records, result_items)
    write(joined)
    summarize(joined)


if __name__ == "__main__":
    main()
