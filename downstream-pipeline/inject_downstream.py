#!/usr/bin/env python3
"""
Regenerate the cedr.tech downstream data from pull output, and inject it into the site.

Reads every  output/gt_patents_<year>_summary.json  produced by pull_uspto_odp.py,
builds the DOWNSTREAM data object (latest year, multi-year trend, leading technology
areas), writes a JSON snapshot, and rewrites the inline `const DOWNSTREAM = {...}`
block in ../index.html between the DOWNSTREAM_DATA markers.

The site stays a single self-contained file (data inlined) so it still opens locally.

USAGE (from the downstream-pipeline folder, after running the pulls):
    python3 inject_downstream.py
Then commit + push index.html to deploy.
"""

import datetime
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent            # downstream-pipeline/
OUTPUT_DIR = HERE / "output"
DATA_OUT = HERE / "data" / "downstream_metrics.json"
SITE = HERE.parent / "index.html"                 # repo-root/index.html
START = "/* DOWNSTREAM_DATA_START"
END = "DOWNSTREAM_DATA_END */"

# CPC subclass -> friendly technology-area label. Subclasses that share a label
# are aggregated (e.g. all C08* -> Polymers & advanced materials). Unmapped codes
# fall back to "CPC <code>".
CPC_LABELS = {
    "G01N": "Measurement & materials testing",
    "H01M": "Batteries & fuel cells",
    "G06F": "Computing & data processing",
    "G06N": "AI & machine learning",
    "G06V": "Imaging & computer vision", "G06T": "Imaging & computer vision", "G06K": "Imaging & computer vision",
    "B01J": "Catalysis & chemical processes", "B01D": "Separation & filtration", "B01L": "Lab & microfluidics",
    "A61M": "Medical devices", "A61B": "Diagnostics & surgery", "A61F": "Medical devices",
    "A61K": "Pharmaceuticals & therapeutics", "A61P": "Pharmaceuticals & therapeutics", "A61L": "Biomaterials",
    "A61N": "Medical devices", "A61Q": "Pharmaceuticals & therapeutics",
    "G02B": "Optics & photonics", "G02F": "Optics & photonics", "G01J": "Optics & photonics",
    "C08G": "Polymers & advanced materials", "C08L": "Polymers & advanced materials",
    "C08F": "Polymers & advanced materials", "C08K": "Polymers & advanced materials",
    "C08J": "Polymers & advanced materials",
    "C09D": "Coatings & adhesives", "C09J": "Coatings & adhesives", "C09K": "Materials & chemistry",
    "C07D": "Organic chemistry", "C07K": "Peptides & proteins",
    "C12N": "Biotechnology", "C12M": "Biotechnology", "C12Q": "Biotechnology",
    "C12P": "Biotechnology", "C12Y": "Biotechnology",
    "Y02E": "Clean energy", "Y02P": "Clean energy", "Y02C": "Clean energy",
    "Y02T": "Clean energy", "Y02W": "Clean energy",
    "H04L": "Communications & networking", "H04B": "Communications & networking",
    "H04N": "Communications & networking", "H01Q": "Antennas & RF",
    "H10D": "Semiconductors", "H10K": "Semiconductors", "H10F": "Semiconductors",
    "H10W": "Semiconductors", "H10P": "Semiconductors", "H01L": "Semiconductors",
    "B33Y": "Additive manufacturing", "B22F": "Additive manufacturing", "B29D": "Additive manufacturing",
    "B25J": "Robotics", "B64C": "Aerospace", "B64U": "Aerospace",
    "C25B": "Electrochemistry", "C01B": "Materials & chemistry", "H02J": "Power systems",
    "H02S": "Solar power", "H02K": "Electric machines",
}

# Internal-intake metrics that the reporting office only began tracking in a given
# fiscal year. Earlier cells hold 0 as a placeholder, not a true zero, so they are
# dropped rather than charted as six flat years of nothing.
NOT_TRACKED_BEFORE = {"licenses_to_startups": 2021}

# ---- Editorial constants (not data-driven; edit here as families come online) ----
BENCHMARK = {"source": "NAI 2024 Top 100 U.S. Universities", "value": 102, "pct": 97}
FAMILIES = {
    "tier1": [
        {"name": "Patents & inventors", "status": "live",
         "desc": "U.S. utility patents assigned to Georgia Tech, and the inventors behind them.",
         "metrics": "__FAMILY_B_METRICS__"},
        {"name": "Talent into the innovation economy", "status": "building",
         "desc": "Degrees Georgia Tech confers each year &mdash; the STEM talent flowing into the workforce and startup ecosystem.",
         "metrics": "__FAMILY_F_METRICS__"},
        {"name": "Licensing & startups", "status": "building",
         "desc": "Invention disclosures, licenses & options executed, licensing income, and startups formed &mdash; on AUTM's national definitions.",
         "metrics": "Source: GT OTL + AUTM licensing survey"},
        {"name": "Federal innovation funding", "status": "building",
         "desc": "SBIR / STTR awards where Georgia Tech is the named research institution &mdash; federally-funded innovation projects built on GT research (mostly STTR, which requires a university partner).",
         "metrics": "Source: SBIR.gov bulk award data"},
        {"name": "Industry research engagement", "status": "building",
         "desc": "Industry-sponsored research expenditures and active corporate research agreements.",
         "metrics": "Source: GT research accounting + NSF HERD"},
    ],
    "tier2": [
        {"name": "Startup growth & retention", "status": "planned",
         "desc": "Jobs and follow-on capital at Georgia Tech spinouts, exits, and the share that stay in Georgia &mdash; reported as activity at spinouts, never as GT-attributed totals.",
         "metrics": "Source: PitchBook / Crunchbase (pending)"},
    ],
    "tier3": [
        {"name": "Alumni entrepreneurship & knowledge diffusion", "status": "planned",
         "desc": "Aggregate footprint of alumni-founded companies and the geographic spread of Georgia Tech knowledge, with an explicit attribution stance &mdash; after the MIT / Stanford model.",
         "metrics": "Every 3&ndash;5 years &middot; modeled"},
    ],
}


def label_for(code: str) -> str:
    return CPC_LABELS.get(code, f"CPC {code}")


def build_family_d(output_dir: Path):
    """Read the widest SBIR/STTR summary, if present, to bring Family D live."""
    files = list(output_dir.glob("sbir_summary_*.json"))
    if not files:
        return None
    best = max((json.loads(p.read_text()) for p in files), key=lambda s: s.get("D1_total_awards", 0))
    if not best.get("D1_total_awards", 0):
        return None
    by_year = best.get("by_year", {})
    years = sorted(int(y) for y in by_year if str(y).isdigit())
    if not years:
        return None
    latest = years[-1]
    ly = by_year.get(str(latest), {"count": 0, "amount": 0.0})
    # Annual headline (latest year), to match the patents family's unit. The full
    # decade lives in the trend series (by_year), not blended into the headline.
    metrics = (f"<b>{ly['count']}</b> SBIR/STTR awards &middot; "
               f"<b>${ly['amount']/1e6:.1f}M</b> project value &middot; {latest}")
    return {"metrics": metrics,
            "data": {"latest_year": latest,
                     "latest_awards": ly["count"], "latest_amount": round(ly["amount"], 2),
                     "cumulative_awards": best.get("D1_total_awards", 0),
                     "cumulative_amount": round(best.get("D1_total_amount", 0.0), 2),
                     "span": [years[0], years[-1]],
                     "by_year": by_year, "by_agency": best.get("by_agency", {}),
                     "by_program": best.get("by_program", {})}}


def build_family_a(output_dir: Path):
    """Read the GT OTL/GTRC licensing & startups intake, if filled, to bring Family A live."""
    p = output_dir / "internal_licensing.json"
    if not p.exists():
        return None
    metrics = json.loads(p.read_text()).get("metrics", {})

    def series(key):
        by = (metrics.get(key) or {}).get("by_year") or {}
        cut = NOT_TRACKED_BEFORE.get(key)
        out = {}
        for y, v in by.items():
            if not str(y).isdigit():
                continue
            y = int(y)
            if cut and y < cut:
                continue
            out[y] = v
        return dict(sorted(out.items()))

    disc = series("invention_disclosures")
    apps = series("patent_apps_filed")
    issued = series("patents_issued")
    lic = series("licenses_options_executed")
    licsu = series("licenses_to_startups")
    inc = series("licensing_income_usd")
    sform = series("startups_formed")
    if not (disc or lic or inc):
        return None

    years = sorted(set(disc) | set(lic) | set(inc) | set(sform))
    latest = years[-1]
    g = lambda d: d.get(latest)

    card = (f"<b>{g(disc):,}</b> invention disclosures (FY{latest}) &middot; "
            f"<b>{g(lic)}</b> licenses &amp; options &middot; "
            f"<b>${g(inc)/1e6:.1f}M</b> licensing income")

    return {"metrics": card,
            "data": {
                "latest_year": latest,
                "span": [years[0], years[-1]],
                "latest": {"disclosures": g(disc), "patent_apps": g(apps),
                           "patents_issued": g(issued), "licenses": g(lic),
                           "licenses_to_startups": g(licsu), "income": g(inc),
                           "startups_formed": g(sform)},
                "cumulative": {"startups_formed": sum(sform.values()),
                               "income": sum(inc.values()),
                               "licenses": sum(lic.values()),
                               "disclosures": sum(disc.values()),
                               "startups_span": [min(sform), max(sform)] if sform else None},
                "income_trend": [{"year": y, "value": v} for y, v in inc.items()],
                "disclosures_trend": [{"year": y, "value": v} for y, v in disc.items()],
                "startups_by_year": [{"year": y, "value": v} for y, v in sform.items()],
                "licenses_to_startups_from": NOT_TRACKED_BEFORE.get("licenses_to_startups"),
            }}


def build_family_f(output_dir: Path):
    """Read IPEDS degrees-conferred summary, if present, to bring Family F live."""
    p = output_dir / "ipeds_summary.json"
    if not p.exists():
        return None
    s = json.loads(p.read_text())
    L = s.get("F1_latest")
    trend = s.get("trend", [])
    if not L or not L.get("degrees"):
        return None
    metrics = (f"<b>{L['degrees']:,}</b> degrees ({L['year']}) &middot; "
               f"<b>{L['masters']:,}</b> master's &middot; <b>{L['doctoral']:,}</b> doctoral")
    return {"metrics": metrics, "data": {"latest": L, "trend": trend}}


def build_data() -> dict:
    summaries = {}
    for p in sorted(OUTPUT_DIR.glob("gt_patents_*_summary.json")):
        m = re.search(r"gt_patents_(\d{4})_summary\.json", p.name)
        if not m:
            continue
        summaries[int(m.group(1))] = json.loads(p.read_text())
    if not summaries:
        raise SystemExit(f"No summary files found in {OUTPUT_DIR}")

    trend = [{"year": y, "patents": s.get("B1_patents_granted", 0)} for y, s in sorted(summaries.items())]
    latest_year = max(summaries)
    latest = summaries[latest_year]

    # Leading technology areas from the latest year, aggregated by friendly label.
    agg: dict[str, int] = {}
    for code, cnt in (latest.get("B3_cpc_subclass_distribution") or {}).items():
        agg[label_for(code)] = agg.get(label_for(code), 0) + cnt
    tech = [{"label": k, "count": v} for k, v in sorted(agg.items(), key=lambda kv: -kv[1])][:9]

    families = json.loads(json.dumps(FAMILIES))  # deep copy
    families["tier1"][0]["metrics"] = (
        f"<b>{latest.get('B1_patents_granted', 0)}</b> patents granted ({latest_year}) "
        f"&middot; <b>{latest.get('B2_unique_inventors', 0)}</b> inventors")

    # Family D (SBIR/STTR) — bring the card live if a summary exists.
    fd = build_family_d(OUTPUT_DIR)
    if fd:
        for fam in families["tier1"]:
            if fam["name"] == "Federal innovation funding":
                fam["status"] = "live"
                fam["metrics"] = fd["metrics"]

    # Family A (GT OTL licensing & startups intake) — bring the card live if filled.
    fa = build_family_a(OUTPUT_DIR)
    if fa:
        for fam in families["tier1"]:
            if fam["name"] == "Licensing & startups":
                fam["status"] = "live"
                fam["metrics"] = fa["metrics"]

    # Family F (IPEDS degrees) — bring the card live if a summary exists.
    ff = build_family_f(OUTPUT_DIR)
    if ff:
        for fam in families["tier1"]:
            if fam["name"] == "Talent into the innovation economy":
                fam["status"] = "live"
                fam["metrics"] = ff["metrics"]

    result = {
        "meta": {"latest_year": latest_year,
                 "updated": datetime.date.today().isoformat(),
                 "source": "USPTO Open Data Portal + SBIR.gov; benchmarked to NAI Top 100 Universities"},
        "familyB": {
            "latest": {"year": latest_year,
                       "patents": latest.get("B1_patents_granted", 0),
                       "inventors": latest.get("B2_unique_inventors", 0)},
            "benchmark": BENCHMARK,
            "trend": trend,
            "techAreas": tech,
        },
        "families": families,
    }
    if fd:
        result["familyD"] = fd["data"]
    if ff:
        result["familyF"] = ff["data"]
    if fa:
        result["familyA"] = fa["data"]
    return result


def inject(data: dict) -> None:
    html = SITE.read_text()
    s = html.find(START)
    e = html.find(END)
    if s == -1 or e == -1:
        raise SystemExit("DOWNSTREAM_DATA markers not found in index.html")
    e_line_end = html.find("\n", e) + 1
    block = ("/* DOWNSTREAM_DATA_START — regenerated by pipeline/inject_downstream.py */\n"
             "const DOWNSTREAM = " + json.dumps(data, indent=2) + ";\n"
             "/* DOWNSTREAM_DATA_END */")
    # Rewind start to the beginning of its line to keep indentation clean.
    s_line_start = html.rfind("\n", 0, s) + 1
    new_html = html[:s_line_start] + block + html[e_line_end:]
    SITE.write_text(new_html)


def main() -> None:
    data = build_data()
    DATA_OUT.parent.mkdir(parents=True, exist_ok=True)
    DATA_OUT.write_text(json.dumps(data, indent=2))
    inject(data)
    t = data["familyB"]["trend"]
    print(f"Injected downstream data: {len(t)} year(s) {t[0]['year']}–{t[-1]['year']}, "
          f"latest B1={data['familyB']['latest']['patents']}.")
    print(f"  wrote {DATA_OUT}")
    print(f"  updated {SITE}")


if __name__ == "__main__":
    main()
