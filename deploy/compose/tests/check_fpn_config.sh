#!/usr/bin/env bash
set -euo pipefail

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

# Derive placeholders from both inputs so required secrets cannot hide a config regression.
grep -hEo '\$\{[A-Za-z_][A-Za-z0-9_]*:?\?' \
    deploy/compose/docker-compose.yml deploy/compose/docker-compose.fpn.yml \
    | sed -E 's/^\$\{//; s/:?\?$//' | sort -u \
    | while IFS= read -r name; do
        printf '%s=stub-%s\n' "$name" "$name"
    done > "$tmp_dir/env"

docker compose -p vexa-check --project-directory deploy/compose --env-file "$tmp_dir/env" \
    -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.fpn.yml \
    config --format json > "$tmp_dir/config.json"

python3 - "$tmp_dir/config.json" <<'PY'
import json
from pathlib import Path
import sys

with open(sys.argv[1]) as config_file:
    services = json.load(config_file)["services"]


def check(number, name, condition):
    if not condition:
        sys.exit(f"FAIL: {number}: {name}")
    print(f"PASS: {number}")


runtime = services.get("runtime", {})
check(1, "runtime has no Docker socket mount", "runtime" in services and all(
    "docker.sock" not in str(volume.get(field, ""))
    for volume in runtime.get("volumes", [])
    for field in ("source", "target")
))
check(2, "runtime DOCKER_HOST points to docker-proxy",
      runtime.get("environment", {}).get("DOCKER_HOST") == "tcp://docker-proxy:2375")
check(3, "runtime has no group_add entries", not runtime.get("group_add"))

proxy = services.get("docker-proxy", {})
image = proxy.get("image", "")
environment = proxy.get("environment", {})
config_mounts = [
    volume for volume in proxy.get("volumes", [])
    if volume.get("type") == "bind"
    and str(volume.get("source", "")).endswith("docker-proxy/haproxy.cfg")
    and volume.get("target") == "/usr/local/etc/haproxy/haproxy.cfg"
    and volume.get("read_only") is True
]
check(4, "docker-proxy pinned image, read-only mounts, hardening and no section flags",
      "docker-proxy" in services
      and image.startswith(("tecnativa/docker-socket-proxy:",
                            "ghcr.io/tecnativa/docker-socket-proxy:"))
      and image.split(":", 1)[1].split("@", 1)[0] not in ("", "latest")
      and any(volume.get("type") == "bind"
              and volume.get("source") == "/var/run/docker.sock"
              and volume.get("target") == "/var/run/docker.sock"
              and volume.get("read_only") is True
              for volume in proxy.get("volumes", []))
      and len(config_mounts) == 1
      and not proxy.get("ports")
      and proxy.get("read_only") is True
      and "no-new-privileges:true" in proxy.get("security_opt", [])
      and "ALL" in proxy.get("cap_drop", [])
      and not any(key in environment for key in ("CONTAINERS", "IMAGES", "POST", "EXEC")))

proxy_networks = set(proxy.get("networks", {}))
runtime_networks = set(runtime.get("networks", {}))
check(5, "docker-proxy has only one network, shared with runtime",
      len(proxy_networks) == 1 and len(proxy_networks & runtime_networks) == 1)
config_path = Path(config_mounts[0]["source"]).resolve()
repo_root = Path.cwd().resolve()
in_repo = repo_root in config_path.parents
config_text = config_path.read_text() if in_repo and config_path.is_file() else ""
check(6, "repo HAProxy config exists, denies requests and has no template placeholders",
      in_repo and config_path.is_file()
      and "http-request deny" in config_text and "${" not in config_text)

# ── C4: split secrets, bot credentials, Redis ACL ─────────────────────────────────────────────────
active = {name: svc for name, svc in services.items() if not svc.get("profiles")}


def env_of(name):
    return active.get(name, {}).get("environment") or {}


def holders(var):
    stub = f"stub-{var}"
    return {name for name, svc in active.items()
            if stub in json.dumps(svc.get("environment") or {}) + json.dumps(svc.get("command") or "")}


expected = {
    "GATEWAY_IDENTITY_SECRET": {"gateway", "meeting-api"},
    "ADMIN_API_INTERNAL_SECRET": {"admin-api", "gateway", "meeting-api"},
    "RUNTIME_CALLBACK_SECRET": {"runtime", "meeting-api"},
    "MEETING_TOKEN_SECRET": {"meeting-api"},
}
check(7, "each split secret reaches only its own surface",
      all(holders(var) == want for var, want in expected.items()))
meeting = env_of("meeting-api")
check(8, "meeting-api: explicit stock-bot compat decision, per-meeting Redis ACL, no shared/admin secret",
      meeting.get("BOT_LEGACY_INTERNAL_SECRET") == "stub-BOT_LEGACY_INTERNAL_SECRET"
      and meeting.get("BOT_REDIS_ACL") == "true"
      and "INTERNAL_API_SECRET" not in meeting and "ADMIN_TOKEN" not in meeting
      and env_of("admin-api").get("INTERNAL_API_SECRET") == "stub-ADMIN_API_INTERNAL_SECRET")
redis_cmd = active.get("redis", {}).get("command") or []
urls = [v for svc in active.values() for k, v in (svc.get("environment") or {}).items()
        if k.endswith("REDIS_URL") and v]
check(9, "redis: default user off, services connect as vexa-svc",
      " ".join(redis_cmd).find("--user default off") >= 0
      and "--requirepass" not in redis_cmd
      and urls and all(u.startswith("redis://vexa-svc:stub-REDIS_PASSWORD@redis:") for u in urls))
print("CHECK-FPN-CONFIG OK")
PY
