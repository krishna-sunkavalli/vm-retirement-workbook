"""Generate docs/QUERIES.md from the built workbook.

Every Azure Resource Graph query the workbook runs is extracted verbatim, so
readers can audit exactly what is executed against their tenant without opening
the 1.3 MB workbook JSON. The embedded lifecycle lookup literal is elided - it
is a 84 KB machine-generated dynamic bag, not something a human reads.
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WB = os.path.join(ROOT, "out", "azure-vm-retirement-workbook.json")
DOCS = os.path.join(ROOT, "docs")

# Human descriptions keyed by the workbook item name.
DESCRIPTIONS = {
    "ov-tiles": ("Overview", "Fleet summary tiles: impacted resources, instances, "
                 "retirement vs end-of-life split, count below readiness 60, "
                 "fleet readiness score and days to the next retirement."),
    "ov-urgency": ("Overview", "Impacted instances grouped by urgency band."),
    "ov-gen": ("Overview", "Impacted resources by the actual Hyper-V generation "
               "read from each managed OS disk."),
    "ov-complexity": ("Overview", "Resource and instance counts by migration "
                      "complexity band, with average readiness."),
    "ov-commercial": ("Overview", "Series in scope for the 1 February 2027 pricing "
                      "update and the Reserved Instance position for each."),
    "ov-topsizes": ("Overview", "The 25 most-used affected sizes with their "
                    "recommended target and next retirement date."),
    "imp-grid": ("Impacted resources", "Every affected VM and scale set with region, "
                 "actual generation, recommended target, target region availability, "
                 "readiness score and named blockers."),
    "map-grid": ("Target mapping", "One row per affected size: current spec, "
                 "recommended target, memory-preserving alternative and retirement date."),
    "map-detail": ("Target mapping", "Alternate targets, size considerations, series "
                   "spec deltas, pricing-update flag and Reserved Instance status."),
    "pre-grid": ("Prerequisites", "Prerequisites this fleet actually triggers, ranked "
                 "Blocking / Validate / Plan, with the affected resource count."),
    "plan-waves": ("Migration plan", "Suggested migration waves derived from readiness "
                   "score and production tagging."),
    "plan-burndown": ("Migration plan", "Instances to migrate by retirement quarter."),
    "plan-longpole": ("Migration plan", "Resources scoring below 60 - the workloads "
                      "with the longest lead time."),
    "about-coverage": ("About", "How many resources matched the lifecycle catalogue, "
                       "by lifecycle stage."),
    "about-allsizes": ("About", "Every VM size in scope, including sizes with no "
                       "lifecycle action, as a coverage cross-check."),
}

BAG = re.compile(r"parse_json\('\{.*?\}'\)", re.S)


def elide(query):
    """Replace the machine-generated lookup literals with a placeholder."""
    return BAG.sub("parse_json('{ ...lifecycle lookup, see note below... }')", query)


def common_prefix(queries):
    """Longest shared leading run of lines across the ARG queries.

    Every fleet query is the same base inventory-and-scoring pipeline with a
    different tail. Printing that prefix once keeps the document readable.
    """
    line_lists = [q.strip().split("\n") for q in queries]
    shortest = min(len(x) for x in line_lists)
    n = 0
    while n < shortest and len({x[n] for x in line_lists}) == 1:
        n += 1
    return line_lists[0][:n]


def collect(items, out, tab=None):
    for it in items:
        if it.get("type") == 12:
            collect(it["content"]["items"], out, tab)
        elif it.get("type") == 3 and it["content"].get("queryType") == 1:
            out.append((it["name"], it["content"]))
    return out


def main():
    wb = json.load(open(WB, encoding="utf-8"))
    queries = collect(wb["items"], [])
    os.makedirs(DOCS, exist_ok=True)

    elided = {name: elide(c["query"]) for name, c in queries}
    # about-allsizes is a standalone coverage query, not part of the fleet
    # pipeline, so it would collapse the shared prefix to nothing.
    fleet = [q for n, q in elided.items() if n != "about-allsizes"]
    prefix = common_prefix(fleet)
    prefix_text = "\n".join(prefix)

    by_tab = {}
    for name, content in queries:
        tab, desc = DESCRIPTIONS.get(name, ("Other", ""))
        body = elided[name]
        if name != "about-allsizes" and body.strip().startswith(prefix_text):
            body = body.strip()[len(prefix_text):].strip()
            shared = True
        else:
            body = body.strip()
            shared = False
        by_tab.setdefault(tab, []).append((name, desc, content, body, shared))

    lines = [
        "# Azure Resource Graph queries",
        "",
        f"The workbook runs **{len(queries)} read-only Azure Resource Graph queries**. "
        "Nothing is written, no agent is required, and no data leaves the tenant. "
        "Every query is scoped to the subscriptions selected in the `Subscriptions` "
        "parameter.",
        "",
        "This file is generated by `scripts/build_queries_doc.py` - edit the build "
        "script, not this file.",
        "",
        "## Contents",
        "",
        "- [The shared base query](#the-shared-base-query)",
    ]
    order = ["Overview", "Impacted resources", "Target mapping", "Prerequisites",
             "Migration plan", "About", "Other"]
    tabs = [t for t in order if t in by_tab]
    for tab in tabs:
        anchor = tab.lower().replace(" ", "-")
        lines.append(f"- [{tab}](#{anchor}) ({len(by_tab[tab])} queries)")
    lines += [
        "",
        "## A note on the embedded lookup",
        "",
        "Azure Resource Graph supports neither `let` nor `datatable`, so the "
        "628-size lifecycle catalogue is embedded in each query as a "
        "`parse_json('{...}')` dynamic literal indexed by VM size. That literal is "
        "about 84 KB and is elided throughout this document for readability. "
        "It is generated from `references/sku-migration-map.json`; warning codes are "
        "stored as single characters and region lists as hex indices into a shared "
        "region table to keep the generated workbook near 1.3 MB rather than 3.5 MB.",
        "",
        "The fields packed into each lookup record, in order:",
        "",
        "| # | Field | Meaning |",
        "| ---: | --- | --- |",
        "| 0 | Lifecycle | `RETIRE`, `EOL` or `OPTIONAL` |",
        "| 1 | SeriesGroup | Announcement grouping, used for the source link |",
        "| 2 | RetirementDate | Announced retirement date, empty when none |",
        "| 3 | RecommendedTarget | Default same-or-better replacement size |",
        "| 4 | MemoryPreservingTarget | Alternative when the default reduces RAM |",
        "| 5-6 | TargetVcpu, TargetMemoryGB | Target spec |",
        "| 7-8 | CurrentVcpu, CurrentMemoryGB | Source spec |",
        "| 9 | WarnCodes | Single-character prerequisite codes |",
        "| 10 | AlternateTargets | Up to three alternates |",
        "| 11 | RiEnded | Reserved Instance purchase and renewal end date |",
        "| 12 | Series | Size series name |",
        "| 13 | PriceUpdate | `1` when the series is in scope for 1 Feb 2027 |",
        "| 14-15 | MpVcpu, MpMemoryGB | Memory-preserving target spec |",
        "",
        "## The shared base query",
        "",
        "Every fleet query below starts with this pipeline. It inventories the VMs "
        "and scale sets, joins each to its managed OS disk to read the real "
        "`hyperVGeneration`, looks the size up in the lifecycle catalogue, and "
        "computes the readiness score from the resulting attributes. Per-query "
        "tails are shown in the sections that follow and should be appended to this "
        "prefix.",
        "",
        "```kusto",
        prefix_text,
        "```",
        "",
    ]

    for tab in tabs:
        lines += [f"## {tab}", ""]
        for name, desc, content, body, shared in by_tab[tab]:
            lines += [
                f"### `{name}`",
                "",
                desc or "_No description._",
                "",
                f"- **Visualization:** {content.get('visualization') or 'table'}",
                f"- **Scope:** `{', '.join(content.get('crossComponentResources', []))}`",
            ]
            if shared:
                lines.append("- **Base:** appended to the shared base query above")
            lines += ["", "```kusto", body, "```", ""]

    path = os.path.join(DOCS, "QUERIES.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"{len(queries)} queries -> {path} ({os.path.getsize(path)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
