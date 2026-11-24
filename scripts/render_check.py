"""Read-only look at Render: workspaces, services, env groups, and each one's variable NAMES (never values).

    # put RENDER_API_KEY=... in your local .env (gitignored), then:
    .venv/bin/python -m scripts.render_check
    .venv/bin/python -m scripts.render_check omai     # only services / groups whose name contains "omai"

Only GET requests. Variable values are never printed, so the output is safe to share.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

API = "https://api.render.com/v1"


def _key() -> str:
    key = os.getenv("RENDER_API_KEY", "")
    env = Path(__file__).resolve().parent.parent / ".env"
    if not key and env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("RENDER_API_KEY="):
                key = line.split("=", 1)[1].split("#", 1)[0].strip()
    if not key:
        raise SystemExit("Set RENDER_API_KEY in .env (never commit it)")
    return key


def _get(http: httpx.Client, path: str, **params: str) -> list:
    out, cursor = [], None
    while True:
        r = http.get(f"{API}{path}", params={**params, "limit": "100", **({"cursor": cursor} if cursor else {})})
        if r.status_code >= 400:
            raise SystemExit(f"GET {path}: HTTP {r.status_code}")
        page = r.json()
        if not isinstance(page, list):
            return [page]
        out.extend(page)
        if len(page) < 100:
            return out
        cursor = page[-1].get("cursor")


def main() -> None:
    flt = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    with httpx.Client(headers={"Authorization": f"Bearer {_key()}", "Accept": "application/json"}, timeout=30) as http:
        for o in _get(http, "/owners"):
            owner = o.get("owner", o)
            print(f"\n== workspace: {owner.get('name')} ({owner.get('id')})")
            for item in _get(http, "/services", ownerId=owner["id"]):
                svc = item.get("service", item)
                if flt and flt not in svc["name"].lower():
                    continue
                names = sorted(v.get("envVar", v).get("key") for v in _get(http, f"/services/{svc['id']}/env-vars"))
                print(f"  service {svc['name']} ({svc['type']}, {svc['id']}): {len(names)} vars")
                for n in names:
                    print(f"      {n}")
            for g in _get(http, "/env-groups", ownerId=owner["id"]):
                grp = g.get("envGroup", g)
                if flt and flt not in grp["name"].lower():
                    continue
                detail = _get(http, f"/env-groups/{grp['id']}")[0]
                names = sorted(v.get("key") for v in detail.get("envVars", []))
                linked = [s.get("name") or s.get("id") for s in detail.get("serviceLinks", [])]
                print(f"  env group {grp['name']} ({grp['id']}): {len(names)} vars, used by: {', '.join(linked) or '-'}")
                for n in names:
                    print(f"      {n}")


if __name__ == "__main__":
    main()
