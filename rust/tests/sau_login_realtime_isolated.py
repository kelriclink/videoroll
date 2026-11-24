#!/usr/bin/env python3
"""Original authenticated publishing-topic event over a real native WebSocket."""
import json
import os
import time
import uuid
from urllib.request import Request,urlopen
import psycopg
from websockets.sync.client import connect

db=os.environ["DATABASE_URL"]
assert db.endswith("/videoroll_test") and "127.0.0.1:" in db
base="http://127.0.0.1:18081"
def post(path,body,headers=None):
    return urlopen(Request(base+path,data=json.dumps(body).encode(),headers={"Content-Type":"application/json",**(headers or {})},method="POST"),timeout=10)
with post("/api/auth/setup",{"password":"isolated-ws-login-test-password"},{"x-videoroll-admin-bootstrap":os.environ["ADMIN_BOOTSTRAP_SECRET"]}) as res:
    assert json.load(res)["password_set"]
with post("/api/auth/login",{"password":"isolated-ws-login-test-password"}) as res:
    cookie=res.headers["Set-Cookie"].split(";",1)[0]
    assert json.load(res)["trusted"]

id=str(uuid.uuid4())
with psycopg.connect(db) as c:
    c.execute("""INSERT INTO operation_inbox(id,operation_key,runtime_engine,status,request_json,result_json)
      VALUES(%s,%s,'rust-sau-login','pending',%s,%s)""",(id,"sau-login:"+id,
      json.dumps({"platform":"douyin","account_name":"isolated-ws-fixture","browser_url":"/social-login/vnc.html?path=/social-login/websockify"}),
      json.dumps({"state":"starting","message":None})))

def until(ws,predicate):
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        msg=json.loads(ws.recv(timeout=max(.05,deadline-time.monotonic())))
        if predicate(msg):return msg
    raise AssertionError("WebSocket event timed out")

with connect("ws://127.0.0.1:18081/api/v1/ws/events",origin=base,additional_headers={"Cookie":cookie}) as ws:
    until(ws,lambda m:m["type"]=="ready")
    ws.send(json.dumps({"op":"set_subscriptions","topics":["publishing"]}))
    until(ws,lambda m:m["type"]=="subscribed")
    def matches(state):
        return lambda m:m.get("name")=="login_session.updated" and m.get("entity_id")==id and m["data"]["state"]==state
    start=until(ws,matches("starting"))
    assert start["topics"]==["publishing"] and start["data"]["id"]==id
    assert "request_json" not in start["data"] and "snapshot" not in start["data"]
    with psycopg.connect(db) as c:
        c.execute("""UPDATE operation_inbox SET status='completed',result_json='{"state":"canceled","message":"login canceled"}'::jsonb,
          completed_at=now(),updated_at=now() WHERE id=%s""",(id,))
    end=until(ws,matches("canceled"))
    assert end["data"]["finished_at"] and end["event_id"]!=start["event_id"]
with connect("ws://127.0.0.1:18081/api/v1/ws/events",origin=base,additional_headers={"Cookie":cookie}) as ws:
    until(ws,lambda m:m["type"]=="ready")
    ws.send(json.dumps({"op":"set_subscriptions","topics":["publishing"]}))
    until(ws,lambda m:m["type"]=="subscribed")
    assert until(ws,matches("canceled"))["data"]==end["data"]
print("PASS: authenticated real WebSocket emits original login_session.updated changes and reconnect snapshot")
