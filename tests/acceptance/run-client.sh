#!/usr/bin/env bash
# Runs the client suites from the Mac, in order, and prints one line per suite.
#   tests/acceptance/run-client.sh                 # all
#   tests/acceptance/run-client.sh egress failover # some
# Suites that need `exitctl proton login` (catalog, keys, secrets) are skipped without it.
set -uo pipefail
here=$(cd "$(dirname "$0")" && pwd)
EXITCTL=${EXITCTL:-$here/../../helper/.venv/bin/exitctl}
all="access egress ipv6 selection preferences failover find catalog keys secrets"
needs_login=" catalog keys secrets "
logged_in=$("$EXITCTL" proton whoami --json 2>/dev/null | python3 -c 'import json,sys; print(bool(json.load(sys.stdin).get("account")))' 2>/dev/null)

results=(); failed=0
for s in ${*:-$all}; do
  [ -x "$here/client/$s.sh" ] || [ -f "$here/client/$s.sh" ] || { echo "no suite '$s' (have: $all)"; exit 2; }
  if [[ "$needs_login" == *" $s "* ]] && [ "$logged_in" != True ]; then results+=("SKIP  $s (exitctl not logged in)"); continue; fi
  echo; echo "### $s"
  out=$(bash "$here/client/$s.sh" 2>&1 | tee /dev/stderr); rc=${PIPESTATUS[0]}
  line=$(echo "$out" | grep '^== ' | tail -1 | sed 's/^== //')
  case $rc in 0) results+=("ok    ${line:-$s}");; 2) results+=("STOP  $s: $(echo "$out" | grep '^STOP' | tail -1)"); failed=1;;
              *) results+=("FAIL  ${line:-$s}"); failed=1;; esac
done
echo; echo "### summary"; printf '%s\n' "${results[@]}"
exit $failed
