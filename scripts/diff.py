#!/usr/bin/env python3
"""
Compare the freshly scraped CSVs in data/ with the versions in the last commit
(git HEAD) and write a change report.

Outputs:
  CHANGES.md          human-readable report (used as the alert body)
  changes.json        machine-readable summary
  $GITHUB_OUTPUT      has_changes=true|false, summary=<one line>

Rows are compared as whole rows. A removed row and an added row that share the
same key (config.key_columns) are reported as a *change* with the differing
columns highlighted. config.alert_filters restricts which rows are alerted on;
the full data is stored regardless.

Usage: python scripts/diff.py [--config config.yml] [--data data] [--ref HEAD]
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml


def git_show(ref: str, path: str) -> str | None:
    try:
        return subprocess.run(["git", "show", f"{ref}:{path}"], check=True,
                              capture_output=True, text=True, encoding="utf-8").stdout
    except subprocess.CalledProcessError:
        return None


def read_csv_text(text: str) -> tuple[list[str], list[dict]]:
    text = text.lstrip("﻿")
    r = csv.DictReader(io.StringIO(text))
    return list(r.fieldnames or []), list(r)


def match_cols(header: list[str], patterns: list[str]) -> list[str]:
    out = []
    for h in header:
        hl = h.lower()
        if any(p.lower() in hl for p in patterns):
            out.append(h)
    return out


def row_passes(row: dict, header: list[str], alert_filters: dict) -> bool:
    if not alert_filters:
        return True
    for col_pat, wanted in alert_filters.items():
        cols = match_cols(header, [col_pat])
        if not cols:
            continue  # column not present in this table -> don't filter on it
        wanted_l = [str(w).lower() for w in wanted]
        if not any(any(w in str(row.get(c, "")).lower() for w in wanted_l) for c in cols):
            return False
    return True


def key_of(row: dict, keycols: list[str]) -> tuple:
    return tuple(str(row.get(c, "")).strip().lower() for c in keycols)


def fmt_row(row: dict, header: list[str], max_cols: int = 8) -> str:
    parts = []
    for h in header[:max_cols]:
        v = str(row.get(h, "")).strip()
        if v:
            parts.append(f"{h}: {v}")
    return "; ".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--data", default="data")
    ap.add_argument("--ref", default="HEAD")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    alert_filters = cfg.get("alert_filters") or {}
    key_patterns = cfg.get("key_columns") or []

    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    report: list[str] = [f"# Statnett tilknytningssaker – endringer {now}", ""]
    summary = {"generated": now, "tables": []}
    total_alerts = 0

    for path in sorted(Path(args.data).rglob("*.csv")):
        rel = path.as_posix()
        new_header, new_rows = read_csv_text(path.read_text(encoding="utf-8"))
        old_text = git_show(args.ref, rel)
        entry = {"table": rel, "new": len(new_rows)}
        if old_text is None:
            entry.update({"status": "new_table"})
            summary["tables"].append(entry)
            report.append(f"## {rel}\n\nNy tabell (første kjøring) – {len(new_rows)} rader.\n")
            continue
        old_header, old_rows = read_csv_text(old_text)
        entry["old"] = len(old_rows)

        if old_header != new_header:
            entry.update({"status": "schema_changed", "old_header": old_header, "new_header": new_header})
            summary["tables"].append(entry)
            total_alerts += 1
            report.append(f"## {rel}\n\n**Kolonnene er endret** (Statnett har trolig republisert rapporten).\n\n"
                          f"- Før: {', '.join(old_header)}\n- Nå: {', '.join(new_header)}\n")
            continue

        def sig(r):
            return tuple(str(r.get(h, "")).strip() for h in new_header)

        old_set = {sig(r): r for r in old_rows}
        new_set = {sig(r): r for r in new_rows}
        removed = [old_set[s] for s in old_set.keys() - new_set.keys()]
        added = [new_set[s] for s in new_set.keys() - old_set.keys()]

        keycols = match_cols(new_header, key_patterns)
        changed = []
        if keycols:
            rem_by_key: dict[tuple, list] = {}
            for r in removed:
                rem_by_key.setdefault(key_of(r, keycols), []).append(r)
            still_added = []
            for r in added:
                k = key_of(r, keycols)
                if rem_by_key.get(k):
                    old = rem_by_key[k].pop(0)
                    diffs = {h: (old.get(h, ""), r.get(h, "")) for h in new_header
                             if str(old.get(h, "")).strip() != str(r.get(h, "")).strip()}
                    changed.append((old, r, diffs))
                else:
                    still_added.append(r)
            added = still_added
            removed = [r for lst in rem_by_key.values() for r in lst]

        # alert filter
        f_added = [r for r in added if row_passes(r, new_header, alert_filters)]
        f_removed = [r for r in removed if row_passes(r, new_header, alert_filters)]
        f_changed = [c for c in changed if row_passes(c[1], new_header, alert_filters)
                     or row_passes(c[0], new_header, alert_filters)]

        entry.update({"status": "ok", "added": len(added), "removed": len(removed), "changed": len(changed),
                      "alert_added": len(f_added), "alert_removed": len(f_removed), "alert_changed": len(f_changed)})
        summary["tables"].append(entry)
        n_alert = len(f_added) + len(f_removed) + len(f_changed)
        total_alerts += n_alert
        if n_alert == 0:
            continue

        report.append(f"## {rel}")
        report.append(f"\n{len(old_rows)} → {len(new_rows)} rader. "
                      f"Nye: {len(added)}, fjernet: {len(removed)}, endret: {len(changed)}"
                      + (f" (etter filter: {len(f_added)}/{len(f_removed)}/{len(f_changed)})" if alert_filters else "")
                      + "\n")
        if f_added:
            report.append("### Nye rader")
            report += [f"- ➕ {fmt_row(r, new_header)}" for r in f_added[:200]]
            report.append("")
        if f_removed:
            report.append("### Fjernede rader")
            report += [f"- ➖ {fmt_row(r, new_header)}" for r in f_removed[:200]]
            report.append("")
        if f_changed:
            report.append("### Endrede rader")
            for old, new, diffs in f_changed[:200]:
                key = "; ".join(f"{c}: {new.get(c, '')}" for c in keycols if str(new.get(c, "")).strip())
                d = ", ".join(f"{h}: {a!r} → {b!r}" for h, (a, b) in diffs.items())
                report.append(f"- ✏️ {key} — {d}")
            report.append("")

    if total_alerts == 0:
        report.append("Ingen endringer i overvåkede rader.")
    summary["total_alerts"] = total_alerts

    Path("CHANGES.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    Path("changes.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n".join(report))

    one_line = "; ".join(
        f"{Path(t['table']).stem}: +{t.get('alert_added', 0)}/-{t.get('alert_removed', 0)}/~{t.get('alert_changed', 0)}"
        for t in summary["tables"] if t.get("status") == "ok" and
        (t.get("alert_added") or t.get("alert_removed") or t.get("alert_changed"))
    ) or ("kolonner endret" if any(t.get("status") == "schema_changed" for t in summary["tables"]) else "ingen endringer")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"has_changes={'true' if total_alerts else 'false'}\n")
            f.write(f"summary={one_line[:200]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
