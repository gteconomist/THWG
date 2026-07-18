#!/usr/bin/env python3
"""
Track 1 - Family D: SBIR/STTR federal innovation funding tied to Georgia Tech.

Works off SBIR.gov's BULK AWARD DATA CSV (not the API — the API rate-limits hard).
The bulk file is a single static download with more fields than the API, so this is
both more reliable and more complete.

Two attribution channels:
  (RI)   SBIR/STTR awards naming Georgia Tech as the RESEARCH INSTITUTION.
         Directly GT-attributable, available now, no spinout roster needed
         (STTR requires a university research partner, so this is mostly STTR).
  (FIRM) Awards to Georgia Tech SPINOUT firms — needs a roster (--roster file,
         one firm name per line). Optional; deduped against the RI channel.

Metric D1: count + total $ of GT SBIR/STTR awards, by program, agency, and year.

DATA SOURCE (public, no key):
  https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv  (~65 MB)

USAGE
  python3 pull_sbir.py                       # auto-downloads the bulk CSV if missing, then filters
  python3 pull_sbir.py --from 2015 --to 2024
  python3 pull_sbir.py --csv /path/to/award_data_no_abstract.csv   # use a file you downloaded
  python3 pull_sbir.py --roster spinouts.txt                       # add spinout-firm channel
"""

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import requests

BULK_URL = "https://data.www.sbir.gov/mod_awarddatapublic_no_abstract/award_data_no_abstract.csv"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "gt_assignees.json"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# logical field -> candidate header names (normalized: lowercased, alphanumerics only)
COLSPEC = {
    "firm": ["company", "firm", "companyname"],
    "award_title": ["awardtitle", "title"],
    "agency": ["agency"],
    "branch": ["branch"],
    "phase": ["phase"],
    "program": ["program"],
    "award_year": ["awardyear", "year"],
    "award_amount": ["awardamount", "amount"],
    "state": ["state"],
    "city": ["city"],
    "pi_name": ["piname", "principalinvestigatorname", "principalinvestigator"],
    "ri_name": ["riname", "researchinstitution", "researchinstitutionname"],
    "agency_tracking_number": ["agencytrackingnumber", "agencytracking"],
    "contract": ["contract", "contractnumber"],
}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def detect_columns(header: list[str]) -> dict:
    nheader = {norm(h): h for h in header}
    colmap = {}
    for logical, cands in COLSPEC.items():
        found = None
        for c in cands:                       # exact normalized match first
            if c in nheader:
                found = nheader[c]; break
        if not found:                         # then substring
            for nh, orig in nheader.items():
                if any(c in nh for c in cands):
                    found = orig; break
        colmap[logical] = found
    return colmap


def load_ri_variants(config_path: Path) -> list[str]:
    cfg = json.loads(Path(config_path).read_text())
    v = cfg.get("sbir_research_institution_variants", [])
    if not v:
        sys.exit(f"No sbir_research_institution_variants in {config_path}")
    return v


def parse_amount(val) -> float:
    if val is None:
        return 0.0
    s = re.sub(r"[^0-9.\-]", "", str(val))
    try:
        return float(s) if s not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def download_bulk(dest: Path) -> None:
    print(f"Downloading SBIR bulk award data (~65 MB) -> {dest} ...", file=sys.stderr)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(BULK_URL, headers={"User-Agent": BROWSER_UA}, stream=True, timeout=300) as r:
        r.raise_for_status()
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if chunk:
                    f.write(chunk); got += len(chunk)
                    print(f"\r  {got/1e6:.0f} MB", end="", file=sys.stderr)
    print("  done.", file=sys.stderr)


def row_id(r: dict) -> str:
    return (r.get("agency_tracking_number") or r.get("contract")
            or f"{r.get('firm')}|{r.get('award_title')}|{r.get('award_year')}")


def year_ok(r: dict, y_from, y_to) -> bool:
    try:
        y = int(re.sub(r"[^0-9]", "", str(r.get("award_year") or ""))[:4] or 0)
    except ValueError:
        return y_from is None and y_to is None
    if not y:
        return y_from is None and y_to is None
    if y_from and y < y_from:
        return False
    if y_to and y > y_to:
        return False
    return True


def process(csv_path: Path, variants, roster, y_from, y_to):
    gt_keys = [v.lower() for v in variants]
    roster_lc = [x.lower() for x in roster] if roster else []
    kept = {}
    with open(csv_path, newline="", encoding="utf-8", errors="replace") as f:
        reader = csv.reader(f)
        header = next(reader)
        colmap = detect_columns(header)
        if not colmap.get("ri_name"):
            sys.exit(f"Could not find a research-institution column. Headers: {header}")
        idx = {log: (header.index(col) if col else None) for log, col in colmap.items()}

        def get(rowlist, log):
            i = idx.get(log)
            return rowlist[i] if i is not None and i < len(rowlist) else ""

        for rowlist in reader:
            ri = get(rowlist, "ri_name").lower()
            firm = get(rowlist, "firm").lower()
            channel = None
            if any(k in ri for k in gt_keys):
                channel = "research_institution"
            elif roster_lc and any(fn in firm for fn in roster_lc):
                channel = "spinout_firm"
            if not channel:
                continue
            r = {log: get(rowlist, log) for log in COLSPEC}
            if not year_ok(r, y_from, y_to):
                continue
            r["_channel"] = channel
            kept.setdefault(row_id(r), r)
    return list(kept.values()), colmap


def summarize(awards, y_from, y_to, variants):
    by_program = defaultdict(lambda: {"count": 0, "amount": 0.0})
    by_agency = defaultdict(lambda: {"count": 0, "amount": 0.0})
    by_year = defaultdict(lambda: {"count": 0, "amount": 0.0})
    by_channel = defaultdict(lambda: {"count": 0, "amount": 0.0})
    total = 0.0
    for a in awards:
        amt = parse_amount(a.get("award_amount"))
        total += amt
        for bucket, key in ((by_program, (a.get("program") or "UNKNOWN").upper()),
                            (by_agency, a.get("agency") or "UNKNOWN"),
                            (by_year, str(a.get("award_year") or "UNKNOWN")[:4]),
                            (by_channel, a.get("_channel", "unknown"))):
            bucket[key]["count"] += 1
            bucket[key]["amount"] += amt
    def clean(d, by_amount=True):
        items = sorted(d.items(), key=(lambda kv: -kv[1]["amount"]) if by_amount else (lambda kv: kv[0]))
        return {k: {"count": v["count"], "amount": round(v["amount"], 2)} for k, v in items}
    return {
        "metric_provenance": {
            "track": "Track 1 - Family D (SBIR/STTR federal innovation funding)",
            "source": "SBIR.gov bulk award data (award_data_no_abstract.csv)",
            "year_range": [y_from, y_to],
            "ri_variants": variants,
            "note": "RI channel = GT named as research institution (direct, mostly STTR). "
                    "FIRM channel = awards to GT spinout firms (needs --roster). Deduped by award id.",
        },
        "D1_total_awards": len(awards),
        "D1_total_amount": round(total, 2),
        "by_program": clean(by_program),
        "by_channel": clean(by_channel),
        "by_agency": clean(by_agency),
        "by_year": clean(by_year, by_amount=False),
    }


def write_csv(awards, path: Path):
    cols = ["award_year", "program", "phase", "agency", "firm", "award_amount",
            "ri_name", "state", "city", "pi_name", "award_title", "_channel"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for a in sorted(awards, key=lambda x: (str(x.get("award_year")), x.get("program") or "")):
            w.writerow([a.get(c, "") for c in cols])


def main():
    ap = argparse.ArgumentParser(description="Pull GT SBIR/STTR awards from the SBIR.gov bulk CSV (Track 1 Family D).")
    ap.add_argument("--from", dest="y_from", type=int, default=None)
    ap.add_argument("--to", dest="y_to", type=int, default=None)
    ap.add_argument("--roster", default=None, help="Optional file of GT spinout firm names (one per line)")
    ap.add_argument("--csv", default=None, help="Path to an already-downloaded bulk CSV (else it downloads)")
    ap.add_argument("--outdir", default=str(Path(__file__).resolve().parent.parent / "output"))
    ap.add_argument("--config", default=str(CONFIG_PATH))
    args = ap.parse_args()

    variants = load_ri_variants(Path(args.config))
    outdir = Path(args.outdir); outdir.mkdir(parents=True, exist_ok=True)

    csv_path = Path(args.csv) if args.csv else (outdir / "award_data_no_abstract.csv")
    if not csv_path.exists():
        try:
            download_bulk(csv_path)
        except Exception as e:
            sys.exit(f"Download failed ({e}).\nDownload it in a browser from:\n  {BULK_URL}\n"
                     f"save it, then re-run with:  --csv /path/to/award_data_no_abstract.csv")

    roster = None
    if args.roster:
        roster = [ln.strip() for ln in Path(args.roster).read_text().splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]

    print(f"Filtering {csv_path.name} for Georgia Tech awards...", file=sys.stderr)
    awards, colmap = process(csv_path, variants, roster, args.y_from, args.y_to)
    print(f"  matched columns: ri_name='{colmap.get('ri_name')}', "
          f"amount='{colmap.get('award_amount')}', program='{colmap.get('program')}', "
          f"year='{colmap.get('award_year')}'", file=sys.stderr)

    tag = f"{args.y_from or 'all'}_{args.y_to or 'all'}"
    out_csv = outdir / f"sbir_awards_{tag}.csv"
    out_json = outdir / f"sbir_summary_{tag}.json"
    write_csv(awards, out_csv)
    summary = summarize(awards, args.y_from, args.y_to, variants)
    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nDone. D1 awards: {summary['D1_total_awards']} | total ${summary['D1_total_amount']:,.0f}")
    for prog, v in summary["by_program"].items():
        print(f"  {prog}: {v['count']} awards, ${v['amount']:,.0f}")
    print(f"  rows    -> {out_csv}")
    print(f"  metrics -> {out_json}")


if __name__ == "__main__":
    main()
