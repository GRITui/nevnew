#!/usr/bin/env python3
# cron suggestion (NOT installed): */30 * * * * /usr/bin/python3 /Volumes/Ugreen_WD\ 1.0\ TB/grit/nevnew/scripts/e2e_nevnew.py >> /tmp/nevnew_e2e.log 2>&1 # nevnew_e2e
"""Headless end-to-end checks for NevNew (DRAFT, stdlib-only).

Usage: e2e_nevnew.py [--json]

Each check returns (name, ok, detail). Exit 0 if all ok (after heals), 1 otherwise.
Self-heal (conservative): only check 1 (docker containers) may trigger
`docker restart <name>` ONCE per failed container, max 2 restarts per run,
then wait 20s and re-check. All other failures are report-only.
Secrets are read from the repo .env (never printed; output is redacted).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# cron/launchd give a minimal PATH (docker lives in /usr/local/bin or
# /opt/homebrew/bin). Same precedent as scripts/error_watcher.py.
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/sbin:/sbin:/usr/bin:/bin"

# Resolve the nevnew repo: when this file lives in <repo>/scripts/ that is
# dirname(dirname(__file__)); the draft lives in /tmp/ so fall back to the
# absolute repo path for .env lookup.
_CANDIDATE = Path(__file__).resolve().parent.parent / ".env"
if _CANDIDATE.exists():
    REPO = str(_CANDIDATE.parent)
else:
    REPO = "/Volumes/Ugreen_WD 1.0 TB/grit/nevnew"
ENV_PATH = os.path.join(REPO, ".env")

CONTAINERS = [
    "newnew-litellm",
    "newnew-ai-core",
    "newnew-telegram-bot",
    "newnew-memory",
    "newnew-mcpo",
    "newnew-postgres",
    "newnew-redis",
    "newnew-cloudflared",
]

AICORE_URL = "http://localhost:8010"
LITELLM_URL = "http://localhost:4000"
N8N_URL = "http://localhost:5678/"
EXPECTED_MODELS = {"NevNew", "NevNew-Pro", "NevNew-Mini", "NevNew-Haiku"}

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{20,}")


def load_env() -> dict:
    vals: dict = {}
    for name in ("LITELLM_MASTER_KEY", "AICORE_API_KEY", "TELEGRAM_BOT_TOKEN"):
        vals[name] = os.environ.get(name, "")
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                if k in vals and not vals[k]:
                    v = v.strip().strip("'\"")
                    vals[k] = v
    return vals


ENV = load_env()


def redact(s: str) -> str:
    return TOKEN_RE.sub("[REDACTED]", s)


def fmt_detail(s: str, limit: int = 200) -> str:
    return redact(s)[:limit]


def http_get(url: str, headers: dict | None = None, timeout: int = 20) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def http_post_json(url: str, payload: dict, headers: dict | None = None, timeout: int = 60) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def auth_header(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"} if key else {}


def docker_status(name: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", name],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, OSError) as e:
        return False, f"{name} inspect error: {type(e).__name__}"
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "inspect failed").strip().splitlines()
        return False, f"{name} inspect failed: {err[0] if err else 'unknown'}"
    status = r.stdout.strip()
    if status == "running":
        return True, f"{name} running"
    return False, f"{name} state={status or 'unknown'}"


def check_containers() -> tuple[str, bool, str]:
    """Check 1: all expected containers running, with conservative self-heal."""
    failed = [c for c in CONTAINERS if not docker_status(c)[0]]
    healed: list[str] = []
    restarts = 0
    if failed:
        for name in failed[:2]:  # never restart more than 2 per run
            try:
                subprocess.run(
                    ["docker", "restart", name],
                    capture_output=True, text=True, timeout=60,
                )
                restarts += 1
                healed.append(name)
            except (subprocess.SubprocessError, OSError):
                continue
        if healed:
            time.sleep(20)
    # Re-check everything after any heals.
    states: dict[str, str] = {}
    for c in CONTAINERS:
        ok, detail = docker_status(c)
        states[c] = "running" if ok else detail.split(" ", 1)[-1][:60]
    still_bad = [c for c in CONTAINERS if not docker_status(c)[0]]
    ok = not still_bad
    detail = f"running={sum(1 for c in CONTAINERS if c not in still_bad)}/{len(CONTAINERS)}"
    if still_bad:
        detail += f" bad={','.join(still_bad)}"
    detail += f" healed={'true' if healed and ok else 'false'}"
    if healed:
        detail += f" restarted={','.join(healed)}"
    if restarts == 0 and failed:
        detail += " healed=false"
    return "docker", ok, detail


def check_ready() -> tuple[str, bool, str]:
    try:
        status, body = http_get(f"{AICORE_URL}/ready", timeout=20)
    except Exception as e:  # URLError/TimeoutError/OSError — report, never raise
        return "ready", False, f"GET /ready failed: {type(e).__name__} healed=false"
    if status != 200:
        return "ready", False, f"GET /ready http={status} healed=false"
    try:
        data = json.loads(body)
    except ValueError:
        return "ready", False, "GET /ready non-JSON body healed=false"
    overall = str(data.get("status", "")).lower()
    checks = data.get("checks", {})
    if overall != "ok":
        return "ready", False, f"status={overall or 'missing'} healed=false"
    if not isinstance(checks, dict) or not checks:
        return "ready", False, "status=ok checks=missing healed=false"
    bad = [k for k, v in checks.items()
           if re.search(r"fail|error|down|unhealth|not.?ok", str(v), re.IGNORECASE)]
    if bad:
        return "ready", False, f"status=ok bad={','.join(bad)} healed=false"
    summary = ",".join(f"{k}={v}" for k, v in checks.items())
    return "ready", True, f"status=ok checks={summary} healed=false"


def check_models() -> tuple[str, bool, str]:
    key = ENV.get("LITELLM_MASTER_KEY", "")
    if not key:
        return "models", False, "LITELLM_MASTER_KEY missing healed=false"
    try:
        status, body = http_get(
            f"{LITELLM_URL}/v1/models", headers=auth_header(key), timeout=20)
    except Exception as e:
        return "models", False, f"GET /v1/models failed: {type(e).__name__} healed=false"
    if status != 200:
        return "models", False, f"GET /v1/models http={status} healed=false"
    try:
        ids = {m.get("id", "") for m in json.loads(body).get("data", [])}
    except ValueError:
        return "models", False, "GET /v1/models non-JSON healed=false"
    missing = EXPECTED_MODELS - ids
    if missing:
        return "models", False, f"missing={','.join(sorted(missing))} healed=false"
    return "models", True, f"models={','.join(sorted(EXPECTED_MODELS))} healed=false"


def check_chat() -> tuple[str, bool, str]:
    key = ENV.get("AICORE_API_KEY", "")
    if not key:
        return "chat", False, "AICORE_API_KEY missing healed=false"
    payload = {
        "user_id": "e2e-probe",
        "messages": [{"role": "user", "content": "reply with exactly: e2e-ok"}],
        "channel": "e2e",
        "store_memories": False,
    }
    try:
        status, body = http_post_json(
            f"{AICORE_URL}/chat", payload, headers=auth_header(key), timeout=120)
    except Exception as e:
        return "chat", False, f"POST /chat failed: {type(e).__name__} healed=false"
    if status != 200:
        return "chat", False, f"POST /chat http={status} healed=false"
    try:
        reply = json.loads(body).get("reply", "")
    except ValueError:
        return "chat", False, "POST /chat non-JSON healed=false"
    if str(reply).strip() == "e2e-ok":
        return "chat", True, "reply=e2e-ok healed=false"
    return "chat", False, f"reply={str(reply).strip()[:60] or 'empty'} healed=false"


def check_memory_proxy() -> tuple[str, bool, str]:
    key = ENV.get("AICORE_API_KEY", "")
    try:
        status, body = http_get(
            f"{AICORE_URL}/memory/users/e2e-probe/memories",
            headers=auth_header(key), timeout=30)
    except Exception as e:
        return "memory", False, f"GET /memory failed: {type(e).__name__} healed=false"
    if status == 200:
        return "memory", True, f"http=200 bytes={len(body)} healed=false"
    return "memory", False, f"http={status} healed=false"


def check_telegram() -> tuple[str, bool, str]:
    token = ENV.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return "telegram", False, "TELEGRAM_BOT_TOKEN missing healed=false"
    try:
        # Token goes in the URL path (Telegram API shape); never echoed back
        # into detail output — names/ids only.
        status, body = http_get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=20)
    except Exception as e:
        return "telegram", False, f"getMe failed: {type(e).__name__} healed=false"
    if status != 200:
        return "telegram", False, f"getMe http={status} healed=false"
    try:
        data = json.loads(body)
    except ValueError:
        return "telegram", False, "getMe non-JSON healed=false"
    if data.get("ok") is True:
        res = data.get("result", {})
        uname = res.get("username", "?")
        bid = res.get("id", "?")
        return "telegram", True, f"ok:true username=@{uname} id={bid} healed=false"
    return "telegram", False, "ok:false healed=false"


def check_tools() -> tuple[str, bool, str]:
    key = ENV.get("AICORE_API_KEY", "")
    try:
        status, body = http_get(
            f"{AICORE_URL}/tools", headers=auth_header(key), timeout=30)
    except Exception as e:
        return "tools", False, f"GET /tools failed: {type(e).__name__} healed=false"
    if status != 200:
        return "tools", False, f"GET /tools http={status} healed=false"
    try:
        data = json.loads(body)
    except ValueError:
        return "tools", False, "GET /tools non-JSON healed=false"
    mcpo = str(data.get("mcpo_status", ""))
    if mcpo.lower().startswith("ok"):
        return "tools", True, f"mcpo_status={mcpo[:60]} healed=false"
    return "tools", False, f"mcpo_status={mcpo[:60] or 'missing'} healed=false"


def check_n8n() -> tuple[str, bool, str]:
    try:
        status, _ = http_get(N8N_URL, timeout=20)
    except Exception as e:
        return "n8n", False, f"GET / failed: {type(e).__name__} healed=false"
    if status == 200:
        return "n8n", True, "http=200 healed=false"
    return "n8n", False, f"http={status} healed=false"


def main() -> int:
    want_json = "--json" in sys.argv[1:]
    results = [
        check_containers(),
        check_ready(),
        check_models(),
        check_chat(),
        check_memory_proxy(),
        check_telegram(),
        check_tools(),
        check_n8n(),
    ]
    if want_json:
        print(json.dumps(
            [{"name": n, "ok": o, "detail": fmt_detail(d)} for n, o, d in results],
            indent=2))
    else:
        for name, ok, detail in results:
            tag = "PASS" if ok else "FAIL"
            print(f"{tag}({name}): {fmt_detail(detail)}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
