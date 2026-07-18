#!/usr/bin/env python3
"""
Track 1 - Family F: Georgia Tech degrees conferred (talent into the innovation economy).

Source: NCES IPEDS Completions complete data files (the authoritative federal source).
This replaces the earlier Urban-API version, whose reprocessed "completions-cip" counts
ran ~1.7x GT's official degree numbers with an opaque award-level coding. Here we read
the raw federal file and take GT's grand-total row, which matches GT's published counts.

Per year it downloads:
  https://nces.ed.gov/ipeds/datacenter/data/C{year}_A.zip
Inside, the Completions "A" CSV has one row per UNITID x CIPCODE x MAJORNUM x AWLEVEL,
with CTOTALT = total degrees for that cell. We keep:
  UNITID = 139755 (Georgia Tech), CIPCODE = 99.0000 (grand total across all fields),
  MAJORNUM = 1 (first major — the standard degree count),
and sum CTOTALT by award level:
  AWLEVEL 5 = Bachelor's, 7 = Master's, 17/18/19 = Doctoral (research/professional/other).

Metric F1: degrees conferred per year, split Bachelor's / Master's / Doctoral.
IPEDS lags ~2 years, so the latest available year will be a couple years back — labeled honestly.

USAGE
  python3 pull_ipeds.py                 # 2015 .. latest available
  python3 pull_ipeds.py --from 2015 --to 2023
Downloaded zips are cached in output/ipeds_cache/ so re-runs are fast.
"""

import argparse
import csv
import datetime
import io
import json
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import requests

GT_UNITID = "139755"
BASE = "https://nces.ed.gov/ipeds/datacenter/data"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

AWLEVEL_BUCKET = {5: "bachelors", 7: "masters", 17: "doctoral", 18: "doctoral", 19: "doctoral",
                  9: "doctoral", 10: "doctoral"}  # 9/10 cover pre-2011 coding, just in case


def download_zip(year: int, cache_dir: Path) -> bytes | None:
    cache = cache_dir / f"C{year}_A.zip"
    if cache.exists() and cache.stat().st_size > 0:
        return cache.read_bytes()
    url = f"{BASE}/C{year}_A.zip"
    for attempt in range(1, 4):
        try:
            r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=120)
        except requests.RequestException as e:
            print(f"  {year}: request error ({e}); retry {attempt}/3...", file=sys.stderr)
            time.sleep(3 * attempt); continue
        if r.status_code == 200 and r.content[:2] == b"PK":  # a real zip
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_bytes(r.content)
            return r.content
        if r.status_code == 404:
            return None  # year not published yet
        print(f"  {year}: HTTP {r.status_code}; retry {attempt}/3...", file=sys.stderr)
        time.sleep(3 * attempt)
    return None


def pick_csv(zf: zipfile.ZipFile) -> str | None:
    names = [n for n in zf.namelist() if n.lower().endswith(".csv")
             and "_dict" not in n.lower() and "_freq" not in n.lower()]
    if not names:
        return None
    rv = [n for n in names if "_rv" in n.lower()]          # prefer the revised/final file
    pool = rv or names
    return max(pool, key=lambda n: zf.getinfo(n).file_size)


def _clean_col(c: str) -> str:
    c = c.strip().strip('"').lstrip("﻿")
    if c.startswith("ï»¿"):        # UTF-8 BOM decoded as latin-1 (2023+ files)
        c = c[3:]
    return c.upper()


def col_index(header: list[str]) -> dict:
    idx = {_clean_col(c): i for i, c in enumerate(header)}
    need = {}
    for key in ("UNITID", "CIPCODE", "MAJORNUM", "AWLEVEL", "CTOTALT"):
        if key not in idx:
            raise RuntimeError(f"column {key} not found; header={header[:15]}")
        need[key] = idx[key]
    return need


def parse_year(zbytes: bytes) -> dict | None:
    with zipfile.ZipFile(io.BytesIO(zbytes)) as zf:
        name = pick_csv(zf)
        if not name:
            return None
        text = zf.read(name).decode("utf-8-sig", errors="replace")  # utf-8-sig strips any BOM
        reader = csv.reader(io.StringIO(text))
        if True:
            header = next(reader)
            ci = col_index(header)
            buckets = defaultdict(int)
            for row in reader:
                if len(row) <= ci["CTOTALT"]:
                    continue
                if row[ci["UNITID"]].strip() != GT_UNITID:
                    continue
                cip = row[ci["CIPCODE"]].strip().strip('"')
                try:
                    if float(cip) != 99.0:      # 99.0000 = grand total across all fields
                        continue
                except ValueError:
                    continue
                if row[ci["MAJORNUM"]].strip() not in ("1", "1.0"):
                    continue
                try:
                    lvl = int(float(row[ci["AWLEVEL"]]))
                    cnt = int(float(row[ci["CTOTALT"]] or 0))
                except ValueError:
                    continue
                bucket = AWLEVEL_BUCKET.get(lvl)
                if bucket:
                    buckets[bucket] += cnt
    b, m, d = buckets["bachelors"], buckets["masters"], buckets["doctoral"]
    if b == 0 and m == 0 and d == 0:
        return None
    return {"bachelors": b, "masters": m, "doctoral": d, "degrees": b + m + d}


def main() -> None:
    ap = argparse.ArgumentParser(description="Pull GT degrees conferred from NCES IPEDS (Track 1 Family F).")
    ap.add_argument("--from", dest="y_from", type=int, default=2015)
    ap.add_argument("--to", dest="y_to", type=int, default=datetime.date.today().year)
    ap.add_argument("--outdir", default=str(Path(__file__).resolve().parent.parent / "output"))
    args = ap.parse_args()

    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)
    cache_dir = outdir / "ipeds_cache"

    trend = []
    for year in range(args.y_from, args.y_to + 1):
        zbytes = download_zip(year, cache_dir)
        if zbytes is None:
            print(f"  {year}: not available (skipped)", file=sys.stderr)
            continue
        try:
            res = parse_year(zbytes)
        except RuntimeError as e:
            print(f"  {year}: parse error ({e})", file=sys.stderr); continue
        if res:
            res["year"] = year
            trend.append(res)
            print(f"  {year}: {res['degrees']:,} degrees "
                  f"(B {res['bachelors']:,}, M {res['masters']:,}, D {res['doctoral']:,})", file=sys.stderr)
        time.sleep(0.3)

    if not trend:
        sys.exit("No IPEDS data parsed — check connectivity or year range.")
    trend.sort(key=lambda r: r["year"])
    latest = trend[-1]
    summary = {
        "metric_provenance": {
            "track": "Track 1 - Family F (degrees conferred / innovation talent)",
            "source": "NCES IPEDS Completions complete data files (C{year}_A), UNITID 139755, "
                      "CIPCODE 99 grand total, MAJORNUM 1 (first major).",
            "note": "Degrees = Bachelor's (AWLEVEL 5) + Master's (7) + Doctoral (17/18/19); "
                    "excludes sub-baccalaureate certificates. Matches GT's official published counts. "
                    "IPEDS lags ~2 years, so the latest year is the most recent reported. "
                    "GT is STEM-focused, so total degrees ≈ STEM talent.",
        },
        "F1_latest": {"year": latest["year"], "bachelors": latest["bachelors"],
                      "masters": latest["masters"], "doctoral": latest["doctoral"],
                      "degrees": latest["degrees"]},
        "trend": trend,
    }
    out = outdir / "ipeds_summary.json"
    out.write_text(json.dumps(summary, indent=2))
    print(f"\nDone. Latest ({latest['year']}): {latest['degrees']:,} degrees "
          f"(Bachelor's {latest['bachelors']:,}, Master's {latest['masters']:,}, "
          f"Doctoral {latest['doctoral']:,}).")
    print(f"  metrics -> {out}")


if __name__ == "__main__":
    main()
