#!/usr/bin/env python3
"""Push current branch, force HACS to redownload sunSale, restart HA.

Pipeline:
  1. git push (current branch -> origin)
  2. POST homeassistant.update_entity for update.sunsale_update so HACS
     re-queries GitHub for the latest commit SHA on the tracked branch.
  3. Poll update.sunsale_update until latest_version matches the new HEAD.
  4. POST update.install to make HACS download the new commit.
  5. Poll until installed_version matches HEAD and in_progress clears.
  6. POST homeassistant.restart.
  7. Poll /api/ until HA is back, then poll /api/sun_sale/debug until
     the integration is loaded again.

Usage:
    python tools/deploy.py
    HA_URL=http://host:port HA_TOKEN=... python tools/deploy.py
    python tools/deploy.py --no-push     # skip git push (already pushed)
    python tools/deploy.py --skip-restart
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_HA_URL = "http://85.206.57.75:8124"
DEFAULT_HA_TOKEN = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiI1YWQzNTk2MmJmMGE0Yjg5YmY0ZTM5N2VjOWJkNDlhMiIsImlhdCI6MTc3NzI0NDI2NCwiZXhwIjoyMDkyNjA0MjY0fQ."
    "fKvXg_uBCvNV23MHaDoKiJrHDlD5VtlD1-7B_e7N7VQ"
)
UPDATE_ENTITY = "update.sunsale_update"


class HA:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _req(self, method: str, path: str, body: dict | None = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else None

    def state(self, entity_id: str) -> dict | None:
        try:
            return self._req("GET", f"/api/states/{entity_id}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise

    def call(self, domain: str, service: str, data: dict) -> Any:
        return self._req("POST", f"/api/services/{domain}/{service}", data)

    def alive(self) -> bool:
        try:
            self._req("GET", "/api/")
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            return False


def sh(cmd: list[str]) -> str:
    out = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def git_head_short() -> str:
    return sh(["git", "rev-parse", "--short=7", "HEAD"])


def git_push() -> None:
    branch = sh(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    print(f"[git] push {branch} -> origin")
    subprocess.run(["git", "push", "origin", branch], check=True)


def wait_until(
    desc: str,
    cond: callable,
    timeout_s: float,
    interval_s: float = 3.0,
) -> bool:
    print(f"[wait] {desc} (timeout {int(timeout_s)}s)")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if cond():
                return True
        except Exception as exc:  # noqa: BLE001
            print(f"  poll error: {exc}")
        time.sleep(interval_s)
    return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default=os.environ.get("HA_URL", DEFAULT_HA_URL))
    p.add_argument("--token", default=os.environ.get("HA_TOKEN", DEFAULT_HA_TOKEN))
    p.add_argument("--no-push", action="store_true", help="skip git push")
    p.add_argument("--skip-restart", action="store_true", help="redownload only, no HA restart")
    p.add_argument("--refresh-timeout", type=int, default=180, help="seconds to wait for HACS to see new SHA")
    p.add_argument("--install-timeout", type=int, default=180, help="seconds to wait for redownload")
    p.add_argument("--restart-timeout", type=int, default=240, help="seconds to wait for HA to come back")
    args = p.parse_args(argv)

    ha = HA(args.url, args.token)

    if not args.no_push:
        git_push()
    head = git_head_short()
    print(f"[git] HEAD = {head}")

    print(f"[ha] poking {UPDATE_ENTITY} -> homeassistant.update_entity")
    ha.call("homeassistant", "update_entity", {"entity_id": UPDATE_ENTITY})

    def latest_is_head() -> bool:
        st = ha.state(UPDATE_ENTITY)
        if not st:
            return False
        latest = (st.get("attributes") or {}).get("latest_version")
        print(f"  latest_version={latest}")
        return latest == head

    if not wait_until(f"HACS sees latest_version={head}", latest_is_head, args.refresh_timeout):
        print(f"[error] HACS never reported latest_version={head}", file=sys.stderr)
        return 2

    st = ha.state(UPDATE_ENTITY) or {}
    installed = (st.get("attributes") or {}).get("installed_version")
    if installed == head:
        print(f"[ha] installed_version already {head} — nothing to redownload")
    else:
        print(f"[ha] update.install ({installed} -> {head})")
        ha.call("update", "install", {"entity_id": UPDATE_ENTITY})

        def installed_is_head() -> bool:
            s = ha.state(UPDATE_ENTITY)
            if not s:
                return False
            attrs = s.get("attributes") or {}
            inst = attrs.get("installed_version")
            in_progress = attrs.get("in_progress")
            print(f"  installed_version={inst} in_progress={in_progress}")
            return inst == head and not in_progress

        if not wait_until(f"installed_version={head}", installed_is_head, args.install_timeout):
            print(f"[error] redownload did not complete to {head}", file=sys.stderr)
            return 3

    if args.skip_restart:
        print("[done] redownload complete, restart skipped")
        return 0

    print("[ha] homeassistant.restart")
    try:
        ha.call("homeassistant", "restart", {})
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        # Restart usually drops the connection mid-request; that's expected.
        print(f"  (request dropped as expected: {e})")

    # Wait for the API to disappear briefly, then come back.
    time.sleep(5)

    if not wait_until("HA API responding", ha.alive, args.restart_timeout, interval_s=4.0):
        print("[error] HA did not come back online", file=sys.stderr)
        return 4

    def integration_loaded() -> bool:
        try:
            payload = ha._req("GET", "/api/sun_sale/debug")  # noqa: SLF001
        except urllib.error.HTTPError as e:
            print(f"  /api/sun_sale/debug -> HTTP {e.code}")
            return False
        ok = isinstance(payload, list) and len(payload) > 0
        print(f"  /api/sun_sale/debug -> {len(payload) if isinstance(payload, list) else '?'} entries")
        return ok

    if not wait_until("sunSale integration loaded", integration_loaded, 120, interval_s=4.0):
        print("[warn] HA back up but sunSale debug endpoint not responding yet", file=sys.stderr)
        return 5

    print(f"[done] deployed {head} and HA restarted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
