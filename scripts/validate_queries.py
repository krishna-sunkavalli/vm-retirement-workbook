#!/usr/bin/env python3
"""Run every Azure Resource Graph query in the generated workbook against the
live ARG service and report pass/fail. Requires `az login` + resource-graph ext."""

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "out")
WB = os.path.join(OUT, "azure-vm-retirement-workbook.json")

# Workbook parameter placeholders are not resolvable outside the portal.
SUBSTITUTIONS = {
    "{Subscriptions}": "",
    "{LifecycleFilter}": "RETIRE,EOL,OPTIONAL",
}


def walk(node, found):
    if isinstance(node, dict):
        c = node.get("content")
        if isinstance(c, dict) and c.get("queryType") == 1 and "query" in c:
            found.append((node.get("name", "?"), c["query"]))
        for v in node.values():
            walk(v, found)
    elif isinstance(node, list):
        for v in node:
            walk(v, found)


def structure_check(wb):
    """Offline structural checks on the workbook JSON."""
    problems = []
    items = wb.get("items", [])

    links = [i for i in items if i.get("type") == 11]
    tab_keys = set()
    for l in links:
        for link in l["content"]["links"]:
            tab_keys.add(link["subTarget"])
            if link.get("linkTarget") != "parameter":
                problems.append(f"link {link.get('linkLabel')} is not a parameter link")

    groups = [i for i in items if i.get("type") == 12]
    vis_values = set()
    for g in groups:
        cv = g.get("conditionalVisibility") or {}
        vis_values.add(cv.get("value"))
        if cv.get("parameterName") != "SelectedTab":
            problems.append(f"group {g.get('name')} keys off {cv.get('parameterName')}")

    if tab_keys != vis_values:
        problems.append(f"tab/group mismatch: tabs={sorted(tab_keys)} groups={sorted(vis_values)}")

    params = []
    for i in items:
        if i.get("type") == 9:
            params += [p["name"] for p in i["content"]["parameters"]]
    for required in ("Subscriptions", "LifecycleFilter"):
        if required not in params:
            problems.append(f"missing parameter {required}")

    # Every ARG query must be scoped and must not reference undefined parameters.
    found = []
    walk(wb, found)
    known = set(params) | {"SelectedTab"}
    import re
    for name, q in found:
        for ref in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", q):
            if ref not in known:
                problems.append(f"{name}: references unknown parameter {{{ref}}}")
        # A declared parameter used WITHOUT braces is never substituted by the
        # portal and fails at runtime with "Failed to resolve scalar expression".
        for p in params:
            for m in re.finditer(rf"(?<![{{\w]){re.escape(p)}(?![}}\w])", q):
                problems.append(
                    f"{name}: parameter '{p}' used without braces at offset {m.start()} "
                    f"- must be written as {{{p}}}")

    for i in items:
        for node in [i] + (i.get("content", {}).get("items", []) if i.get("type") == 12 else []):
            c = node.get("content", {})
            if c.get("queryType") == 1 and c.get("crossComponentResources") != ["{Subscriptions}"]:
                problems.append(f"{node.get('name')}: ARG query not scoped to {{Subscriptions}}")

    return problems


def main():
    wb = json.load(open(WB, encoding="utf-8"))

    print("Structural checks")
    problems = structure_check(wb)
    for p in problems:
        print(f"  FAIL  {p}")
    if not problems:
        print("  PASS  tabs, groups, parameters and query scoping are consistent")
    print()

    queries = []
    walk(wb, queries)
    print(f"Found {len(queries)} Azure Resource Graph queries\n")

    failures = 0
    for name, q in queries:
        for a, b in SUBSTITUTIONS.items():
            q = q.replace(a, b)
        with tempfile.NamedTemporaryFile("w", suffix=".kql", delete=False,
                                         encoding="utf-8") as f:
            f.write(q)
            path = f.name
        try:
            r = subprocess.run(
                ["az", "graph", "query", "-q", f"@{path}", "--first", "5", "-o", "json"],
                capture_output=True, text=True, shell=True)
            if r.returncode == 0:
                data = json.loads(r.stdout)
                cols = list(data["data"][0].keys()) if data["data"] else []
                print(f"  PASS  {name:<18} rows={data['count']:<4} cols={len(cols)}")
            else:
                failures += 1
                err = (r.stderr or r.stdout).strip().splitlines()
                msg = " | ".join(x.strip() for x in err if x.strip())[:400]
                print(f"  FAIL  {name:<18} {msg}")
        finally:
            os.unlink(path)

    print(f"\n{len(queries) - failures}/{len(queries)} queries passed")
    return 1 if (failures or problems) else 0


if __name__ == "__main__":
    sys.exit(main())
