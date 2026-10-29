"""
End-to-end: the real server process on 127.0.0.1:18443, real MCP requests.

Two client styles:
  - the MCP SDK client (ClientSession over streamable HTTP) for discovery and
    tool calls, exactly what a Swarn client will use;
  - raw HTTP JSON-RPC for cases the SDK client hides (401 status codes,
    replaying another user's session id, Host header, oversized body).
Synthetic data only. Workspace, token file and logs live in a pytest temp dir.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bruin_mcp_service import admin
from bruin_mcp_service.tools import TOOL_NAMES
from conftest import PROJECT, needs_bruin
from synthetic import MESSY_EXPECTED, MESSY_ORDERS_CSV, b64, large_csv

pytestmark = needs_bruin

HOST, PORT = "127.0.0.1", int(os.environ.get("BMCP_E2E_PORT", "18443"))
BASE = f"http://{HOST}:{PORT}"
URL = BASE + "/mcp"
ACCEPT = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def _port_free() -> bool:
    with socket.socket() as s:
        return s.connect_ex((HOST, PORT)) != 0


@pytest.fixture(scope="module")
def server(tmp_path_factory, request):
    if not _port_free():
        pytest.fail(f"{HOST}:{PORT} is already in use; stop the dev server first")
    root = tmp_path_factory.mktemp("e2e")
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(root),
        "BMCP_ENV": "dev", "BMCP_BIND_HOST": HOST, "BMCP_PORT": str(PORT),
        "BMCP_PUBLIC_URL": BASE,
        "BMCP_WORKSPACE_ROOT": str(root / "ws"), "BMCP_LOG_DIR": str(root / "logs"),
        "BMCP_TMP_DIR": str(root / "tmp"), "BMCP_TOKEN_FILE": str(root / "auth" / "tokens.json"),
        "BRUIN_BIN": str(PROJECT / "vendor/bruin/v0.11.780/bruin"),
        "BRUIN_VERIFIED_FILE": str(PROJECT / "vendor/bruin/v0.11.780/VERIFIED.txt"),
        "BRUIN_EXPECTED_VERSION": "v0.11.780",
        "BRUIN_HOME": str(PROJECT / "var/bruin-home"),
        "BMCP_MAX_UPLOAD_MB": "5",
        "BMCP_USER_CONCURRENCY": "1",
    }
    token_file = Path(env["BMCP_TOKEN_FILE"])
    os.environ["BMCP_TOKEN_FILE"] = str(token_file)  # for admin.main
    admin.main(["init"])
    tokens = {
        "alice": admin.issue(token_file, "alice", TOOL_NAMES, 90),
        "bob": admin.issue(token_file, "bob", TOOL_NAMES, 90),
        "carol_health_only": admin.issue(token_file, "carol", frozenset({"health"}), 90),
        "expired": admin.issue(token_file, "alice", TOOL_NAMES, 1, now=time.time() - 2 * 86400),
    }
    proc = subprocess.Popen([sys.executable, "-I", "-m", "bruin_mcp_service.server"],
                            cwd=PROJECT / "src", env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.time() + 30
    while _port_free():
        if proc.poll() is not None or time.time() > deadline:
            proc.kill()
            out, err = proc.communicate()
            pytest.fail(f"server did not start: {err[-2000:]}")
        time.sleep(0.2)
    yield {"tokens": tokens, "token_file": token_file, "root": root, "proc": proc}
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    out, err = proc.communicate()
    request.config._bmcp_server_stdout = out


@asynccontextmanager
async def mcp_session(token: str):
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=120) as http:
        async with streamable_http_client(URL, http_client=http) as (read, write, _get_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


def run(coro):
    return asyncio.run(coro)


def _rpc(method, params=None, id_=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if id_ is not None:
        msg["id"] = id_
    if params is not None:
        msg["params"] = params
    return msg


def _parse(resp: httpx.Response):
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:])
        return None
    return resp.json() if resp.content else None


def raw_open_session(token: str) -> tuple[httpx.Client, str]:
    client = httpx.Client(base_url=BASE, timeout=60, headers={**ACCEPT, "Authorization": f"Bearer {token}"})
    init = client.post("/mcp", json=_rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "e2e-raw", "version": "0"}}))
    assert init.status_code == 200, init.text
    sid = init.headers["mcp-session-id"]
    client.headers["mcp-session-id"] = sid
    assert client.post("/mcp", json=_rpc("notifications/initialized", id_=None)).status_code == 202
    return client, sid


def raw_call(client: httpx.Client, tool: str, args: dict, id_=2) -> httpx.Response:
    return client.post("/mcp", json=_rpc("tools/call", {"name": tool, "arguments": args}, id_))


# ---------------------------------------------------------------- discovery

def test_tool_discovery(server):
    async def go():
        async with mcp_session(server["tokens"]["alice"]) as s:
            return (await s.list_tools()).tools
    tools = run(go())
    assert {t.name for t in tools} == TOOL_NAMES
    for t in tools:
        props = set(t.inputSchema.get("properties", {}))
        assert not props & {"caller", "user_id", "ctx"}, (t.name, props)


# ------------------------------------------------------------ authentication

@pytest.mark.parametrize("header", [None, "Bearer", "Bearer nope", "Basic abc",
                                    "Bearer bmcp_000000000000_" + "A" * 43])
def test_missing_or_invalid_token_gets_401(server, header):
    headers = dict(ACCEPT)
    if header is not None:
        headers["Authorization"] = header
    r = httpx.post(URL, headers=headers, json=_rpc("initialize", {}))
    assert r.status_code == 401
    assert "invalid_token" in r.headers.get("www-authenticate", "")


def test_wrong_secret_for_real_token_id_gets_401(server):
    good = server["tokens"]["alice"]
    forged = good[:-2] + ("AA" if not good.endswith("AA") else "BB")
    r = httpx.post(URL, headers={**ACCEPT, "Authorization": f"Bearer {forged}"}, json=_rpc("initialize", {}))
    assert r.status_code == 401


def test_expired_token_gets_401(server):
    r = httpx.post(URL, headers={**ACCEPT, "Authorization": f"Bearer {server['tokens']['expired']}"},
                   json=_rpc("initialize", {}))
    assert r.status_code == 401


def test_revocation_mid_session(server):
    token = admin.issue(server["token_file"], "dave", TOOL_NAMES, 90)
    client, _ = raw_open_session(token)
    ok = _parse(raw_call(client, "health", {}))
    assert ok["result"]["structuredContent"]["user_id"] == "dave"
    admin.revoke(server["token_file"], token.split("_")[1])
    after = raw_call(client, "health", {}, id_=3)
    assert after.status_code == 401
    r = httpx.post(URL, headers={**ACCEPT, "Authorization": f"Bearer {token}"}, json=_rpc("initialize", {}))
    assert r.status_code == 401


# ------------------------------------------------------ identity and tools

def test_caller_identity_comes_from_token(server):
    async def whoami(token):
        async with mcp_session(token) as s:
            return (await s.call_tool("health", {})).structuredContent
    a = run(whoami(server["tokens"]["alice"]))
    b = run(whoami(server["tokens"]["bob"]))
    assert a["user_id"] == "alice" and b["user_id"] == "bob"
    assert a["token_id"] != b["token_id"]
    assert a["bruin"] == {"version": "v0.11.780", "pin_ok": True} and a["status"] == "ok"


def test_identity_arguments_are_ignored(server):
    async def go():
        async with mcp_session(server["tokens"]["bob"]) as s:
            return await s.call_tool("list_datasets", {"user_id": "alice"})
    res = run(go())
    # either refused as an unknown argument or ignored; never alice's data
    if not res.isError:
        assert all(d.get("filename") != "synthetic_orders.csv" for d in res.structuredContent["datasets"])


def test_per_tool_authorization(server):
    async def go():
        async with mcp_session(server["tokens"]["carol_health_only"]) as s:
            ok = await s.call_tool("health", {})
            denied = await s.call_tool("list_datasets", {})
            denied_upload = await s.call_tool("upload_dataset", {"filename": "a.csv", "content_base64": b64("x\n1\n")})
            return ok, denied, denied_upload
    ok, denied, denied_upload = run(go())
    assert not ok.isError and ok.structuredContent["allowed_tools"] == ["health"]
    for res in (denied, denied_upload):
        assert res.isError and "not authorized" in res.content[0].text
    assert not (server["root"] / "ws" / "users" / "carol" / "datasets").exists() or \
        list((server["root"] / "ws" / "users" / "carol" / "datasets").iterdir()) == []


# ------------------------------------------------- data and cross-user isolation

def test_upload_and_inspect_messy_dataset_and_isolation(server):
    async def alice_flow():
        async with mcp_session(server["tokens"]["alice"]) as s:
            up = (await s.call_tool("upload_dataset", {
                "filename": "synthetic_orders.csv", "content_base64": b64(MESSY_ORDERS_CSV)})).structuredContent
            info = (await s.call_tool("inspect_dataset", {"dataset_id": up["dataset_id"], "sample_rows": 2})).structuredContent
            listed = (await s.call_tool("list_datasets", {})).structuredContent
            return up, info, listed

    up, info, listed = run(alice_flow())
    ds_id = up["dataset_id"]
    assert info["row_count"] == MESSY_EXPECTED["row_count"]
    assert info["duplicate_rows"] == MESSY_EXPECTED["duplicate_rows"]
    assert {c["name"]: c["null_count"] for c in info["columns"]} == MESSY_EXPECTED["nulls"]
    assert [d["dataset_id"] for d in listed["datasets"]] == [ds_id]
    assert "path" not in json.dumps(up) and str(server["root"]) not in json.dumps(info)

    async def bob_flow():
        async with mcp_session(server["tokens"]["bob"]) as s:
            listed = (await s.call_tool("list_datasets", {})).structuredContent
            inspect = await s.call_tool("inspect_dataset", {"dataset_id": ds_id})
            delete = await s.call_tool("delete_dataset", {"dataset_id": ds_id})
            traversal = await s.call_tool("inspect_dataset", {"dataset_id": f"../alice/datasets/{ds_id}"})
            return listed, inspect, delete, traversal

    listed_b, inspect_b, delete_b, trav_b = run(bob_flow())
    assert listed_b["datasets"] == []
    for res in (inspect_b, delete_b, trav_b):
        assert res.isError and "no dataset" in res.content[0].text

    async def alice_still_has_it():
        async with mcp_session(server["tokens"]["alice"]) as s:
            return (await s.call_tool("list_datasets", {})).structuredContent
    assert [d["dataset_id"] for d in run(alice_still_has_it())["datasets"]] == [ds_id]


def test_session_cannot_be_used_with_another_token(server):
    alice_client, alice_sid = raw_open_session(server["tokens"]["alice"])
    with httpx.Client(base_url=BASE, timeout=30, headers={
            **ACCEPT, "Authorization": f"Bearer {server['tokens']['bob']}", "mcp-session-id": alice_sid}) as bob:
        r = raw_call(bob, "list_datasets", {})
    assert r.status_code == 404
    assert _parse(raw_call(alice_client, "health", {}, id_=5))["result"]["structuredContent"]["user_id"] == "alice"


def test_delete_own_dataset(server):
    async def go():
        async with mcp_session(server["tokens"]["bob"]) as s:
            up = (await s.call_tool("upload_dataset", {"filename": "synthetic_tmp.csv",
                                                       "content_base64": b64("a\n1\n")})).structuredContent
            gone = (await s.call_tool("delete_dataset", {"dataset_id": up["dataset_id"]})).structuredContent
            listed = (await s.call_tool("list_datasets", {})).structuredContent
            return up, gone, listed
    up, gone, listed = run(go())
    assert gone == {"deleted": up["dataset_id"]} and listed["datasets"] == []


# ------------------------------------------------------- transport and limits

def test_wrong_host_header_refused(server):
    r = httpx.post(URL, headers={**ACCEPT, "Authorization": f"Bearer {server['tokens']['alice']}",
                                 "Host": "evil.example:18443"}, json=_rpc("initialize", {}))
    assert r.status_code == 421


def test_foreign_origin_refused(server):
    r = httpx.post(URL, headers={**ACCEPT, "Authorization": f"Bearer {server['tokens']['alice']}",
                                 "Origin": "http://evil.example"}, json=_rpc("initialize", {}))
    assert r.status_code == 403


def test_oversized_body_refused(server):
    # server runs with BMCP_MAX_UPLOAD_MB=5 -> body limit ~7.7 MB
    client, _ = raw_open_session(server["tokens"]["alice"])
    r = raw_call(client, "upload_dataset", {"filename": "big.csv", "content_base64": "A" * (9 * 1024 * 1024)})
    assert r.status_code == 413


def test_upload_over_limit_refused_by_tool(server):
    async def go():
        async with mcp_session(server["tokens"]["alice"]) as s:
            return await s.call_tool("upload_dataset", {"filename": "big.csv",
                                                        "content_base64": b64("x\n" + "1\n" * 3_000_000)})
    res = run(go())
    assert res.isError and "5 MB" in res.content[0].text


def test_per_user_concurrency_limit(server):
    """BMCP_USER_CONCURRENCY=1 here: a second simultaneous call from the same user is refused."""
    async def go():
        async with mcp_session(server["tokens"]["bob"]) as s:
            up = (await s.call_tool("upload_dataset", {"filename": "synthetic_large.csv",
                                                       "content_base64": b64(large_csv(90_000))})).structuredContent
            results = await asyncio.gather(*[
                s.call_tool("inspect_dataset", {"dataset_id": up["dataset_id"], "sample_rows": 0})
                for _ in range(3)])
            other_user = None
            async with mcp_session(server["tokens"]["alice"]) as a:
                other_user = await a.call_tool("health", {})
            return results, other_user
    results, other = run(go())
    busy = [r for r in results if r.isError and "busy" in r.content[0].text]
    ok = [r for r in results if not r.isError]
    assert len(ok) >= 1 and len(busy) >= 1, [r.content[0].text[:80] for r in results]
    assert not other.isError


# ------------------------------------------------------------------- audit log

def test_audit_log_has_no_secrets(server):
    log = (server["root"] / "logs" / "service.log").read_text()
    for token in server["tokens"].values():
        assert token not in log and token.split("_", 2)[2] not in log
    entries = [json.loads(line) for line in log.splitlines()]
    events = {(e.get("event"), e.get("outcome") or e.get("reason")) for e in entries}
    assert ("tool_call", "ok") in events
    assert ("tool_call", "denied") in events
    assert ("auth_rejected", "expired") in events
    assert ("auth_rejected", "revoked") in events
    denied = [e for e in entries if e.get("outcome") == "denied"]
    assert denied and all(e["user_id"] == "carol" and len(e["token_id"]) == 12 for e in denied)


# ------------------------------------------------------- plan engine (Phase 5)

def _plan_for(dataset_id, extra_step=None):
    steps = [{"op": "trim", "columns": ["customer", "country"]},
             {"op": "cast", "columns": {"order_id": "BIGINT"}},
             {"op": "dedupe", "key": ["order_id"], "order_by": ["order_id"]}]
    if extra_step:
        steps.append(extra_step)
    return {"name": "e2e_orders", "sources": [{"table": "orders", "dataset_id": dataset_id}],
            "clean": [{"table": "orders", "steps": steps,
                       "checks": [{"column": "order_id", "check": "unique"}]}]}


def test_plan_tools_over_mcp_validate_compile_and_isolation(server):
    async def alice_flow():
        async with mcp_session(server["tokens"]["alice"]) as s:
            up = (await s.call_tool("upload_dataset", {"filename": "synthetic_orders.csv",
                                                       "content_base64": b64(MESSY_ORDERS_CSV)})).structuredContent
            ds = up["dataset_id"]
            ok = await s.call_tool("validate_plan", {"plan": _plan_for(ds)})
            invented = await s.call_tool("validate_plan", {"plan": _plan_for(
                ds, {"op": "derive", "name": "x", "expression": "discount * 2"})})
            malformed = await s.call_tool("validate_plan", {"plan": {"name": "Bad Name", "sources": []}})
            unsafe = await s.call_tool("validate_plan", {"plan": {**_plan_for(ds), "outputs": [
                {"kind": "sql", "name": "leak", "sources": ["clean.orders"],
                 "query": "SELECT * FROM read_csv('/etc/passwd')"}]}})
            built = await s.call_tool("compile_plan", {"draft_id": ok.structuredContent["draft_id"]})
            listed = (await s.call_tool("list_pipelines", {})).structuredContent
            saved = (await s.call_tool("list_saved_plans", {})).structuredContent
            return ds, ok, invented, malformed, unsafe, built, listed, saved

    ds, ok, invented, malformed, unsafe, built, listed, saved = run(alice_flow())
    draft = ok.structuredContent["draft_id"]
    assert ok.structuredContent["steps_received"]["clean.orders"][0].startswith("1. trim")
    assert not ok.isError and ok.structuredContent["valid"] is True
    assert ok.structuredContent["assets"]["clean.orders"]["steps"][-1]["op"] == "dedupe"
    assert invented.isError and "'discount'" in invented.content[0].text
    assert malformed.isError and "expected format" in malformed.content[0].text
    assert unsafe.isError and "table functions are not allowed" in unsafe.content[0].text
    assert not built.isError and built.structuredContent["bruin_validate"] == "passed"
    pid = built.structuredContent["pipeline_id"]
    assert "CREATE TABLE clean.orders AS WITH" in built.structuredContent["assets"]["clean.orders"]["rendered"]
    assert [p["pipeline_id"] for p in listed["pipelines"]] == [pid]
    assert {p["name"] for p in saved["saved_plans"]} >= {"invoice_cleanup", "ml_split"}

    async def bob_flow():
        async with mcp_session(server["tokens"]["bob"]) as s:
            read = await s.call_tool("get_pipeline", {"pipeline_id": pid})
            use = await s.call_tool("validate_plan", {"plan": _plan_for(ds)})
            steal = await s.call_tool("compile_plan", {"draft_id": draft})
            mine = (await s.call_tool("list_pipelines", {})).structuredContent
            return read, use, steal, mine
    read, use, steal, mine = run(bob_flow())
    assert read.isError and "no pipeline" in read.content[0].text
    assert use.isError and "no dataset" in use.content[0].text
    assert steal.isError and "no draft" in steal.content[0].text
    assert mine["pipelines"] == []


def test_plan_tools_need_permission(server):
    async def go():
        async with mcp_session(server["tokens"]["carol_health_only"]) as s:
            return await s.call_tool("compile_plan", {"draft_id": "dr_0000000000000000"})
    res = run(go())
    assert res.isError and "not authorized" in res.content[0].text


# --------------------------------------------- run, export, download (Phase 6)

def test_run_export_download_over_mcp(server):
    import base64 as _b64
    import hashlib as _hl

    async def alice_flow():
        async with mcp_session(server["tokens"]["alice"]) as s:
            ds = (await s.call_tool("upload_dataset", {"filename": "synthetic_orders.csv",
                                                       "content_base64": b64(MESSY_ORDERS_CSV)})).structuredContent["dataset_id"]
            draft = (await s.call_tool("validate_plan", {"plan": _plan_for(ds)})).structuredContent["draft_id"]
            pid = (await s.call_tool("compile_plan", {"draft_id": draft})).structuredContent["pipeline_id"]
            started = (await s.call_tool("run_pipeline", {"pipeline_id": pid})).structuredContent
            status = started
            for _ in range(300):
                status = (await s.call_tool("get_run", {"pipeline_id": pid, "run_id": started["run_id"]})).structuredContent
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.3)
            meta = (await s.call_tool("export_asset", {"pipeline_id": pid, "asset": "clean.orders"})).structuredContent
            data, offset = b"", 0
            while offset is not None:
                part = (await s.call_tool("download_export", {"export_id": meta["export_id"], "offset": offset})).structuredContent
                data += _b64.b64decode(part["content_base64"])
                offset = part["next_offset"]
            return pid, status, meta, data

    pid, result, meta, data = run(alice_flow())
    assert result["status"] == "succeeded" and result["tables"]["clean.orders"]["rows"] == 10
    assert _hl.sha256(data).hexdigest() == meta["sha256"] and data.startswith(b"order_id,")

    async def bob_flow():
        async with mcp_session(server["tokens"]["bob"]) as s:
            return (await s.call_tool("run_pipeline", {"pipeline_id": pid}),
                    await s.call_tool("download_export", {"export_id": meta["export_id"]}))
    bob_run, bob_dl = run(bob_flow())
    assert bob_run.isError and "no pipeline" in bob_run.content[0].text
    assert bob_dl.isError and "no export" in bob_dl.content[0].text


# ------------------------------------------- flat single-table form (item 1)

def test_flat_table_plan_over_mcp_schema_validate_compile_run(server):
    async def go():
        async with mcp_session(server["tokens"]["alice"]) as s:
            tools = {t.name: t for t in (await s.list_tools()).tools}
            ds = (await s.call_tool("upload_dataset", {"filename": "synthetic_orders.csv",
                                                       "content_base64": b64(MESSY_ORDERS_CSV)})).structuredContent["dataset_id"]
            report = await s.call_tool("validate_table_plan", {
                "dataset_id": ds, "table": "orders",
                "steps": [{"op": "trim", "columns": ["customer", "country"]},
                          {"op": "change_case", "columns": ["country"], "to": "upper"},
                          {"op": "cast", "columns": {"order_id": "BIGINT"}},
                          {"op": "dedupe", "key": ["order_id"], "order_by": ["order_id"]}]})
            bad = await s.call_tool("validate_table_plan", {
                "dataset_id": ds, "steps": [{"op": "dedupe", "options": {"by": "order_id"}}]})
            pid = (await s.call_tool("compile_plan", {"draft_id": report.structuredContent["draft_id"]})).structuredContent["pipeline_id"]
            started = (await s.call_tool("run_pipeline", {"pipeline_id": pid})).structuredContent
            status = started
            for _ in range(300):
                status = (await s.call_tool("get_run", {"pipeline_id": pid, "run_id": started["run_id"]})).structuredContent
                if status["status"] != "running":
                    break
                await asyncio.sleep(0.3)
            return tools, report, bad, status

    tools, report, bad, status = run(go())
    schema = tools["validate_table_plan"].inputSchema
    assert "$ref" not in json.dumps(schema) and len(schema["properties"]["steps"]["items"]["oneOf"]) == 14
    assert tools["compile_plan"].inputSchema["required"] == ["draft_id"]
    assert not report.isError and len(report.structuredContent["steps_received"]["clean.orders"]) == 4
    assert bad.isError and "dedupe accepts: op, key" in bad.content[0].text
    assert status["status"] == "succeeded" and status["tables"]["clean.orders"]["rows"] == 10
