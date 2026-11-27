#!/usr/bin/env python3
"""Inventory tracked Python source and AST definitions for parity review.
Static inventory is not a declaration of behavioral equivalence.
No imports of application code, no database/network access.
"""
import argparse
import ast
import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--out", type=Path, required=True)
parser.add_argument("--all-python", action="store_true",
                    help="include scripts, migrations, tests and third-party snapshots")
parser.add_argument("--file-manifest", type=Path,
                    help="also write a compact per-file review checklist (no semantic signoff)")
args = parser.parse_args()
pattern = "*.py" if args.all_python else "src/videoroll/*.py"
paths = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z", pattern]).decode().split("\0")

def scope(path):
    if path.startswith("src/videoroll/"): return "first_party_business"
    if path.startswith(("scripts/", "docker/")): return "first_party_operations_or_model_bridge"
    if path.startswith("migrations/"): return "historical_schema"
    if path.startswith(("tests/", "rust/tests/", "rust/scripts/")): return "test_and_audit"
    if path.startswith(("services/", "vendor/")): return "third_party_snapshot"
    return "other"

errors = []
files = []
for rel in sorted(filter(None, paths)):
    raw = (ROOT / rel).read_bytes()
    lines = raw.splitlines()
    symbols = []
    try:
        tree = ast.parse(raw, filename=rel)
    except (SyntaxError, ValueError) as exc:
        errors.append({"path":rel,"line":getattr(exc,"lineno",None),"error":str(exc)})
        tree = ast.Module(body=[],type_ignores=[])
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        types = Counter(type(child).__name__ for child in ast.walk(node))
        symbols.append({"name": node.name, "type": type(node).__name__,
                        "first_line": min([node.lineno] + [d.lineno for d in node.decorator_list]),
                        "last_line": node.end_lineno,
                        "branches": types["If"] + types["Match"],
                        "exceptions": types["Try"] + types["ExceptHandler"],
                        "loops": types["For"] + types["AsyncFor"] + types["While"],
                        "asserts": types["Assert"],
                        "awaits": types["Await"]})
    # Every source line is represented by the file hash, physical line count,
    # and explicit definition spans, including module-level initialization.
    ranges = sorted(symbols, key=lambda item: (item["first_line"], item["last_line"]))
    files.append({"path": rel, "sha256": hashlib.sha256(raw).hexdigest(),
                  "lines": len(lines), "symbols": ranges, "scope":scope(rel),
                  "semantic_review": "not_implicitly_verified"})
summary = {"source_head": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
           "files": len(files), "lines": sum(f["lines"] for f in files),
           "definitions": sum(len(f["symbols"]) for f in files),
           "syntax_errors": len(errors), "parse_errors": errors,
           "scope": "all tracked Python" if args.all_python else "first-party src/videoroll Python only",
           "scope_counts":dict(Counter(f["scope"] for f in files)),
           "inventory": files}
args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
if args.file_manifest:
    import csv
    with args.file_manifest.open("w",encoding="utf-8",newline="") as out:
        writer=csv.writer(out)
        writer.writerow(["path","scope","lines","definitions","sha256","review_status"])
        bad={item["path"] for item in errors}
        for f in files:
            writer.writerow([f["path"],f["scope"],f["lines"],len(f["symbols"]),f["sha256"],
                             "parse_error_needs_review" if f["path"] in bad else "indexed_not_semantically_signed_off"])
print(json.dumps({k:v for k,v in summary.items() if k != "inventory"}, ensure_ascii=False))
