#!/usr/bin/env python3
"""Combine broad probes with separately executed route-specific evidence.

Do not rewrite broad HTTP results or promote route evidence to Python parity.
Run only after all listed smoke invocations completed successfully. Log markers
are required evidence, not a substitute for the caller checking exit status.
"""
import argparse, hashlib, json
from pathlib import Path
from collections import Counter

O='orchestrator_api'; E='egress_gateway'
# Exact original route, test mode, specific marker emitted after assertions,
# and limited scope. These are not new calls or inferred successes.
EXTRA={
 (O,'POST','/auth/setup'):('CONTRACT_217','PASS: original V1 admin initialization and setup replay rejected','fresh versioned bootstrap and replay protection'),
 (O,'POST','/maintenance/storage/cleanup-terminal'):('TERMINAL','PASS: task history remains, pending counts honest, second call idempotent and external symlink target unchanged','explicitly enabled isolated cleanup; terminal asset/history protection'),
 (O,'POST','/maintenance/workdir/cleanup'):('WORKDIR','PASS: original cross-process lock, safe deletion, retained active/recent entries, symlink avoidance and idempotency','explicitly enabled isolated cleanup; Python scanner differential'),
 (O,'POST','/render-management/executions/{execution_id}/requeue'):('RENDER_ADMIN','PASS: native queued cancel and expired requeue are atomic/idempotent','native expired execution and safety refusals'),
 (O,'PUT','/settings/dubbing/native'):('YOUDUB_CONTROL','PASS: model availability and original on-demand control JSON','local mock model and resident control; no real GPU'),
 (O,'POST','/settings/youtube/home_scan/run'):('HOME','PASS: real native YouTube Browse pagination and long-only stats','loopback Browse, filters and duplicate protection'),
 (O,'POST','/tasks/{task_id}/actions/publish'):('FULL','PASS: native SAU Douyin account validation, durable publish intent','Douyin branch, mock sidecar; Bilibili branch remains blocked in broad probe'),
 (O,'POST','/tasks/{task_id}/actions/publish_all'):('FULL','PASS: V1 multi-SAU publish_all exposes per-platform partial outcomes','multi-SAU mock partial outcomes; Bilibili mixed targets rejected'),
 (O,'POST','/tasks/{task_id}/publish_jobs/{publish_job_id}/append_p2'):('FULL','PASS: P1 native P2 append, upload failure safety','loopback Bilibili P2 edit, retry and dedupe; no live upload'),
 (E,'POST','/fetch'):('EGRESS','PASS: native GET returns Python-compatible base64 response and strictly bounded truncation','exact test-only loopback fixture; production SSRF refusal unchanged'),
}
RETIRED={(O,'GET','/remote/auto/youtube'),(O,'POST','/tasks/{task_id}/dubbing_jobs/{job_id}/confirm'),(O,'POST','/tasks/{task_id}/dubbing_jobs/{job_id}/rerun')}

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
    p=argparse.ArgumentParser();p.add_argument('directory',type=Path);args=p.parse_args();root=args.directory
    src=root/'contract-parity-217-standalone.json'; broad=json.loads(src.read_text())
    assert broad['summary']['failed']==0 and broad['summary']['probed']==217
    rows=broad['routes'];keys={(r['service'],r['method'],r['path']) for r in rows};assert len(rows)==len(keys)==217
    assert set(EXTRA)|RETIRED <= keys and not set(EXTRA)&RETIRED
    counts=Counter();logs={};result=[]
    for r in rows:
        k=(r['service'],r['method'],r['path']);e=None
        if k in RETIRED:
            assert r['probe']=='rejection' and r['http_status']==410,k
            state='retired_410'
        elif r['probe']=='positive':state='broad_positive'
        elif k in EXTRA:
            suite,marker,scope=EXTRA[k];f=root/(suite+'.log')
            if f.exists():
                text=f.read_text(errors='replace')
                if marker in text:
                    state='specialized_positive';e={'suite':suite,'log':f.name,'sha256':digest(f),'marker':marker,'scope':scope}
                    logs[f.name]=e['sha256']
                else:state='unresolved'
            else:state='unresolved'
        else:state='unresolved'
        counts[state]+=1
        result.append({**r,'coverage':state,'specialized_evidence':e})
    report={'broad_report':src.name,'broad_sha256':digest(src),'broad_summary':broad['summary'],'coverage':dict(counts),'full_python_parity_verified':0,'logs':logs,'routes':result}
    (root/'contract-closeout-217.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(report['coverage'],ensure_ascii=False))
    for r in result:
        if r['coverage']=='unresolved':print('UNRESOLVED',r['service'],r['method'],r['path'])
    if counts['unresolved']:raise SystemExit(1)

if __name__=='__main__':main()
