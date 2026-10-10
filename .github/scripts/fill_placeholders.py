"""Fill the {{RESULT: <metric> | source: <path>}} placeholders in README.md and docs/adr/*.md.

Every value is read from a committed result file; nothing is computed except two simple
differences. A cell that the source marks "-" (not measured) becomes
"not measured (time-boxed run)". Placeholders this script cannot resolve are left in place and
listed, so a person can fill them by hand.

    python .github/scripts/fill_placeholders.py            # dry run: print metric -> value
    python .github/scripts/fill_placeholders.py --write    # rewrite the files
    python .github/scripts/fill_placeholders.py --load-summary <summary.json> --write

Needs only the standard library.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLACEHOLDER = re.compile(r"\{\{RESULT: (?P<metric>[^|}]+?) \| source: (?P<source>[^}|]+?)\}\}")
NOT_MEASURED = "not measured (time-boxed run)"

MAIN_COLUMNS = {
    "nDCG@10": "nDCG@10",
    "Recall@20": "R@20",
    "p95 e2e latency ms": "p95 e2e ms",
    "Cost/query": "Cost/query (est.)",
}


def parse_table(text: str, heading: str | None = None) -> list[dict[str, str]]:
    """Rows of the first markdown table at or after `heading` (a line prefix), as dicts."""
    lines = text.splitlines()
    start = 0
    if heading is not None:
        start = next((i for i, ln in enumerate(lines) if ln.startswith(heading)), -1)
        if start < 0:
            return []
    i = start
    while i < len(lines) and not lines[i].lstrip().startswith("|"):
        i += 1
    block = []
    while i < len(lines) and lines[i].lstrip().startswith("|"):
        block.append(lines[i])
        i += 1
    if len(block) < 3:
        return []

    def cells(line: str) -> list[str]:
        return [c.strip() for c in line.strip().strip("|").split("|")]

    header = cells(block[0])
    return [dict(zip(header, cells(b), strict=False)) for b in block[2:]]


def clean(cell: str | None) -> str:
    if cell is None:
        return NOT_MEASURED
    value = re.sub(r"\s*\(est\.\)$", "", cell.strip().strip("`"))
    return NOT_MEASURED if value in ("", "-") else value


def number(cell: str | None) -> float | None:
    value = clean(cell).replace(",", "")
    try:
        return float(value)
    except ValueError:
        return None


class Sources:
    def __init__(self, root: Path, load_summary: Path | None):
        self.root = root
        self.load_summary = load_summary
        self._cache: dict[str, str] = {}

    def text(self, rel: str) -> str:
        if rel not in self._cache:
            path = self.root / rel
            self._cache[rel] = path.read_text(encoding="utf-8") if path.exists() else ""
        return self._cache[rel]

    # --- evals/results.md -------------------------------------------------------------------
    def grid_row(self, row: int) -> dict[str, str]:
        for r in parse_table(self.text("evals/results.md"), "## Ablation grid"):
            if r.get("#") == str(row):
                return r
        return {}

    def stage_cell(self, stage: str, config: str, which: str) -> str:
        table = parse_table(self.text("evals/results.md"), "## Per-stage latency")
        by_stage = {r.get("Stage", ""): r for r in table}
        row = by_stage.get(stage) or (by_stage.get("server total") if stage == "total" else None)
        if row is None:
            return NOT_MEASURED
        cell = row.get(f"`{config}`", "-")
        parts = [p.strip() for p in cell.split("/")]
        if len(parts) != 2 or parts[0] == "-":
            return NOT_MEASURED
        return parts[0 if which == "p50" else 1]

    # --- other files ------------------------------------------------------------------------
    def bench_value(self, index: str, setting: str, column: str) -> str:
        rows = parse_table(self.text("indexer/bench/index_bench.md"), "## Unfiltered recall")
        for r in rows:
            if r.get("Index") == index and r.get("Setting") == setting:
                return clean(r.get(column))
        return ""

    def build_seconds(self, index: str) -> str:
        for r in parse_table(self.text("indexer/bench/index_bench.md"), "## Build time"):
            if r.get("Index") == index:
                return clean(r.get("Seconds"))
        return ""

    def most_selective_filter(self) -> str:
        rows = parse_table(self.text("indexer/bench/index_bench.md"), "## Filtered HNSW recall")
        rows = [r for r in rows if number(r.get("Selectivity")) is not None]
        if not rows:
            return ""
        r = min(rows, key=lambda x: number(x["Selectivity"]) or 0.0)
        return f"{clean(r['Recall@10'])} ({r['Filter']}, selectivity {r['Selectivity']})"

    def pushdown_recall(self) -> str:
        rows = parse_table(self.text("evals/results_filter_pushdown.md"))
        out = []
        for r in rows:
            name = r.get("Strategy", "").split(" ")[0].strip("`")
            out.append(f"{name} {clean(r.get('R@20'))}")
        return ", ".join(out)

    def load_p95(self) -> str:
        if self.load_summary is None:
            return ""
        data = json.loads(self.load_summary.read_text(encoding="utf-8"))
        metric = data["metrics"]["http_req_duration{scenario:retrieval}"]
        values = metric.get("values", metric)
        return f"{values['p(95)']:.0f}"


def resolve(metric: str, source: str, src: Sources) -> str | None:
    m = re.fullmatch(r"(nDCG@10|Recall@20|p95 e2e latency ms|Cost/query) row (\d)", metric)
    if m and source == "evals/results.md":
        row = src.grid_row(int(m[2]))
        return clean(row.get(MAIN_COLUMNS[m[1]])) if row else None
    if metric == "p95 rerank stage ms":
        row = src.grid_row(5)
        return clean(row.get("p95 rerank ms")) if row else None
    m = re.fullmatch(r"(p50|p95) (\S+) ms, row 6", metric)
    if m and source == "evals/results.md":
        value = src.stage_cell(m[2], "06-routing", m[1])
        return value if src.text("evals/results.md") else None
    m = re.fullmatch(r"nDCG@10 change from row (\d) to row (\d)", metric)
    if m:
        a, b = (number(src.grid_row(int(g)).get("nDCG@10")) for g in m.groups())
        if a is None or b is None:
            return NOT_MEASURED if src.grid_row(int(m[1])) else None
        return f"{b - a:+.3f}"
    m = re.fullmatch(r"(HNSW|IVFFlat) build seconds", metric)
    if m:
        return src.build_seconds(m[1]) or None
    m = re.fullmatch(r"HNSW recall@10 at ef_search (\d+)", metric)
    if m:
        return src.bench_value("HNSW ef_search", m[1], "Recall@10") or None
    m = re.fullmatch(r"IVFFlat recall@10 at probes (\d+)", metric)
    if m:
        return src.bench_value("IVFFlat probes", m[1], "Recall@10") or None
    if metric == "filtered recall@10 at the most selective filter":
        return src.most_selective_filter() or None
    if metric == "recall@20 for pre, post and auto":
        return src.pushdown_recall() or None
    if metric == "retrieval scenario p95 ms at 20 rps":
        return src.load_p95() or None
    return None  # free-form placeholders (the negative finding) are filled by hand


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--write", action="store_true", help="rewrite the files (default: dry run)")
    ap.add_argument("--load-summary", type=Path, help="k6 summary JSON of the retrieval scenario")
    ap.add_argument("--root", type=Path, default=ROOT)
    args = ap.parse_args()

    src = Sources(args.root, args.load_summary)
    files = [args.root / "README.md", *sorted((args.root / "docs/adr").glob("*.md"))]
    filled = unresolved = 0
    for path in files:
        text = path.read_text(encoding="utf-8")

        def repl(match: re.Match[str], path: Path = path) -> str:
            nonlocal filled, unresolved
            value = resolve(match["metric"].strip(), match["source"].strip(), src)
            if value is None:
                unresolved += 1
                print(f"UNRESOLVED {path.relative_to(args.root)}: {match['metric'][:70]}")
                return match[0]
            filled += 1
            print(f"{value:>34}  <- {match['metric'].strip()[:60]}")
            return value

        new = PLACEHOLDER.sub(repl, text)
        if args.write and new != text:
            path.write_text(new, encoding="utf-8")
    print(f"{filled} resolved, {unresolved} left (write={args.write})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
