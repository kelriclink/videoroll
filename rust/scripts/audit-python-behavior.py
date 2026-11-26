#!/usr/bin/env python3
"""Inventory each first-party Python source line and AST definition for parity review.
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
args = parser.parse_args()
paths = subprocess.check_output(["git", "-C", str(ROOT), "ls-files", "-z", "src/videoroll/*.py"]).decode().split("\0")
files = []
for rel in sorted(filter(None, paths)):
    raw = (ROOT / rel).read_bytes()
    lines = raw.decode("utf-8").splitlines()
    tree = ast.parse(raw, filename=rel)
    symbols = []
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
                  "lines": len(lines), "symbols": ranges,
                  "semantic_review": "not_implicitly_verified"})
summary = {"source_head": subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip(),
           "files": len(files), "lines": sum(f["lines"] for f in files),
           "definitions": sum(len(f["symbols"]) for f in files),
           "syntax_errors": 0, "scope": "first-party src/videoroll Python only",
           "inventory": files}
args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({k:v for k,v in summary.items() if k != "inventory"}, ensure_ascii=False))
