#!/usr/bin/env python3
"""
Build the interactive map page (kart.html) from the CSV snapshots in data/.

Usage: python scripts/build_map.py [--data data] [--geo geo/no_zones.geojson] [--out kart.html] [--standalone]
"""
from __future__ import annotations

import argparse
import glob
import json
import subprocess
from datetime import date
from pathlib import Path

import pandas as pd

LISTS = {  # key -> (folder, label)
    "reservert": ("reservasjoner", "Reservert"),
    "ko": ("kapasitetsko", "I kapasitetskø"),
    "tilbaketrukket": ("tilbaketrukket", "Tilbaketrukket"),
    "tilknyttet": ("tilknyttet", "Tilknyttet"),
}
MW_COL = {
    "reservert": "Reservert kapasitet (MW)",
    "ko": "Kapasitet i kø (MW)",
    "tilbaketrukket": "Tilbaketrukket kapasitet (MW)",
    "tilknyttet": "Tilknyttet kapasitet totalt (MW)",
}
DATE_COL = {
    "reservert": "Dato for når Statnett reserverte kapasitet til kunden",
    "ko": "Dato - moden bestilling fra sluttkunde",
    "tilbaketrukket": "Dato - tilbaketrukket kapasitet",
    "tilknyttet": "Dato - tilknyttet kapasitet",
}
# Stations in the tilknyttet list that never appear in a list carrying Prisområde.
STATION_FALLBACK = {
    "Dagali TRA": "NO5", "Lødingen 66kV TRA": "NO4", "Skillemoen TRA": "NO4", "Songkjølen KRA/TRA": "NO1",
    "Trofors TRA": "NO4", "Ullsfjord TRA": "NO4", "Vemorktoppen": "NO2", "Åsen": "NO2",
}


def load_rows(data_dir: Path):
    station_area: dict[str, str] = {}
    frames = []
    for key, (folder, label) in LISTS.items():
        for f in sorted(glob.glob(str(data_dir / folder / "*.csv"))):
            df = pd.read_csv(f)
            df.columns = [c.lstrip("\ufeff") for c in df.columns]
            kategori = "Produksjon" if "Produksjon" in Path(f).stem else "Forbruk"
            df["_liste"] = key
            df["_kategori"] = kategori
            df["_mw"] = pd.to_numeric(df[MW_COL[key]], errors="coerce").fillna(0)
            df["_dato"] = df.get(DATE_COL[key], "")
            if "Prisområde" in df.columns:
                for s, p in zip(df["Stasjon for tilknytning i transmisjonsnettet"], df["Prisområde"]):
                    if isinstance(p, str) and p.startswith("NO"):
                        station_area[s] = p
            frames.append(df)
    all_df = pd.concat(frames, ignore_index=True)
    if "Prisområde" not in all_df.columns:
        all_df["Prisområde"] = None
    inferred = 0
    for i, r in all_df.iterrows():
        p = r.get("Prisområde")
        if not (isinstance(p, str) and p.startswith("NO")):
            s = r["Stasjon for tilknytning i transmisjonsnettet"]
            all_df.at[i, "Prisområde"] = station_area.get(s) or STATION_FALLBACK.get(s) or "Ukjent"
            inferred += 1
    rows = []
    for _, r in all_df.iterrows():
        def g(c):
            v = r.get(c)
            return "" if (v is None or (isinstance(v, float) and pd.isna(v))) else str(v)
        extra = {}
        for c in ("Opprinnelig reservert kapasitet (MW)", "Gjeldende reservert kapasitet (MW)",
                  "Tilknyttet kapasitet med ordinære vilkår (MW)", "Tilknyttet kapasitet med særlige vilkår (MW)",
                  "Dato - Ønsket tilknytning", "Dato - Planlagt tilknytning", "Dato - Planlagt tiknytning"):
            if c in all_df.columns and g(c):
                extra[c.replace("tiknytning", "tilknytning")] = g(c)
        rows.append({
            "l": r["_liste"], "k": r["_kategori"], "id": g("Statnett saksnr."), "tilko": g("Tilko saksnr."),
            "st": g("Stasjon for tilknytning i transmisjonsnettet"), "op": g("Områdeplan"), "pa": g("Prisområde"),
            "kunde": g("Statnetts kunde"), "slutt": g("Sluttkunde"), "n": g("Næringstype"),
            "mw": round(float(r["_mw"]), 2), "d": g("_dato"), "ansv": g("Kunde og tilknytningsansvarlig"), "x": extra,
        })
    return rows, inferred


def project(lon, lat):
    import math
    return (lon - 15.0) * math.cos(math.radians(lat)), lat


def load_geo(path: Path):
    g = json.loads(path.read_text())
    zones = {}
    for f in g["features"]:
        name = f["properties"]["zoneName"].replace("NO-", "")
        polys = []
        for poly in f["geometry"]["coordinates"]:
            ring = poly[0]
            polys.append([[round(v, 3) for v in project(x, y)] for x, y in ring])
        zones[name] = polys
    return zones


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data")
    ap.add_argument("--geo", default="geo/no_zones.geojson")
    ap.add_argument("--out", default="kart.html")
    ap.add_argument("--template", default=str(Path(__file__).with_name("map_template.html")))
    ap.add_argument("--logo", default="assets/logo-white.svg")
    ap.add_argument("--standalone", action="store_true", help="wrap in a full HTML document (for GitHub Pages)")
    args = ap.parse_args()

    rows, inferred = load_rows(Path(args.data))
    zones = load_geo(Path(args.geo))
    try:
        snap = subprocess.run(["git", "log", "-1", "--format=%cd", "--date=short", "--", args.data],
                              capture_output=True, text=True, check=True).stdout.strip() or str(date.today())
    except Exception:  # noqa: BLE001
        snap = str(date.today())
    tpl = Path(args.template).read_text(encoding="utf-8")
    logo = Path(args.logo).read_text(encoding="utf-8")
    html = (tpl.replace("/*__LOGO__*/", logo)
               .replace("/*__ROWS__*/", json.dumps(rows, ensure_ascii=False, separators=(",", ":")))
               .replace("/*__ZONES__*/", json.dumps(zones, separators=(",", ":")))
               .replace("__SNAPSHOT__", snap))
    if args.standalone:
        html = ('<!doctype html><html lang="nb"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
                '<style>[hidden]{display:none!important}</style></head><body>' + html + '</body></html>')
    Path(args.out).write_text(html, encoding="utf-8")
    print(f"{args.out}: {len(rows)} rows ({inferred} prisområde inferred from station), snapshot {snap}, {len(html)/1024:.0f} kB")


if __name__ == "__main__":
    main()
