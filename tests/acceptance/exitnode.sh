# Sourced by the client scripts that select the exit node: records the exit node this Mac uses
# now (EXIT0, restored by each script's trap). A Mac with no exit node is refused, because
# "restoring" none would leave its traffic going out directly after the test (a leak, not a
# cleanup). ALLOW_NO_EXIT=1 runs anyway and restores none.
EXIT0=$(tailscale status --json | python3 -c 'import json,sys; s=json.load(sys.stdin); print(next((p["TailscaleIPs"][0] for p in (s.get("Peer") or {}).values() if p.get("ExitNode")), ""))')
if [ -z "$EXIT0" ] && [ "${ALLOW_NO_EXIT:-0}" != 1 ]; then
  echo "This Mac uses no exit node, so the test would end by restoring none." >&2
  echo "Select one first (tailscale set --exit-node=${NODE:-exit-proton}), or run with ALLOW_NO_EXIT=1." >&2
  exit 2
fi
