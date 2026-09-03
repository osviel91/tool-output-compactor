"""Drive a live Hermes session against the running Desktop gateway.

Used to validate tool-output-compactor end-to-end on a real (small) model:
the session runs in the Desktop backend, whose process already imported the
currently installed plugin source from ~/.hermes/plugins/tool-output-compactor.

REQUIRES the venv python (websockets is not installed for system python3):
    ~/.hermes/hermes-agent/venv/bin/python live/live_test.py [prompt...]

Config (no hardcoded tokens):
    TOC_WS_PORT   gateway port (required; the Desktop backend picks a random
                  port per launch). Find it with:
                      lsof -nP -iTCP -sTCP:LISTEN | grep hermes
                  or ask Hermes: the Desktop backend logs the chosen port.
    TOC_WS_MODEL  model id, default fast-new.

The gateway session token is fetched at runtime from the HTTP root page:
    curl -s http://127.0.0.1:$PORT/   # __HERMES_SESSION_TOKEN__ in the HTML

Default prompt runs live/gen_records.py (uniform JSON record array) which the
compactor should render schema-once as `JSON records:` rows.

Verification after the run (DB persists the compacted tool message):
    sqlite3 ~/.hermes/state.db \
      "SELECT id, role, tool_name, length(content) FROM messages
       WHERE session_id='<stored_session_id>' ORDER BY id;"
    # compacted content contains the marker + 'JSON records:' header
"""

import asyncio
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

import websockets

PORT = os.environ.get("TOC_WS_PORT", "")
MODEL = os.environ.get("TOC_WS_MODEL", "fast-new")

_GEN = Path(__file__).resolve().parent / "gen_records.py"

PROMPT = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else (
    "Run exactly this command with the terminal tool and then tell me how many "
    "total records and how many BLOCKER-severity records it contains. Command: "
    f"python3 {_GEN}"
)

_results: dict = {}


def _fetch_token() -> str:
    if not PORT:
        raise SystemExit("TOC_WS_PORT is required (Desktop backend port per launch)")
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=5) as resp:
        html = resp.read().decode("utf-8", "replace")
    m = re.search(r"__HERMES_SESSION_TOKEN__\s*[:=]\s*[\"']([^\"']+)[\"']", html)
    if not m:
        raise SystemExit(f"no __HERMES_SESSION_TOKEN__ found on http://127.0.0.1:{PORT}/")
    return m.group(1)


async def main() -> None:
    token = _fetch_token()
    uri = f"ws://127.0.0.1:{PORT}/api/ws?token={token}"
    async with websockets.connect(uri) as ws:
        rid = 0

        async def rpc(method: str, params: dict) -> dict:
            nonlocal rid
            rid += 1
            fut = asyncio.get_running_loop().create_future()
            _results[rid] = fut
            await ws.send(json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
            ))
            return await fut

        async def pump() -> None:
            async for raw in ws:
                msg = json.loads(raw)
                m = msg.get("method")
                if m == "event":
                    ev = msg.get("params", {}).get("type", "")
                    pl = msg.get("params", {}).get("payload", {})
                    if ev in ("message.start", "turn.start"):
                        print(f"[event] {ev}", flush=True)
                    elif ev in ("message.complete", "turn.complete"):
                        print(f"[event] {ev}", flush=True)
                    elif ev == "assistant":
                        txt = (pl.get("text") if isinstance(pl, dict) else "") or ""
                        if txt:
                            print(f"[assistant] {txt[:300]}", flush=True)
                    elif ev == "status.update":
                        print(f"[status] {pl.get('text', '') if isinstance(pl, dict) else ''}", flush=True)
                elif "id" in msg:
                    fut = _results.pop(msg["id"], None)
                    if fut and not fut.done():
                        if "error" in msg:
                            fut.set_result({"error": msg["error"]})
                        else:
                            fut.set_result(msg.get("result") or {})

        pump_task = asyncio.create_task(pump())
        try:
            create = await rpc("session.create", {
                "model": MODEL,
                "source": "desktop",
                "title": "tool-output-compactor live test",
            })
            print("session.create ->", json.dumps(create)[:400], flush=True)
            sid = create.get("session_id")
            stored = create.get("stored_session_id", "")
            if not sid:
                print("NO SESSION ID", flush=True)
                return
            print(f"stored_session_id: {stored}", flush=True)
            time.sleep(2)
            res = await rpc("prompt.submit", {"session_id": sid, "text": PROMPT})
            print("prompt.submit ->", json.dumps(res)[:400], flush=True)
            await asyncio.sleep(240)
        finally:
            pump_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
