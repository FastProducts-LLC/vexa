#!/usr/bin/env bash
set -euo pipefail

tmp_dir=$(mktemp -d)
trap 'rm -rf "$tmp_dir"' EXIT

# Derive placeholders from both inputs so required secrets cannot hide a config regression.
grep -hEo '\$\{[A-Za-z_][A-Za-z0-9_]*:?\?' \
    deploy/compose/docker-compose.yml deploy/compose/docker-compose.fpn.yml \
    | sed -E 's/^\$\{//; s/:?\?$//' | sort -u \
    | while IFS= read -r name; do
        printf '%s=stub\n' "$name"
    done > "$tmp_dir/env"

docker compose -p vexa-check --project-directory deploy/compose --env-file "$tmp_dir/env" \
    -f deploy/compose/docker-compose.yml -f deploy/compose/docker-compose.fpn.yml \
    config --format json > "$tmp_dir/config.json"

python3 - "$tmp_dir/config.json" <<'PY'
import json
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
disabled = (
    "EXEC VOLUMES NETWORKS BUILD SYSTEM INFO EVENTS SWARM SECRETS CONFIGS NODES "
    "SERVICES TASKS PLUGINS DISTRIBUTION COMMIT AUTH SESSION GRPC ALLOW_RESTARTS"
).split()
check(4, "docker-proxy image, socket mount, ports and API permissions",
      "docker-proxy" in services
      and image.startswith(("tecnativa/docker-socket-proxy:",
                            "ghcr.io/tecnativa/docker-socket-proxy:"))
      and image.split(":", 1)[1].split("@", 1)[0] not in ("", "latest")
      and any(volume.get("type") == "bind"
              and volume.get("source") == "/var/run/docker.sock"
              and volume.get("target") == "/var/run/docker.sock"
              and volume.get("read_only") is True
              for volume in proxy.get("volumes", []))
      and not proxy.get("ports")
      and all(str(environment.get(key)) == "1"
              for key in ("CONTAINERS", "IMAGES", "POST", "DELETE"))
      and all(key not in environment or str(environment[key]) == "0" for key in disabled)
      and all(key not in environment or str(environment[key]) == "1"
              for key in ("ALLOW_START", "ALLOW_STOP")))

proxy_networks = set(proxy.get("networks", {}))
runtime_networks = set(runtime.get("networks", {}))
check(5, "docker-proxy has only one network, shared with runtime",
      len(proxy_networks) == 1 and len(proxy_networks & runtime_networks) == 1)
check(6, "docker-proxy prevents privilege escalation and is not privileged",
      "no-new-privileges:true" in proxy.get("security_opt", [])
      and not proxy.get("privileged", False))
print("CHECK-FPN-CONFIG OK")
PY
