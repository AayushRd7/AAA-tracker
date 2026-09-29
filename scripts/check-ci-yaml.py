#!/usr/bin/env python3
"""Validate the GitHub Actions workflow files locally.

A YAML typo in a workflow file does not fail a job — no job ever starts. GitHub
reports "this run likely failed because of a workflow file issue" and every push
goes green-looking while nothing runs. That is exactly what an unquoted `echo
"ok: ..."` inside a `run:` scalar did here, unnoticed for a whole session, so the
workflow files get their own parser pass.

Exits 1 on invalid YAML. Without PyYAML it reports that it skipped rather than
pretending the files are fine.
"""
import glob
import sys

FILES = sorted(glob.glob(".github/workflows/*.yml"))
if not FILES:
    print("ci: no workflow files found")
    sys.exit(0)

try:
    import yaml
except ImportError:
    print(f"ci: skipped — PyYAML is not installed ({len(FILES)} file(s) unchecked)")
    print("ci: run 'pip install pyyaml' (or 'make check-ci' in CI) to validate them")
    sys.exit(0)

bad = []
for path in FILES:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
    except Exception as e:  # noqa: BLE001 - the message is the whole point
        bad.append(f"{path}: {e}")
        continue
    if not isinstance(doc, dict) or not doc.get("jobs"):
        bad.append(f"{path}: no jobs defined")

if bad:
    print("ci: invalid workflow file(s):")
    for line in bad:
        print(f"  {line}")
    sys.exit(1)

print(f"ci: {len(FILES)} workflow file(s) parse")
