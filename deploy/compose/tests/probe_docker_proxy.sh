#!/usr/bin/env bash
set -euo pipefail

# Requires Docker, curl, Python 3 and PyYAML. Run explicitly on a local test daemon.
repo_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
cfg="$repo_root/deploy/compose/docker-proxy/haproxy.cfg"
[[ -f "$cfg" ]] || { echo "FAIL: missing $cfg" >&2; exit 1; }
image=$(python3 - "$repo_root/deploy/compose/docker-compose.fpn.yml" <<'PY'
import sys
import yaml

class ComposeLoader(yaml.SafeLoader):
    pass

for tag in ("!override", "!reset"):
    ComposeLoader.add_constructor(tag, lambda loader, node: loader.construct_sequence(node))
with open(sys.argv[1]) as source:
    image = yaml.load(source, Loader=ComposeLoader)["services"]["docker-proxy"]["image"]
if not image.startswith(("tecnativa/docker-socket-proxy:",
                         "ghcr.io/tecnativa/docker-socket-proxy:")) or image.split(":", 1)[1].split("@", 1)[0] in ("", "latest"):
    sys.exit("FAIL: proxy image must have an explicit non-latest tag")
print(image)
PY
)

docker info >/dev/null
probe_name=vexa-proxy-probe
if docker container inspect "$probe_name" >/dev/null 2>&1; then
    echo "FAIL: $probe_name already exists; refusing to touch it" >&2
    exit 1
fi
tmp_dir=$(mktemp -d)
owner="vexa-proxy-probe-$(basename "$tmp_dir")"
proxy_id=
cleanup() {
    local result=$? ids id
    trap - EXIT
    # Labels retain ownership even if a faulty allowlist permits the rename probe.
    if ids=$(docker ps -aq --filter "label=vexa.proxy-probe=$owner"); then
        for id in $ids; do
            if ! docker rm -f "$id" >/dev/null; then
                echo "FAIL: cleanup $id" >&2
                result=1
            fi
        done
    else
        echo "FAIL: cannot enumerate probe containers for cleanup" >&2
        result=1
    fi
    rm -rf "$tmp_dir"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Hardcode the overlay's intended command: haproxy -W -db -f <mounted config>.
# Override the image entrypoint to avoid its section-flag template renderer.
proxy_id=$(docker run -d --rm --label "vexa.proxy-probe=$owner" \
    --read-only --tmpfs /run:mode=0755,size=1m --cap-drop ALL \
    --security-opt no-new-privileges:true \
    -v /var/run/docker.sock:/var/run/docker.sock:ro \
    -v "$cfg:/usr/local/etc/haproxy/haproxy.cfg:ro" \
    -p 127.0.0.1:0:2375 --entrypoint haproxy "$image" \
    -W -db -f /usr/local/etc/haproxy/haproxy.cfg)
port=$(docker port "$proxy_id" 2375/tcp)
base="http://$port"
ready=false
for ((attempt=0; attempt<60; attempt++)); do
    if curl --silent --output /dev/null --max-time 1 -X GET "$base/containers/json"; then
        ready=true
        break
    fi
    sleep 0.5
done
if [[ "$ready" != true ]]; then
    echo 'FAIL: proxy did not answer within readiness deadline' >&2
    docker logs "$proxy_id" >&2
    exit 1
fi

checks=0
failures=0
probe() {
    local expected=$1 method=$2 path=$3 body=${4:-\{\}} status
    local -a args=(--silent --show-error --max-time 120 --output "$tmp_dir/body" --write-out '%{http_code}' -X "$method")
    if [[ "$method" == POST ]]; then
        args+=(-H 'Content-Type: application/json' -d "$body")
    fi
    checks=$((checks + 1))
    if status=$(curl "${args[@]}" "$base$path") && [[ " $expected " == *" $status "* ]]; then
        printf 'PASS: %s %s -> %s\n' "$method" "$path" "$status"
    else
        printf 'FAIL: %s %s -> %s (expected %s)\n' "$method" "$path" "$status" "$expected" >&2
        failures=$((failures + 1))
    fi
}

probe 200 GET /containers/json
probe 200 GET '/containers/json?all=1'
probe 200 GET /v1.45/containers/json
probe 403 GET '/images/json?all=1'
probe 200 POST '/images/create?fromImage=alpine&tag=3.20'
probe 200 GET /images/alpine:3.20/json
probe 201 POST "/containers/create?name=$probe_name" \
    "{\"Image\":\"alpine:3.20\",\"Cmd\":[\"true\"],\"Labels\":{\"vexa.proxy-probe\":\"$owner\"}}"
probe 200 GET "/containers/$probe_name/json"

for path in /volumes /networks /info /version /_ping '/events?since=0&until=1' /images/json; do
    probe 403 GET "$path"
done
probe 403 POST "/containers/$probe_name/exec" '{"Cmd":["true"]}'
probe 403 GET "/containers/$probe_name/logs?stdout=1"
probe 403 GET "/containers/$probe_name/archive?path=/"
probe 403 PUT "/containers/$probe_name/archive?path=/tmp"
probe 403 POST "/containers/$probe_name/attach"
probe 403 POST "/containers/$probe_name/update" '{}'
probe 403 POST "/containers/$probe_name/rename?name=x"
probe 403 POST "/commit?container=$probe_name"
probe 403 POST /build
probe 403 GET "/containers/$probe_name/top"
probe 403 POST "/containers/$probe_name/restart"
probe 403 GET /exec/abc/json
probe 403 POST /images/alpine:3.20/push
probe 403 DELETE /images/alpine:3.20
probe 403 POST "/v1.45/containers/$probe_name/exec" '{"Cmd":["true"]}'

probe 204 POST "/containers/$probe_name/start"
probe '204 409' POST "/containers/$probe_name/kill"
probe '204 304' POST "/containers/$probe_name/stop?t=1"
probe 204 DELETE "/containers/$probe_name?force=true"
if ((failures)); then
    printf 'PROBE FAILED (%d/%d checks failed)\n' "$failures" "$checks" >&2
    exit 1
fi
printf 'PROBE OK (%d checks)\n' "$checks"
