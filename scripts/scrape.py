#!/usr/bin/env python3
"""
Scrape the table visuals in Statnett's public Power BI reports on
"Statistikk om tilknytningssaker" and write them as CSV files.

How it works (Power BI "publish to web" protocol, verified 2026-09-22):
  1. The report URL carries a base64 JSON blob ("r=") with the resource key (k).
  2. GET app.powerbi.com/view?r=...  -> HTML naming the backend cluster
     (resolvedClusterUri), e.g. https://wabi-north-europe-api.analysis.windows.net
  3. GET {cluster}/public/reports/{k}/modelsAndExploration?preferReadOnlySession=true
     (header X-PowerBI-ResourceKey: k) -> model id + dataset id + the report
     definition. Statnett's reports use the Fabric "PBIR" layout: the definition
     is a JSON string in exploration.explorationContent.explorationDocument with
     report.json / pages/*/page.json / visuals/*/visual.json content.
  4. For every table visual: build a semantic query from the visual's
     query.queryState projections, merge the report/page/visual filterConfig
     Where-clauses, and POST it to {cluster}/public/reports/querydata?synchronous=true
     asking for up to config.max_rows rows.
  5. Decode the compressed "DSR" result (ValueDicts + repeat/null bitmasks) -> CSV.

Slicers on the page (date ranges, drop-downs) are NOT applied, so the CSV is the
unfiltered superset of what the page shows.

Nothing here is an official API. When Statnett republishes a report the resource
key may change (copy the new "Fullskjermvisning" link into config.yml).

Usage:
  python scripts/scrape.py [--config config.yml] [--out data] [--debug] [--only NAME]
"""
from __future__ import annotations

import argparse
import base64
import copy
import csv
import json
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")
DEFAULT_CLUSTER = "https://wabi-north-europe-api.analysis.windows.net"
TABLE_VISUAL_TYPES = {"tableEx", "pivotTable", "table", "matrix"}
DSR_DATETIME_TYPE = 7   # values are epoch milliseconds


# --------------------------------------------------------------------------- helpers

def log(*a):
    print(*a, file=sys.stderr, flush=True)


def slug(s: str, maxlen: int = 70) -> str:
    s = re.sub(r"[^\w\-]+", "_", str(s).strip(), flags=re.UNICODE).strip("_")
    return (s or "x")[:maxlen]


def decode_r(url: str) -> dict:
    m = re.search(r"[?&]r=([^&]+)", url)
    if not m:
        raise ValueError(f"No r= parameter in {url}")
    b64 = m.group(1)
    b64 += "=" * (-len(b64) % 4)
    return json.loads(base64.b64decode(b64))


def maybe_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return v
    return v


def literal(v):
    """Power BI literal 'Text' -> Text."""
    if isinstance(v, str) and len(v) >= 2 and v[0] == "'" and v[-1] == "'":
        return v[1:-1]
    return v


# --------------------------------------------------------------------------- query building

class QueryBuilder:
    """Builds one semantic query from a PBIR visual + its applicable filters."""

    def __init__(self):
        self.aliases: dict[str, str] = {}
        self.from_: list[dict] = []

    def alias(self, entity: str) -> str:
        if entity not in self.aliases:
            a = entity[0].lower() + (str(len(self.aliases)) if self.aliases else "")
            self.aliases[entity] = a
            self.from_.append({"Name": a, "Entity": entity, "Type": 0})
        return self.aliases[entity]

    def remap_entity(self, node):
        """SourceRef{Entity} -> SourceRef{Source: alias}."""
        if isinstance(node, list):
            return [self.remap_entity(x) for x in node]
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "SourceRef" and isinstance(v, dict) and "Entity" in v:
                    out[k] = {"Source": self.alias(v["Entity"])}
                else:
                    out[k] = self.remap_entity(v)
            return out
        return node

    @staticmethod
    def remap_source(node, mapping: dict):
        if isinstance(node, list):
            return [QueryBuilder.remap_source(x, mapping) for x in node]
        if isinstance(node, dict):
            out = {}
            for k, v in node.items():
                if k == "SourceRef" and isinstance(v, dict) and "Source" in v:
                    out[k] = {"Source": mapping.get(v["Source"], v["Source"])}
                else:
                    out[k] = QueryBuilder.remap_source(v, mapping)
            return out
        return node

    def build(self, visual: dict, filter_configs: list) -> tuple[dict, list[str]]:
        select, names = [], []
        qs = (visual.get("query") or {}).get("queryState") or {}
        for role, rs in qs.items():
            for pr in rs.get("projections", []):
                field = self.remap_entity(pr["field"])
                sel = dict(field)
                sel["Name"] = pr.get("queryRef") or role
                select.append(sel)
                names.append(pr.get("displayName") or column_of(pr["field"]) or pr.get("nativeQueryRef") or sel["Name"])
        where = []
        for fc in filter_configs:
            for f in (fc or {}).get("filters", []) or []:
                flt = f.get("filter")
                if not isinstance(flt, dict) or not flt.get("Where"):
                    continue
                mapping = {fr["Name"]: self.alias(fr["Entity"]) for fr in flt.get("From", [])}
                for w in flt["Where"]:
                    w2 = self.remap_source(w, mapping)
                    if w2 not in where:
                        where.append(w2)
        q = {"Version": 2, "From": self.from_, "Select": select}
        if where:
            q["Where"] = where
        return q, names


def categorical_filter_value(fc: dict | None, property_substring: str) -> str | None:
    """Return the single literal of a Categorical 'In' filter on a column whose name contains the substring."""
    for f in (fc or {}).get("filters", []) or []:
        prop = (column_of(f.get("field", {})) or "").lower()
        flt = f.get("filter") or {}
        if property_substring in prop and flt.get("Where"):
            try:
                vals = flt["Where"][0]["Condition"]["In"]["Values"]
                if len(vals) == 1 and len(vals[0]) == 1:
                    return literal(vals[0][0]["Literal"]["Value"])
            except (KeyError, IndexError, TypeError):
                pass
    return None


def column_of(field: dict) -> str | None:
    for k in ("Column", "Measure"):
        if k in field:
            return field[k].get("Property")
    if "Aggregation" in field:
        return column_of(field["Aggregation"].get("Expression", {}))
    return None


# --------------------------------------------------------------------------- Power BI client

class PublicReport:
    def __init__(self, name: str, url: str, session: requests.Session, debug_dir: Path | None):
        self.name = name
        self.url = url
        self.s = session
        self.debug_dir = debug_dir
        r = decode_r(url)
        self.resource_key: str = r["k"]
        self.cluster = self._resolve_cluster()
        self.models: list[dict] = []
        self.report_id = None
        self.doc: dict = {}

    def _resolve_cluster(self) -> str:
        """The embed page declares e.g. resolvedClusterUri = 'https://wabi-north-europe-redirect.analysis.windows.net/'.
        The Power BI front end talks to the matching '-api' host; the '-redirect' host 307s there for other clients."""
        try:
            html = self.s.get(self.url, timeout=60).text
            m = re.search(r"""resolvedClusterUri\\?["']?\s*[:=]\s*\\?["'](https:[^"'\\]+)""", html)
            if m:
                uri = m.group(1).replace("\\/", "/").rstrip("/")
                uri = uri.replace("-redirect.analysis.windows.net", "-api.analysis.windows.net")
                log(f"[{self.name}] cluster: {uri}")
                return uri
            log(f"[{self.name}] resolvedClusterUri not found in HTML; using default {DEFAULT_CLUSTER}")
        except Exception as e:  # noqa: BLE001
            log(f"[{self.name}] cluster lookup failed ({e}); using default {DEFAULT_CLUSTER}")
        return DEFAULT_CLUSTER

    def _headers(self) -> dict:
        return {
            "X-PowerBI-ResourceKey": self.resource_key,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "ActivityId": str(uuid.uuid4()),
            "RequestId": str(uuid.uuid4()),
            "Referer": "https://app.powerbi.com/",
            "Origin": "https://app.powerbi.com",
        }

    def load(self):
        u = f"{self.cluster}/public/reports/{self.resource_key}/modelsAndExploration?preferReadOnlySession=true"
        resp = self.s.get(u, headers=self._headers(), timeout=90)
        if resp.status_code != 200:
            raise RuntimeError(f"[{self.name}] modelsAndExploration -> HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.history:  # followed a redirect – keep talking to the final host
            final = re.match(r"https://[^/]+", resp.url)
            if final and final.group(0) != self.cluster:
                log(f"[{self.name}] redirected to cluster {final.group(0)}")
                self.cluster = final.group(0)
        j = resp.json()
        if self.debug_dir:
            (self.debug_dir / f"{self.name}__modelsAndExploration.json").write_text(
                json.dumps(j, ensure_ascii=False, indent=1), encoding="utf-8")
        self.models = j.get("models") or []
        if not self.models:
            raise RuntimeError(f"[{self.name}] no models in modelsAndExploration response")
        ex = j.get("exploration") or {}
        self.report_id = ex.get("reportId") or ex.get("id")
        content = ex.get("explorationContent") or {}
        doc = maybe_json(content.get("explorationDocument"))
        if not isinstance(doc, dict) or "pages" not in doc:
            raise RuntimeError(f"[{self.name}] report definition not in the expected PBIR format "
                               f"(exploration keys: {list(ex.keys())}). Run with --debug and inspect.")
        self.doc = doc
        pkg = j.get("package") or {}
        log(f"[{self.name}] model {self.models[0].get('id')} dataset {self.models[0].get('dbName')} "
            f"package '{pkg.get('name')}' pages {len(doc['pages'].get('pages', []))}")

    def table_visuals(self):
        report_fc = ((self.doc.get("report") or {}).get("content") or {}).get("filterConfig")
        for page in self.doc["pages"].get("pages", []):
            pc = page.get("content") or {}
            page_fc = pc.get("filterConfig")
            for vc in page.get("visualContainers", []):
                c = vc.get("content") or {}
                vis = c.get("visual") or {}
                if vis.get("visualType") not in TABLE_VISUAL_TYPES:
                    continue
                title = None
                try:
                    title = literal(vis["visualContainerObjects"]["title"][0]["properties"]["text"]["expr"]["Literal"]["Value"])
                except Exception:  # noqa: BLE001
                    pass
                # Statnett's list pages have one table per "Forbruk / produksjon" value, both with
                # the same (hidden) title. Label the table by that filter value when present.
                label = categorical_filter_value(c.get("filterConfig"), "produksjon")
                if label:
                    title = f"Liste {label}"
                yield {
                    "page": pc.get("displayName") or pc.get("name"),
                    "title": title or c.get("name"),
                    "visual_name": c.get("name"),
                    "type": vis["visualType"],
                    "visual": vis,
                    "filter_configs": [report_fc, page_fc, c.get("filterConfig")],
                }

    def run_query(self, visual: dict, max_rows: int) -> tuple[list[str], list[list]]:
        model = self.models[0]
        q, names = QueryBuilder().build(visual["visual"], visual["filter_configs"])
        n = len(q["Select"])
        body = {
            "version": "1.0.0",
            "queries": [{
                "Query": {"Commands": [{"SemanticQueryDataShapeCommand": {
                    "Query": q,
                    "Binding": {"Primary": {"Groupings": [{"Projections": list(range(n))}]},
                                "DataReduction": {"DataVolume": 6, "Primary": {"Window": {"Count": max_rows}}},
                                "Version": 1}}}]},
                "CacheKey": "",
                "QueryId": "",
                "ApplicationContext": {"DatasetId": model.get("dbName"), "Sources": [{"ReportId": self.report_id}]},
            }],
            "cancelQueries": [],
            "modelId": model.get("id"),
        }
        u = f"{self.cluster}/public/reports/querydata?synchronous=true"
        resp = self.s.post(u, headers=self._headers(), data=json.dumps(body), timeout=180)
        tag = f"{self.name}__{slug(visual['page'])}__{slug(visual['title'])}"
        if self.debug_dir:
            (self.debug_dir / f"{tag}__request.json").write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
            (self.debug_dir / f"{tag}__response.json").write_text(resp.text, encoding="utf-8")
        if resp.status_code != 200:
            raise RuntimeError(f"[{tag}] querydata -> HTTP {resp.status_code}: {resp.text[:400]}")
        return decode_dsr(resp.json(), q, names)


# --------------------------------------------------------------------------- DSR decoding

def decode_dsr(resp: dict, q: dict, names: list[str]) -> tuple[list[str], list[list]]:
    results = resp.get("results") or []
    if not results:
        raise RuntimeError(f"querydata returned no results: {json.dumps(resp)[:400]}")
    data = results[0]["result"]["data"]
    dsr = data.get("dsr", {})
    # descriptor Select: Value ("G0"/"M0") -> Name ("Saker.Saksnr.")
    desc_by_value = {d.get("Value"): d.get("Name") for d in data.get("descriptor", {}).get("Select", [])}
    friendly = {sel["Name"]: names[i] for i, sel in enumerate(q["Select"])}

    header: list[str] = []
    types: list[int] = []
    rows: list[list] = []
    for ds in dsr.get("DS", []):
        dicts = ds.get("ValueDicts", {})
        if ds.get("RT"):
            log("  WARNING: result has a restart token (RT) – table may be truncated; raise max_rows")
        for ph in ds.get("PH", []):
            for dm_key, dm in ph.items():
                if not dm_key.startswith("DM"):
                    continue
                schema = None
                prev = None
                for row in dm:
                    if "S" in row:
                        schema = row["S"]
                        if not header:
                            for col in schema:
                                raw = desc_by_value.get(col["N"], col["N"])
                                header.append(friendly.get(raw, raw))
                                types.append(col.get("T"))
                    if schema is None:
                        continue
                    C = row.get("C", [])
                    R = row.get("R", 0)
                    NULLS = row.get("Ø", 0)
                    if not C and R == 0 and NULLS == 0:
                        continue
                    vals = []
                    ci = 0
                    for i, col in enumerate(schema):
                        bit = 1 << i
                        if R & bit and prev is not None:
                            v = prev[i]
                        elif NULLS & bit:
                            v = None
                        else:
                            v = C[ci] if ci < len(C) else None
                            ci += 1
                            if "DN" in col and isinstance(v, int) and not isinstance(v, bool):
                                try:
                                    v = dicts[col["DN"]][v]
                                except (KeyError, IndexError):
                                    pass
                        if types[i] == DSR_DATETIME_TYPE and isinstance(v, (int, float)):
                            v = datetime.fromtimestamp(v / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
                        vals.append(v)
                    prev = vals
                    rows.append(vals)
    seen: dict[str, int] = {}
    for i, h in enumerate(header):
        if h in seen:
            seen[h] += 1
            header[i] = f"{h}_{seen[h]}"
        else:
            seen[h] = 0
    return header, rows


# --------------------------------------------------------------------------- main

def write_csv(path: Path, header: list[str], rows: list[list]):
    path.parent.mkdir(parents=True, exist_ok=True)

    def norm(v):
        if v is None:
            return ""
        if isinstance(v, float) and v.is_integer():
            return str(int(v))
        return str(v)

    out = sorted((list(map(norm, r)) for r in rows))
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config.yml")
    ap.add_argument("--out", default="data")
    ap.add_argument("--debug", action="store_true", help="dump raw requests/responses to debug/")
    ap.add_argument("--only", help="scrape only this report name")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    max_rows = int(cfg.get("max_rows", 50000))
    out = Path(args.out)
    debug_dir = Path("debug") if args.debug else None
    if debug_dir:
        debug_dir.mkdir(exist_ok=True)

    s = requests.Session()
    s.headers.update({"User-Agent": UA})

    failures = 0
    written = 0
    for name, rc in cfg["reports"].items():
        if args.only and name != args.only:
            continue
        try:
            rep = PublicReport(name, rc["url"], s, debug_dir)
            rep.load()
            visuals = list(rep.table_visuals())
            log(f"[{name}] table visuals found: {len(visuals)}")
            if not visuals:
                log(f"[{name}] no table visuals – run with --debug and inspect "
                    f"debug/{name}__modelsAndExploration.json (visualType values)")
            for v in visuals:
                header, rows = rep.run_query(v, max_rows)
                fname = out / name / f"{slug(v['page'])}__{slug(v['title'])}.csv"
                write_csv(fname, header, rows)
                written += 1
                log(f"  {fname}: {len(rows)} rows x {len(header)} cols  [{', '.join(header)[:150]}]")
        except Exception as e:  # noqa: BLE001
            failures += 1
            log(f"[{name}] FAILED: {e}")

    log(f"done: {written} tables written, {failures} report(s) failed")
    if written == 0:
        sys.exit(2)
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
