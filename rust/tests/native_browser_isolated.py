"""Opt-in real Chromium exercise of built React against isolated Rust + Nginx."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
from urllib.request import urlopen
from websockets.sync.client import connect

def browser(root,port,task_id,bootstrap):
    origin=f"http://127.0.0.1:{port}"
    chromium=shutil.which("chromium") or shutil.which("chromium-browser")
    assert chromium, "Chromium required for opt-in browser test"
    profile=root/"chromium"
    profile.mkdir()
    with (root/"chromium.log").open("w") as log:
        p=subprocess.Popen([chromium,"--headless","--no-sandbox","--disable-dev-shm-usage",
            "--disable-background-networking","--disable-component-update","--no-first-run",
            "--no-default-browser-check","--remote-debugging-address=127.0.0.1","--remote-debugging-port=0",
            f"--user-data-dir={profile}","--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1",
            "--window-size=1440,1000","about:blank"],stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
        try:
            for _ in range(100):
                if (profile/"DevToolsActivePort").is_file():break
                assert p.poll() is None
                time.sleep(.1)
            debug=int((profile/"DevToolsActivePort").read_text().splitlines()[0])
            pages=json.load(urlopen(f"http://127.0.0.1:{debug}/json/list",timeout=5))
            ws=next(item["webSocketDebuggerUrl"] for item in pages if item["type"]=="page")
            with connect(ws,open_timeout=10,max_size=8*1024*1024) as c:
                seq=0
                exceptions=[]
                errors=[]
                def cmd(method,params=None):
                    nonlocal seq
                    seq+=1
                    i=seq
                    c.send(json.dumps({"id":i,"method":method,"params":params or {}}))
                    while True:
                        msg=json.loads(c.recv(timeout=15))
                        if msg.get("id")==i:
                            assert "error" not in msg,(method,msg)
                            return msg.get("result",{})
                        if msg.get("method")=="Runtime.exceptionThrown":
                            exceptions.append(msg["params"]["exceptionDetails"].get("text"))
                        if msg.get("method")=="Network.responseReceived":
                            response=msg["params"]["response"]
                            if response["status"]>=400 and response["url"].startswith(origin+"/api/v1/"):
                                errors.append((response["status"],response["url"].split("?")[0].removeprefix(origin)))
                def ev(expression):
                    result=cmd("Runtime.evaluate",{"expression":expression,"returnByValue":True,"awaitPromise":True})
                    assert "exceptionDetails" not in result,(expression[:80],result.get("exceptionDetails"))
                    return result.get("result",{}).get("value")
                def wait(expression,timeout=15):
                    end=time.monotonic()+timeout
                    while time.monotonic()<end:
                        result=ev(expression)
                        if result:return result
                        time.sleep(.2)
                    raise AssertionError(("browser timeout",expression[:150],ev("document.body?.innerText.slice(0,1300)")))
                def fill(index,value):
                    ev(f"""(()=>{{const x=document.querySelectorAll('input[type=password]')[{index}];Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(x,{json.dumps(value)});x.dispatchEvent(new Event('input',{{bubbles:true}}));}})()""")
                def click_text(text):
                    return ev(f"""(()=>{{const b=[...document.querySelectorAll('button')].find(x=>x.textContent.trim()==={json.dumps(text)}&&!x.disabled);if(!b)return false;b.click();return true;}})()""")
                def navigate(path):
                    cmd("Page.navigate",{"url":origin+path})
                    wait(f"location.pathname==={json.dumps(path)} && document.readyState!=='loading'")
                cmd("Page.enable")
                cmd("Runtime.enable")
                cmd("Network.enable")
                navigate("/")
                wait("document.body.innerText.includes('Set admin password')")
                password="isolated-browser-password-123"
                fill(0,password);fill(1,password);fill(2,bootstrap)
                assert click_text("Set password")
                wait("document.body.innerText.includes('Admin login')")
                fill(0,password)
                assert click_text("Login")
                wait("!!document.querySelector('main') && document.querySelector('main').innerText.includes('工作台')")
                assert ev("!document.querySelector('nav a[href=\"/rust-v2\"]')")
                print("PASS: Chromium real React admin setup, login cookie and original dashboard",flush=True)
                paths=["/tasks","/tasks/new",f"/tasks/{task_id}","/videos","/youtube/sources",
                    "/queue/render","/render","/knowledge","/dictionaries","/operations","/workflow",
                    "/settings/auto","/settings/dubbing","/settings/youtube","/settings/publish",
                    "/settings/asr","/settings/translate","/settings/review","/settings/storage","/settings/api"]
                # Read-only navigation; actual platform/discovery page and embedded
                # playout are covered by separate isolated suites, not live calls.
                for path in paths:
                    navigate(path)
                    heading=wait("""(()=>{const m=document.querySelector('main');if(!m||m.innerText.includes('页面不存在')||m.innerText.includes('页面加载中'))return '';return [...m.querySelectorAll('h1,h2')].map(x=>x.textContent.trim()).filter(Boolean).join(' / ')||m.innerText.trim().slice(0,80);})()""")
                    if path=="/workflow":
                        wait("document.querySelector('main')?.innerText.includes('Rust 原生调度')")
                        assert ev("!document.querySelector('iframe[title=\"Hatchet 工作流控制台\"]')")
                    print(f"PASS: Chromium route {path}: {heading[:90]}",flush=True)
                navigate("/settings/auto")
                wait("document.querySelector('main')?.innerText.includes('输出格式')")
                wait("[...document.querySelectorAll('label')].find(l=>l.textContent.trim()==='SRT')?.querySelector('input')?.checked===true")
                # Exercise the React state update and actual PUT, not direct API writes.
                assert ev("""(()=>{const x=[...document.querySelectorAll('label')].find(l=>l.textContent.trim()==='ASS')?.querySelector('input');if(!x)return false;if(x.checked)x.click();return true;})()""")
                wait("[...document.querySelectorAll('button')].some(b=>b.textContent.trim()==='保存配置'&&!b.disabled)")
                assert click_text("保存配置")
                wait("""fetch('/api/v1/subtitle/auto/profile').then(r=>r.json()).then(x=>Array.isArray(x.formats)&&x.formats.length===1&&x.formats[0]==='srt')""")
                navigate("/settings/auto")
                wait("[...document.querySelectorAll('label')].find(l=>l.textContent.trim()==='ASS')?.querySelector('input')?.checked===false")
                print("PASS: Chromium SRT-only selection saved through React and survived reload",flush=True)
                assert not exceptions, exceptions
                # Preserve diagnostics instead of claiming all interactions work.
                print("Browser HTTP error diagnostics:",json.dumps(sorted(set(errors)),ensure_ascii=False),flush=True)
        finally:
            if p.poll() is None:
                os.killpg(p.pid,signal.SIGTERM)
                try:p.wait(timeout=8)
                except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait(timeout=5)
            shutil.rmtree(profile,ignore_errors=True)
