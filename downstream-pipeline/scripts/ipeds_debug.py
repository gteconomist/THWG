#!/usr/bin/env python3
"""Diagnostic v3 (decisive): the by-award_level sums are ~2x GT reality, which usually
means the data has a demographic 'Total' row being summed alongside the individual
race/sex rows. This dumps the race and sex breakdowns so we can see the code used for
'Total', then filter to it. GT 2022. Paste the output back."""
import json, sys, time
import requests

BASE = "https://educationdata.urban.org/api/v1/college-university/ipeds/completions-cip-2/summaries"
UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36", "Accept": "application/json"}

QUERIES = {
    "by=race (find the Total race code)": {"var":"awards","stat":"sum","by":"race","unitid":139755,"year":2022},
    "by=sex (find the Total sex code)":   {"var":"awards","stat":"sum","by":"sex","unitid":139755,"year":2022},
}

def run(params):
    for attempt in range(1, 7):
        try:
            r = requests.get(BASE, params=params, headers=UA, timeout=180)
        except requests.RequestException as e:
            print(f"    timeout ({e}); retry {attempt}/6...", file=sys.stderr); time.sleep(5); continue
        if r.status_code == 200:
            d = r.json(); return d.get("results", d) if isinstance(d, dict) else d
        print(f"    HTTP {r.status_code}; retry {attempt}/6...", file=sys.stderr); time.sleep(5)
    return None

for name, params in QUERIES.items():
    print(f"\n=== {name} ===")
    rows = run(params)
    if rows is None:
        print("  (failed)"); continue
    total = 0
    for row in sorted(rows, key=lambda x: -(x.get("awards") or 0)):
        keys = [k for k in row if k not in ("awards",)]
        lbl = ", ".join(f"{k}={row.get(k)}" for k in keys)
        print(f"  {lbl}  awards={row.get('awards')}")
        total += row.get("awards") or 0
    print(f"  [sum of rows above = {total}]  <- if one row ~= this, that row is the Total")
