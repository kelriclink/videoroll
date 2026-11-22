#!/usr/bin/env python3
"""Python to native Rust V1 route audit. Structural, NOT full business parity."""
import argparse
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ROUTES = ROOT / "rust/docs/LEGACY_ROUTE_CATALOG.json"
FRONT = ROOT / "rust/docs/FRONTEND_API_REFERENCES.json"
PARITY = ROOT / "rust/src/parity.rs"
OUTPUT = ROOT / "rust/docs/CONTRACT_MATRIX.json"
REPORT = ROOT / "rust/docs/CONTRACT_MATRIX.md"
ADAPTER_RE = re.compile(
    r'\(\s*"(?P<method>GET|POST|PUT|DELETE|PATCH|HEAD|OPTIONS)"\s*,\s*"(?P<path>/[^"]+)"\s*,?\s*\)'
)

def collect():
    routes = json.loads(ROUTES.read_text())["routes"]
    refs = json.loads(FRONT.read_text())["references"]
    body = PARITY.read_text().split("const V1_ADAPTERS:", 1)[1].split("];", 1)[0]
    adapters = {(m.group("method"), m.group("path")) for m in ADAPTER_RE.finditer(body)}
    original = {(r["method"], r["path"]) for r in routes
                if r["service"] == "orchestrator_api"
                or (r["service"]=="youtube_ingest" and r["path"]=="/youtube/ingest")}
    if adapters - original:
        raise ValueError("V1 adapter not in original routes: " + repr(sorted(adapters - original)))
    rows = []
    for source in routes:
        adapted = ((source["service"] == "orchestrator_api"
                    or (source["service"]=="youtube_ingest" and
                        source["path"]=="/youtube/ingest")) and
                   (source["method"], source["path"]) in adapters)
        rows.append({
            "service":source["service"],"method":source["method"],"path":source["path"],
            "python_source":source["file"]+":"+str(source["line"]),
            "python_handler":source["handler"],
            "v1_status":"native_adapter_partial" if adapted else "not_native_v1",
            "full_contract_verified":False,
            "auth_verified":False,
            "side_effects_verified":False,
            "restart_verified":False,
            "frontend_verified":False,
            "real_platform_verified":False,
        })
    assert sum(r["v1_status"]=="native_adapter_partial" for r in rows) == len(adapters)
    return {
        "status":"INCOMPLETE",
        "notice":"Route registration is not contract parity; V2-only routes are counted separately.",
        "legacy_declarations":len(routes),
        "native_v1_partial":len(adapters),
        "fully_verified":0,
        "frontend_reference_text_occurrences":len(refs),
        "checks_needed":[
            "HTTP schema/status codes","auth/session","lease, idempotency and receipts",
            "storage references and GC","restart and failure injection",
            "real frontend interaction","real platform or GPU validation"
        ],
        "routes":rows,
    }

def markdown(data):
    counts=Counter((x["service"],x["v1_status"]) for x in data["routes"])
    services=sorted(set(x["service"] for x in data["routes"]))
    lines=[
        "# Rust 2.0 Python 路由逐项验收矩阵",
        "",
        "本清单只由原版 Python 路由装饰器和 Rust V1 适配器登记表生成；"
        "标记 native_adapter_partial 不代表业务完整。",
        "缺少 V1 对应的接口可能在 V2 中部分实现，但不能直接供旧版前端调用。",
        "全部完成状态只允许结合 API 契约、权限、并发、重启、外部副作用与设备测试证据填写。",
        "",
        "- 原版声明："+str(data["legacy_declarations"]),
        "- 原生 V1 部分适配："+str(data["native_v1_partial"]),
        "- 无原生 V1 入口："+str(data["legacy_declarations"]-data["native_v1_partial"]),
        "- 已完成全面验收：0",
        "- 前端引用文本："+str(data["frontend_reference_text_occurrences"]),
        "",
        "| Python 子系统 | 原始声明 | Rust V1 部分适配 | 未映射 V1 |",
        "|---|---:|---:|---:|",
    ]
    for name in services:
        total=sum(n for (srv,_),n in counts.items() if srv==name)
        ready=counts[(name,"native_adapter_partial")]
        lines.append(f"| {name} | {total} | {ready} | {total-ready} |")
    lines.extend([
        "", "## 完整逐项清单", "",
        "| 子系统 | 方法 | 路径 | 原生 V1 状态 | Python 原方法 |",
        "|---|---|---|---|---|",
    ])
    for row in data["routes"]:
        lines.append(f"| {row['service']} | {row['method']} | {row['path']} | "
                     f"{row['v1_status']} | {row['python_handler']} |")
    lines.extend(["","此矩阵是待核验清单，不是实现完成率。",""])
    return "\n".join(lines)

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--check",action="store_true")
    args=p.parse_args()
    data=collect()
    files={
        OUTPUT:json.dumps(data,ensure_ascii=False,indent=2)+"\n",
        REPORT:markdown(data),
    }
    if args.check:
        stale=[str(path) for path,value in files.items()
               if not path.exists() or path.read_text()!=value]
        if stale:raise SystemExit("stale contract matrix: "+", ".join(stale))
        print(f"OK {data['legacy_declarations']} original routes, {data['native_v1_partial']} partial native V1, zero full parity signoffs")
    else:
        for path,value in files.items():path.write_text(value)
        print(f"Generated {data['legacy_declarations']} route checks")
if __name__=="__main__":
    main()
