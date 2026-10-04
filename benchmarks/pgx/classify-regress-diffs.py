#!/usr/bin/env python3
"""Classify regression.diffs lines as plan-only (EXPLAIN output) or semantic.

Usage: classify-regress-diffs.py <regress output dir>

For every changed line in <dir>/regression.diffs, look up whether that line of
the expected file (for '-') or of the actual result file (for '+') lies inside
an EXPLAIN result block, i.e. between a "QUERY PLAN" (or explain_* helper
function) column header and its "(N rows)" footer. A test is

  plan_only  if every changed line is inside such a block
  semantic   if any changed line is a statement result, error or row count

Writes <dir>/classification.json and prints a table.
"""

import json
import os
import re
import sys

HEADER_RE = re.compile(r"^\s*(QUERY PLAN|explain_\w+)\s*$")
FOOTER_RE = re.compile(r"^\(\d+ rows?\)$")
DIFF_RE = re.compile(r"^diff -U3 (\S+) (\S+)$")
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)")


def plan_lines(path):
    """Set of 1-based line numbers that belong to an EXPLAIN result block."""
    inside, out = False, set()
    with open(path, errors="replace") as f:
        for no, line in enumerate(f.read().split("\n"), 1):
            if HEADER_RE.match(line):
                inside = True
            if inside:
                out.add(no)
            if inside and FOOTER_RE.match(line):
                inside = False
    return out


def main():
    outdir = sys.argv[1]
    tests, cur = {}, None
    with open(os.path.join(outdir, "regression.diffs"), errors="replace") as f:
        for line in f:
            line = line.rstrip("\n")
            m = DIFF_RE.match(line)
            if m:
                name = os.path.basename(m.group(1))[:-len(".out")]
                cur = tests[name] = {"plan_lines": 0, "semantic_lines": 0, "semantic_examples": []}
                exp, got = plan_lines(m.group(1)), plan_lines(m.group(2))
                continue
            m = HUNK_RE.match(line)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                continue
            if cur is None or line.startswith(("--- ", "+++ ")):
                continue
            if line.startswith("-"):
                is_plan, a = a in exp, a + 1
            elif line.startswith("+"):
                is_plan, b = b in got, b + 1
            else:
                a, b = a + 1, b + 1
                continue
            if is_plan:
                cur["plan_lines"] += 1
            else:
                cur["semantic_lines"] += 1
                if len(cur["semantic_examples"]) < 20:
                    cur["semantic_examples"].append(line)
    for name, t in tests.items():
        t["class"] = "semantic" if t["semantic_lines"] else "plan_only"
        print("%-22s %-10s plan lines %4d  other lines %d" % (
            name, t["class"], t["plan_lines"], t["semantic_lines"]))
        for ex in t["semantic_examples"]:
            print("      " + ex)
    with open(os.path.join(outdir, "classification.json"), "w") as f:
        json.dump({"plan_only": sorted(n for n, t in tests.items() if t["class"] == "plan_only"),
                   "semantic": sorted(n for n, t in tests.items() if t["class"] == "semantic"),
                   "tests": tests}, f, indent=2)
        f.write("\n")


if __name__ == "__main__":
    main()
