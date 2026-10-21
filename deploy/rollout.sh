#!/usr/bin/env bash
# Update an existing exit node to a published release, from the Mac:
#   deploy/rollout.sh 1.0.0
# 1. reads the release's pinned image digests (images.env in its deploy kit),
# 2. verifies both images' build provenance with `gh attestation verify` (built by this
#    repo's node-release workflow from the tagged commit), and refuses if either fails,
# 3. on the host: writes the digests into .env, pulls, restarts, and waits until the
#    controller reports the new version and a healthy exit.
# Host access as in CLAUDE.md: VPS_SSH (ssh alias), VPS_TS_NAME (tailnet name), NODE_DIR.
set -euo pipefail
v=${1:?usage: rollout.sh <version, e.g. 1.0.0>}
repo=${GITHUB_REPOSITORY:-deOliveira-R/exit-vpn}
VPS=${VPS_SSH:-fin-intel}; VPS_TS_NAME=${VPS_TS_NAME:-fin-intel}; NODE_DIR=${NODE_DIR:-/opt/exit-node}
NODE=${EXIT_NODE_NAME:-exit-proton}
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT

gh release download "node-v$v" -R "$repo" -p "exit-vpn-node-$v.tar.gz" -D "$work"
tar -C "$work" -xzf "$work/exit-vpn-node-$v.tar.gz"
images="$work/exit-vpn-node-$v/images.env"
exit_ref=$(sed -n 's/^EXIT_IMAGE=//p' "$images"); prober_ref=$(sed -n 's/^PROBER_IMAGE=//p' "$images")
for ref in "$exit_ref" "$prober_ref"; do
  echo "verifying $ref"
  gh attestation verify "oci://$ref" --repo "$repo" >/dev/null || { echo "provenance check FAILED for $ref" >&2; exit 1; }
done

vps() { ssh -o HostName="$(tailscale ip -4 "$VPS_TS_NAME")" \
            -o HostKeyAlias="$(ssh -G "$VPS" | awk '$1=="hostname"{print $2}')" "$VPS" "$@"; }
vps "cd $NODE_DIR && sudo sed -i -e 's#^EXIT_IMAGE=.*#EXIT_IMAGE=$exit_ref#' -e 's#^PROBER_IMAGE=.*#PROBER_IMAGE=$prober_ref#' .env \
     && (sudo grep -q '^EXIT_IMAGE=' .env || echo 'EXIT_IMAGE=$exit_ref' | sudo tee -a .env >/dev/null) \
     && (sudo grep -q '^PROBER_IMAGE=' .env || echo 'PROBER_IMAGE=$prober_ref' | sudo tee -a .env >/dev/null) \
     && sudo docker compose pull -q && sudo docker compose up -d"
echo "waiting for node-v$v to report healthy"
for _ in $(seq 60); do
  st=$(curl -s -m5 "http://$NODE:${API_PORT:-8080}/v1/status" || true)
  if echo "$st" | python3 -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d.get('version')=='node-v$v' and d.get('health')=='healthy' else 1)" 2>/dev/null; then
    echo "node-v$v is live and healthy"; exit 0
  fi
  sleep 5
done
echo "node-v$v did not report healthy within 5 minutes (sudo docker compose logs exit)" >&2
exit 1
