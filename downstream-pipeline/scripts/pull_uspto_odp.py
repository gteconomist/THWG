#!/usr/bin/env python3
"""
Track 1 - Family B: USPTO patent output puller for Georgia Tech (USPTO Open Data Portal).

Supersedes pull_uspto_patents.py (which targeted the retired PatentsView API).
This targets the USPTO ODP, the live platform as of 2026.

Pulls Georgia Tech patent APPLICATIONS from the ODP file-wrapper search, keeps the
GRANTED ones, and computes:
    B1  patents granted in the target year (count)
    B2  unique named inventors on those patents (count)
    B3  technology-area distribution (CPC subclass counts)

API (confirmed against USPTO docs + multiple working clients):
    GET https://api.uspto.gov/api/v1/patent/applications/search
    Header: X-API-KEY: <key>          (your active ODP key)
    Header: User-Agent: <browser UA>  (REQUIRED — USPTO's WAF blocks non-browser agents)
    q= Lucene query on applicationMetaData fields; 404 = zero matches (not an error).

NOTE ON COVERAGE: ODP search is application/file-wrapper-centric. "Granted in year Y"
here = GT-applicant records whose grantDate falls in year Y. File-wrapper coverage is
strongest for modern applications; sanity-check the first run against GT's known counts.
Forward citations (metric B4) are NOT available from this endpoint — separate source needed.

USAGE
    export USPTO_API_KEY="your_key_here"
    python pull_uspto_odp.py --year 2024
    python pull_uspto_odp.py                # all grant years found (no filter)
"""

import argparse
import csv
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlencode

import requests

SEARCH_URL = "https://api.uspto.gov/api/v1/patent/applications/search"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "gt_assignees.json"
PAGE_SIZE = 100
MAX_RETRIES = 4
RETRY_BACKOFF = 3.0
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def load_applicant_variants(config_path: Path) -> list[str]:
    with open(config_path) as f:
        cfg = json.load(f)
    variants = cfg.get("assignee_name_variants", [])
    if not variants:
        sys.exit(f"No assignee_name_variants found in {config_path}")
    return variants


def build_query(variants: list[str]) -> str:
    """Match GT as an applicant in any position (phrase match on each variant, OR-combined)."""
    clauses = [f'applicationMetaData.applicantBag.applicantNameText:"{v}"' for v in variants]
    return "(" + " OR ".join(clauses) + ")"


def api_get(api_key: str, params: dict) -> dict | None:
    headers = {"X-API-KEY": api_key, "Accept": "application/json", "User-Agent": BROWSER_UA}
    url = f"{SEARCH_URL}?{urlencode(params)}"
    for attempt in range(1, MAX_RETRIES + 1):
        resp = requests.get(url, headers=headers, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return None  # ODP convention: zero matching records
        if resp.status_code in (429, 500, 502, 503, 504):
            wait = RETRY_BACKOFF * attempt
            print(f"  HTTP {resp.status_code}; retry in {wait:.0f}s...", file=sys.stderr)
            time.sleep(wait)
            continue
        sys.exit(f"API error {resp.status_code}: {resp.text[:400]}")
    sys.exit(f"Gave up after {MAX_RETRIES} retries.")


def fetch_all(api_key: str, variants: list[str]) -> list[dict]:
    """Offset-paginate the full GT applicant result set. Dedupe defensively by
    application number and stop when a page yields no NEW records (guards against
    an offset the API might ignore)."""
    q = build_query(variants)
    records: dict[str, dict] = {}
    offset = 0
    page = 0
    while True:
        params = {
            "q": q,
            "offset": offset,
            "limit": PAGE_SIZE,
            "sort": "applicationMetaData.filingDate desc",
        }
        data = api_get(api_key, params)
        if data is None:
            break
        batch = data.get("patentFileWrapperDataBag") or []
        if not batch:
            break
        new = 0
        for rec in batch:
            meta = rec.get("applicationMetaData") or {}
            appno = str(rec.get("applicationNumberText") or meta.get("applicationNumberText") or "").strip()
            key = appno or json.dumps(rec, sort_keys=True)[:80]
            if key not in records:
                records[key] = rec
                new += 1
        page += 1
        total = data.get("count")
        print(f"  page {page}: {len(batch)} rows, {new} new (have {len(records)}"
              + (f"/{total}" if total is not None else "") + ")", file=sys.stderr)
        if new == 0:  # API ignored offset or we've seen everything
            break
        if len(batch) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.5)
    return list(records.values())


def is_granted(meta: dict) -> bool:
    status = (meta.get("applicationStatusDescriptionText") or "").lower()
    return "patented" in status or bool(meta.get("patentNumber") or meta.get("grantDate"))


def cpc_subclasses(meta: dict) -> list[str]:
    """Return distinct CPC subclasses (first 4 chars, e.g. 'H04B') for one record."""
    out = set()
    for c in meta.get("cpcClassificationBag") or []:
        code = c if isinstance(c, str) else (c.get("cpcSymbol") or c.get("symbol") or "")
        code = str(code).strip().replace(" ", "")
        if len(code) >= 4:
            out.add(code[:4])
    return sorted(out)


def summarize(records: list[dict], year: int | None, variants: list[str]) -> tuple[dict, list[dict]]:
    kept = []
    inventors: set[str] = set()
    cpc = Counter()
    for rec in records:
        meta = rec.get("applicationMetaData") or {}
        if not is_granted(meta):
            continue
        grant_date = str(meta.get("grantDate") or "").strip()
        if year is not None and not grant_date.startswith(str(year)):
            continue
        kept.append(rec)
        for inv in meta.get("inventorBag") or []:
            name = (inv.get("inventorNameText") or "").strip().lower()
            if name:
                inventors.add(name)
        for sub in cpc_subclasses(meta):
            cpc[sub] += 1
    summary = {
        "metric_provenance": {
            "track": "Track 1 - Family B (USPTO patent output)",
            "source": "USPTO Open Data Portal (api.uspto.gov file-wrapper search)",
            "grant_year_filter": year,
            "applicant_variants": variants,
            "note": "Granted = status 'Patented Case' or patentNumber/grantDate present. "
                    "B4 forward citations not available from this endpoint.",
        },
        "B1_patents_granted": len(kept),
        "B2_unique_inventors": len(inventors),
        "B3_cpc_subclass_distribution": dict(cpc.most_common()),
    }
    return summary, kept


def write_csv(records: list[dict], path: Path) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["patent_number", "application_number", "grant_date", "filing_date",
                    "title", "status", "applicants", "inventors", "cpc_subclasses"])
        for rec in records:
            m = rec.get("applicationMetaData") or {}
            applicants = "; ".join(
                a.get("applicantNameText", "") for a in (m.get("applicantBag") or []) if a.get("applicantNameText"))
            inventors = "; ".join(
                i.get("inventorNameText", "") for i in (m.get("inventorBag") or []) if i.get("inventorNameText"))
            w.writerow([
                m.get("patentNumber", ""),
                rec.get("applicationNumberText", "") or m.get("applicationNumberText", ""),
                m.get("grantDate", ""), m.get("filingDate", ""),
                m.get("inventionTitle", ""), m.get("applicationStatusDescriptionText", ""),
                applicants, inventors, "; ".join(cpc_subclasses(m)),
            ])


def main() -> None:
    ap = argparse.ArgumentParser(description="Pull GT granted patents from USPTO ODP (Track 1 Family B).")
    ap.add_argument("--year", type=int, default=None, help="Filter to patents GRANTED in this calendar year")
    ap.add_argument("--outdir", default=str(Path(__file__).resolve().parent.parent / "output"))
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    api_key = os.environ.get("USPTO_API_KEY")
    if not api_key:
        sys.exit("Set USPTO_API_KEY (your active ODP key from data.uspto.gov -> Manage API Key).")

    variants = load_applicant_variants(Path(args.config))
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    print(f"Querying USPTO ODP for GT applicant records ({len(variants)} name variants)...", file=sys.stderr)
    records = fetch_all(api_key, variants)
    print(f"  retrieved {len(records)} total GT applicant records", file=sys.stderr)

    summary, kept = summarize(records, args.year, variants)
    tag = str(args.year) if args.year is not None else "all"
    csv_path = outdir / f"gt_patents_{tag}.csv"
    summary_path = outdir / f"gt_patents_{tag}_summary.json"
    write_csv(kept, csv_path)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nDone. B1 granted patents ({tag}): {summary['B1_patents_granted']} | "
          f"B2 unique inventors: {summary['B2_unique_inventors']}")
    print(f"  rows    -> {csv_path}")
    print(f"  metrics -> {summary_path}")
    if summary["B1_patents_granted"] == 0 and records:
        print("\n  NOTE: retrieved records but 0 matched the granted/year filter. "
              "Check the applicant name variants in config/gt_assignees.json against the "
              "'applicants' column in the CSV, or widen the --year filter.", file=sys.stderr)


if __name__ == "__main__":
    main()
