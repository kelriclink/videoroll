#!/usr/bin/env python3
"""Inventory original FastAPI route declarations and Rust/React candidates.

This is a source-level coverage audit, NOT a correctness certificate.
Matching verb/path never proves compatible auth, schemas or side effects.
"""
from __future__ import annotations
import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re

METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "websocket"}


def literal(node):
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError):
        return None


def prefix_for(node):
    for stmt in ast.walk(node):
        if not isinstance(stmt, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "router" for t in stmt.targets):
            continue
        val = stmt.value
        if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) and val.func.id == "APIRouter":
            for kw in val.keywords:
                if kw.arg == "prefix":
                    return literal(kw.value) or ""
    return ""


def route_catalog(project):
    base = project / "src/videoroll/apps"
    routes = []
    for source in sorted(base.rglob("*.py")):
        if "__pycache__" in source.parts or "tests" in source.parts:
            continue
        try:
            module = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        except (SyntaxError, UnicodeError):
            continue
        prefix = prefix_for(module)
        for node in ast.walk(module):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                if dec.func.attr not in METHODS:
                    continue
                path_node = literal(dec.args[0]) if dec.args else None
                path = prefix + path_node if isinstance(path_node, str) else None
                kw = {v.arg: literal(v.value) for v in dec.keywords}
                response = next((v.value for v in dec.keywords if v.arg == "response_model"), None)
                routes.append({
                    "service": source.relative_to(base).parts[0],
                    "file": source.relative_to(project).as_posix(),
                    "line": dec.lineno,
                    "method": dec.func.attr.upper(),
                    "path": path,
                    "handler": node.name,
                    "operation_id": kw.get("operation_id"),
                    "response_model": ast.unparse(response) if response else None,
                })
    return sorted(routes, key=lambda r: (r["service"], r["path"] or "~", r["method"], r["line"]))


def rust_catalog(project):
    records = []
    expr = re.compile(
        r'\.route\(\s*"(?P<path>/[^"\\]+)"\s*,\s*(?P<method>get|post|put|patch|delete|head|options)\s*\(',
        re.S,
    )
    for source in sorted((project / "rust/src").rglob("*.rs")):
        body = source.read_text(encoding="utf-8")
        for found in expr.finditer(body):
            records.append({
                "file": source.relative_to(project).as_posix(),
                "method": found.group("method").upper(),
                "path": found.group("path"),
                "line": body.count("\n", 0, found.start()) + 1,
            })
    return records


def normalize(path):
    return (re.sub(r"\{[^{}]+\}", "{}", path).rstrip("/") or "/")


def frontend_catalog(project):
    # Textual candidates only. Final contract signoff needs TypeScript AST review.
    base = project / "src/web/src"
    patterns = [
        re.compile(r'orchestratorUrl\(\s*([\x60"\'])(.*?)\1', re.S),
        re.compile(r'\x24\x7bORCHESTRATOR_URL\x7d(/[^\\x60\n]*)'),
    ]
    rows = []
    for source in sorted(base.rglob("*.ts*")):
        body = source.read_text(encoding="utf-8")
        for pattern in patterns:
            for hit in pattern.finditer(body):
                candidate = hit.group(2) if pattern is patterns[0] else hit.group(1)
                if not candidate.startswith("/"):
                    continue
                candidate = re.sub(r"\x24\x7b[^{}]+\}", "{}", candidate).split("?")[0]
                rows.append({
                    "file": source.relative_to(project).as_posix(),
                    "line": body.count("\n", 0, hit.start()) + 1,
                    "path_pattern": candidate,
                })
    return sorted(rows, key=lambda r: (r["file"], r["line"], r["path_pattern"]))


def render_doc(routes, rust, frontend):
    counts = Counter(r["service"] for r in routes)
    native = {(r["method"], normalize(r["path"])) for r in rust}
    overlap = sum(1 for r in routes if r["path"] and (r["method"], normalize(r["path"])) in native)
    unique_front = {x["path_pattern"] for x in frontend}
    lines = [
        "# Rust API / 原版前端迁移差异审计", "",
        "本报告由源码扫描生成。HTTP 路径相同不代表功能完成：请求/响应、鉴权、并发、外部副作用和前端实际行为必须逐项测试。", "",
        f"- 原版 Python FastAPI 装饰器：**{len(routes)}**（含 WebSocket、各独立服务和动态路由）。",
        f"- 原版 orchestrator：**{counts['orchestrator_api']}** 个（含 WebSocket）。",
        f"- Rust 静态 HTTP 路由候选：**{len(rust)}** 个（V2 不等于原版契约）。",
        f"- 无前缀方法+路径字面重合：**{overlap}** 个（仅重合，未经验收）。",
        f"- 前端旧 API 调用文本候选：**{len(frontend)}** 处，**{len(unique_front)}** 种 URL；可能遗漏动态拼接。", "",
        "## 原版子系统", "", "| 子系统 | 端点装饰器数量 |", "|---|---:|",
    ]
    lines.extend(f"| {name} | {count} |" for name, count in sorted(counts.items()))
    lines += [
        "", "## 上线前需要的逐项验收", "",
        "HTTP schema、状态码、权限与会话、外部平台幂等收据、重启恢复、资产引用、GC、真实前端调用、实际设备与真实平台测试。",
        "", "仅当原生 Rust 逻辑及其契约测试完成才可标记为已迁移。未支持端点应返回明确错误，不可伪造成功。",
        "", "精确源码清单：LEGACY_ROUTE_CATALOG.json、FRONTEND_API_REFERENCES.json；生成脚本 rust/scripts/audit_legacy_routes.py。", "",
    ]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[2]
    routes, rust, frontend = route_catalog(project), rust_catalog(project), frontend_catalog(project)
    outputs = {
        project / "rust/docs/LEGACY_ROUTE_CATALOG.json": json.dumps(
            {"source": "src/videoroll/apps/**/*.py", "note": "Declared routes, not verified parity", "routes": routes},
            indent=2, ensure_ascii=False) + "\n",
        project / "rust/docs/FRONTEND_API_REFERENCES.json": json.dumps(
            {"source": "src/web/src/**/*.ts*", "note": "Textual candidate extraction; not complete TypeScript AST", "references": frontend},
            indent=2, ensure_ascii=False) + "\n",
        project / "rust/docs/PARITY_AUDIT.md": render_doc(routes, rust, frontend),
    }
    if args.check:
        stale = [str(path) for path, value in outputs.items() if not path.exists() or path.read_text() != value]
        if stale:
            raise SystemExit(f"stale API audit: {stale}")
        print(f"OK {len(routes)} legacy routes; {len(rust)} Rust route declarations; {len(frontend)} frontend references")
    else:
        for path, value in outputs.items():
            path.write_text(value)
        print(f"Generated API audit: {len(routes)} legacy endpoints, {len(rust)} Rust routes, {len(frontend)} frontend references")


if __name__ == "__main__":
    main()
