"use strict";
// A real WebSocket handshake, cookie auth, subscription, and DB-to-browser event.
const WebSocket = require("ws");
const fs = require("fs");
const cp = require("child_process");
const assert = require("assert").strict;
const jar = fs.readFileSync(process.env.VR2_COOKIE_JAR, "utf8");
const cookieRow = jar.split("\n").find((line)=>line.includes("\tvideoroll_admin_device\t"));
if (!cookieRow) throw new Error("browser admin cookie was not issued");
const cols = cookieRow.split("\t");
const cookie = `${cols[5]}=${cols[6]}`;
const origin = "http://127.0.0.1:18080";
const url = "ws://127.0.0.1:18080/api/v1/ws/events";
const sleep=(ms)=>new Promise((res)=>setTimeout(res,ms));
function connect(headers) {
    return new Promise((resolve,reject)=>{
        const ws = new WebSocket(url,{headers});
        ws.once("open",()=>resolve(ws));
        ws.once("error",reject);
    });
}
function message(ws,predicate,timeout=6500) {
    return new Promise((resolve,reject)=>{
        const timer=setTimeout(()=>{ws.removeListener("message",callback);reject(new Error("websocket message timeout"));},timeout);
        function callback(raw){
            const obj=JSON.parse(raw.toString());
            if(!predicate(obj))return;
            clearTimeout(timer);
            ws.removeListener("message",callback);
            resolve(obj);
        }
        ws.on("message",callback);
    });
}
async function run() {
    let denied = await connect({Origin:origin});
    const closed = await new Promise((resolve,reject)=>{
        denied.once("close",(code)=>resolve(code));
        setTimeout(()=>reject(new Error("anonymous WS not closed")),5500);
    });
    assert.equal(closed,4401);
    let cross = await connect({Origin:"http://evil.example",Cookie:cookie});
    const crossCode = await new Promise((resolve,reject)=>{
        cross.once("close",(code)=>resolve(code));
        setTimeout(()=>reject(new Error("cross-site WS not closed")),5500);
    });
    assert.equal(crossCode,4403);
    const ws = new WebSocket(url,{headers:{Origin:origin,Cookie:cookie}});
    const ready=message(ws,(m)=>m.type==="ready");
    await new Promise((resolve,reject)=>{ws.once("open",resolve);ws.once("error",reject)});
    const received=await ready;
    assert.equal(received.heartbeat_seconds,20);
    const subscribed=message(ws,(m)=>m.type==="subscribed");
    ws.send(JSON.stringify({op:"set_subscriptions",topics:["tasks","resources"]}));
    assert.deepEqual((await subscribed).topics,["resources","tasks"]);
    const error=message(ws,(m)=>m.type==="error");
    ws.send(JSON.stringify({op:"set_subscriptions",topics:["task:not-a-uuid"]}));
    assert.match((await error).detail,/invalid/);
    const resources=await message(ws,(m)=>m.name==="system.resources.sample");
    assert.ok(resources.data.memory.total_bytes>0);
    const titleSql = "INSERT INTO vr2_task_titles(task_id,source_title) VALUES('"+process.env.VR2_TEST_TASK+"','WS Title Fixture') ON CONFLICT(task_id) DO UPDATE SET source_title='WS Title Fixture'";
    cp.execFileSync("docker",["exec",process.env.VR2_SMOKE_CONTAINER,"psql","-v","ON_ERROR_STOP=1","-U","postgres","-d","videoroll_test","-c",titleSql],{stdio:"ignore"});
    const update=message(ws,(m)=>m.name==="task.updated");
    cp.execFileSync("docker",["exec",process.env.VR2_SMOKE_CONTAINER,"psql","-v","ON_ERROR_STOP=1","-U","postgres","-d","videoroll_test",
        "-c",`INSERT INTO vr2_events(id,workflow_id,kind,details) VALUES(gen_random_uuid(),'${process.env.VR2_TEST_WORKFLOW}','smoke.live', '{}'::jsonb)`],{stdio:"ignore"});
    const msg=await update;
    assert.equal(msg.data.display_title,"WS Title Fixture");
    const restoreTitleSql = "DELETE FROM vr2_task_titles WHERE task_id='"+process.env.VR2_TEST_TASK+"'";
    cp.execFileSync("docker",["exec",process.env.VR2_SMOKE_CONTAINER,"psql","-v","ON_ERROR_STOP=1","-U","postgres","-d","videoroll_test","-c",restoreTitleSql],{stdio:"ignore"});
    assert.equal(msg.data.id,process.env.VR2_TEST_TASK);
    assert.equal(msg.entity_id,process.env.VR2_TEST_TASK);
    assert.ok(msg.topics.includes("queue"));
    ws.close();
    console.log("PASS: authenticated native WebSocket, origin denial, subscriptions, resources and real DB-backed task.updated");
}
run().catch((err)=>{console.error(err);process.exitCode=1;});
