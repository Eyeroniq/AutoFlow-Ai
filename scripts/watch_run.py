#!/usr/bin/env python
"""Run a FlowForge workflow and watch its execution events live over the WebSocket.

    pip install -r scripts/requirements.txt
    export FLOWFORGE_PASSWORD=...            # never pass passwords on the command line
    python scripts/watch_run.py --email you@example.com --graph scripts/examples/delay.json

Steps: log in (optionally --register first), create a workflow from --graph (or use
--workflow-id), POST /run (timed: it returns 202 before the work is done), connect to
WS /ws/executions/{id} (authenticating with a first message, or ?token= with
--auth-mode query), and print every event as it arrives with the time since the POST.

Demo helpers:
  --stop-after 2        POST /stop two seconds after the run was queued
  --connect-delay 8     join late (the snapshot replays what already happened)
  --execution-id ID     just watch an existing execution
  --show-final          print the execution from GET /api/executions/{id} at the end
"""

import argparse
import asyncio
import getpass
import json
import os
import sys
import time
from typing import Any

try:
    import httpx
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed
except ImportError as exc:  # most often: run with a Python that isn't the project's venv
    sys.exit(
        f"Missing dependency: {exc.name}. Either run it with the API's venv, e.g.\n"
        "    apps/api/venv/Scripts/python scripts/watch_run.py ...   (Windows)\n"
        "    apps/api/venv/bin/python scripts/watch_run.py ...       (macOS/Linux)\n"
        "or install what it needs into this Python:\n"
        "    python -m pip install -r scripts/requirements.txt"
    )

T0 = time.perf_counter()


def stamp() -> str:
    return f"+{(time.perf_counter() - T0) * 1000:8.0f} ms"


def out(line: str) -> None:
    print(f"{stamp()}  {line}", flush=True)


def preview(value: Any, limit: int = 90) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def describe(event: dict[str, Any], show_tokens: bool) -> str | None:
    kind = event.get("type")
    seq = event.get("seq")
    head = f"[seq {seq:>3}] {kind:<19}" if isinstance(seq, int) else f"[   -  ] {kind:<19}"
    node = event.get("node_key")
    if kind == "snapshot":
        execution = event["execution"]
        nodes = ", ".join(f"{n['node_key']}={n['status']}" for n in execution["node_executions"])
        tag = " (resync)" if event.get("resync") else ""
        return f"{head} status={execution['status']}{tag} | {nodes}"
    if kind == "execution.started":
        return f"{head} worker={event.get('worker')} queue={event.get('queue')}"
    if kind == "node.started":
        return f"{head} {node} ({event.get('node_type')})"
    if kind == "node.token":
        return f"{head} {node} {preview(event.get('text', ''), 60)!r}" if show_tokens else None
    if kind == "node.succeeded":
        output = event.get("output") or {}
        detail = (output.get("response") or output.get("message_id") or output.get("waited_seconds")
                  or output.get("value") or output)
        extra = f" provider_used={output['provider_used']}" if "provider_used" in output else ""
        return f"{head} {node} in {event.get('duration_ms')} ms{extra} -> {preview(detail)}"
    if kind == "node.failed":
        return f"{head} {node}: {preview(event.get('error'), 160)}"
    if kind == "node.skipped":
        return f"{head} {node}: {event.get('reason')}"
    if kind == "execution.finished":
        replay = " (replayed from DB)" if event.get("replayed") else ""
        detail = f" error={preview(event.get('error'), 160)}" if event.get("error") else ""
        result = f" final_output={preview(event.get('final_output'))}" if event.get("final_output") else ""
        return f"{head} status={event.get('status')} duration_ms={event.get('duration_ms')}{replay}{detail}{result}"
    if kind == "error":
        return f"{head} {event.get('code')}: {event.get('message')}"
    if kind in ("heartbeat", "pong"):
        return None
    return f"{head} {preview(event)}"


async def watch(args: argparse.Namespace, token: str, execution_id: str) -> str | None:
    ws_base = args.api.replace("https://", "wss://").replace("http://", "ws://")
    url = f"{ws_base}/ws/executions/{execution_id}"
    if args.auth_mode == "query":
        url += f"?token={token}"
    status = None
    stopper = None
    async with connect(url, max_size=None, open_timeout=10) as ws:
        out(f"WS connected ({args.auth_mode} auth)")
        if args.auth_mode == "first-message":
            await ws.send(json.dumps({"type": "auth", "token": token}))
        if args.stop_after is not None:
            stopper = asyncio.create_task(stop_later(args, token, execution_id))
        try:
            async for raw in ws:
                event = json.loads(raw)
                if args.json:
                    out(raw if isinstance(raw, str) else raw.decode())
                else:
                    line = describe(event, args.show_tokens)
                    if line:
                        out(line)
                if event.get("type") == "execution.finished":
                    status = event.get("status")
                elif event.get("type") == "snapshot":
                    status = event["execution"]["status"]
        except ConnectionClosed:
            pass
        out(f"WS closed by server: code={ws.close_code} reason={ws.close_reason!r}")
    if stopper is not None:
        await stopper
    return status


async def stop_later(args: argparse.Namespace, token: str, execution_id: str) -> None:
    await asyncio.sleep(max(0.0, args.stop_after - (time.perf_counter() - T0)))
    async with httpx.AsyncClient(base_url=args.api, timeout=30) as http:
        start = time.perf_counter()
        response = await http.post(f"/api/executions/{execution_id}/stop", headers={"Authorization": f"Bearer {token}"})
        took = (time.perf_counter() - start) * 1000
    body = response.json()
    out(f"POST /stop -> {response.status_code} in {took:.0f} ms: status={body.get('status', body)}")


def login(http: httpx.Client, args: argparse.Namespace) -> str:
    password = os.environ.get("FLOWFORGE_PASSWORD") or getpass.getpass(f"Password for {args.email}: ")
    if args.register:
        response = http.post("/api/auth/register", json={"email": args.email, "password": password, "full_name": args.email})
        if response.status_code not in (201, 409):
            sys.exit(f"register failed: {response.status_code} {response.text}")
    response = http.post("/api/auth/login", json={"email": args.email, "password": password})
    if response.status_code != 200:
        sys.exit(f"login failed: {response.status_code} {response.text}")
    return response.json()["access_token"]


def create_workflow(http: httpx.Client, headers: dict[str, str], args: argparse.Namespace) -> str:
    with open(args.graph, encoding="utf-8") as fh:
        graph = json.load(fh)
    for assignment in args.var:
        key, _, value = assignment.partition("=")
        graph["variables"] = [v for v in graph.get("variables", []) if v["key"] != key] + [
            {"key": key, "value": value, "type": "workflow"}
        ]
    name = args.name or os.path.basename(args.graph)
    workflow_id = http.post("/api/workflows", json={"name": name}, headers=headers).json()["id"]
    response = http.put(f"/api/workflows/{workflow_id}", json={"graph": graph}, headers=headers)
    if response.status_code != 200:
        sys.exit(f"saving the graph failed: {response.status_code} {response.text}")
    return workflow_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default=os.environ.get("FLOWFORGE_API", "http://localhost:8000"))
    parser.add_argument("--email", required=True)
    parser.add_argument("--register", action="store_true", help="create the account first if it doesn't exist")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--graph", help="workflow graph JSON file to create and run")
    target.add_argument("--workflow-id", help="an existing workflow to run")
    target.add_argument("--execution-id", help="don't run anything; watch this execution")
    parser.add_argument("--name", help="name for the created workflow")
    parser.add_argument("--inputs", default="{}", help="run inputs as JSON, e.g. '{\"topic\": \"tides\"}'")
    parser.add_argument("--var", action="append", default=[], metavar="KEY=VALUE", help="set a workflow variable")
    parser.add_argument("--auth-mode", choices=("first-message", "query"), default="first-message")
    parser.add_argument("--connect-delay", type=float, default=0.0, help="seconds to wait before connecting")
    parser.add_argument("--stop-after", type=float, help="POST /stop this many seconds after queueing")
    parser.add_argument("--show-tokens", action="store_true", help="print node.token events")
    parser.add_argument("--show-final", action="store_true", help="print GET /api/executions/{id} at the end")
    parser.add_argument("--json", action="store_true", help="print raw event JSON")
    args = parser.parse_args()
    # Model output is arbitrary Unicode; don't die on a console that can't encode it.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    global T0
    with httpx.Client(base_url=args.api, timeout=60) as http:
        token = login(http, args)
        headers = {"Authorization": f"Bearer {token}"}
        execution_id = args.execution_id
        if execution_id is None:
            workflow_id = args.workflow_id or create_workflow(http, headers, args)
            T0 = time.perf_counter()
            response = http.post(f"/api/workflows/{workflow_id}/run", json={"inputs": json.loads(args.inputs)}, headers=headers)
            took = (time.perf_counter() - T0) * 1000
            if response.status_code != 202:
                sys.exit(f"run failed: {response.status_code} {response.text}")
            body = response.json()
            execution_id = body["execution_id"]
            out(f"POST /run -> 202 in {took:.0f} ms: execution {execution_id} status={body['status']} queue={body['queue']}")
        else:
            T0 = time.perf_counter()

    if args.connect_delay:
        out(f"waiting {args.connect_delay:g}s before connecting (late join)")
        time.sleep(args.connect_delay)
    status = asyncio.run(watch(args, token, execution_id))

    if args.show_final:
        with httpx.Client(base_url=args.api, timeout=30) as http:
            execution = http.get(f"/api/executions/{execution_id}", headers={"Authorization": f"Bearer {token}"}).json()
        print("\nDatabase state (GET /api/executions/{id}):")
        print(f"  status={execution['status']} error={execution['error_message']!r} worker={execution['worker_hostname']}"
              f" queue={execution['queue']} duration_ms={execution['duration_ms']}")
        for node in execution["node_executions"]:
            print(f"  - {node['node_key']:<8} {node['status']:<8} {str(node['duration_ms']) + ' ms':>9}  {node['error_message'] or ''}")
    sys.exit(0 if status == "success" else 1)


if __name__ == "__main__":
    main()
