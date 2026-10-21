#!/usr/bin/env bash
# Set up and start an exit node from the deploy kit (PLAN Phase D), on a Docker host:
#   1. unpack the kit (exit-vpn-node-<version>.tar.gz) where it should live, e.g. /opt/exit-node
#   2. put the secrets in ./secrets (see README.md) and run: sudo ./install.sh
# Safe to run again: it never overwrites .env or the secrets, only checks them and (re)starts.
set -euo pipefail
cd "$(dirname "$0")"
say() { echo "exit-vpn install: $*"; }
die() { echo "exit-vpn install: $*" >&2; exit 1; }

command -v docker >/dev/null || die "Docker is not installed"
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing"
[ -e /dev/net/tun ] || die "/dev/net/tun is missing (the WireGuard tunnel needs it)"

# Secrets: a directory only root can read
mkdir -p secrets && chmod 700 secrets
for f in proton_a.conf proton_b.conf ts_authkey; do
  [ -s "secrets/$f" ] || die "secrets/$f is missing (README.md, 'Secrets')"
  chmod 600 "secrets/$f"
done
grep -q '^\[Interface\]' secrets/proton_a.conf || die "secrets/proton_a.conf is not a WireGuard config"

# .env: from the example the first time, with the kit's pinned images
if [ ! -f .env ]; then
  cp .env.example .env
  say "created .env from .env.example: set EXIT_OWNERS (your tailnet login), then run again"
  chmod 600 .env
  [ -f images.env ] && { grep -v '^EXIT_IMAGE=\|^PROBER_IMAGE=' .env > .env.tmp; cat images.env >> .env.tmp; mv .env.tmp .env; chmod 600 .env; }
  exit 0
fi
chmod 600 .env
grep -q '^EXIT_OWNERS=.\+' .env || die ".env: EXIT_OWNERS is empty (who may use the controller API)"
grep -q '^EXIT_IMAGE=.*@sha256:[0-9a-f]\{64\}' .env || die ".env: EXIT_IMAGE must be pinned by digest (copy it from images.env)"
grep -q '^PROBER_IMAGE=.*@sha256:[0-9a-f]\{64\}' .env || die ".env: PROBER_IMAGE must be pinned by digest"

say "pulling the images"
docker compose pull
say "starting"
docker compose up -d
for _ in $(seq 60); do
  h=$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q exit)" 2>/dev/null || true)
  [ "$h" = healthy ] && { say "the exit container is healthy"; docker compose ps; exit 0; }
  sleep 5
done
docker compose ps
die "the exit container is not healthy after 5 minutes: docker compose logs exit"
