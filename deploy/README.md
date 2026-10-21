# Deploying an exit node

An exit node is two containers, `exit` and `prober`, from published images (GHCR, amd64 and
arm64, built by this repo's `node-release` workflow with signed build provenance). A host
needs Docker with the Compose plugin and `/dev/net/tun`; nothing is built on it. How the
node works: [ARCHITECTURE.md](../ARCHITECTURE.md).

## New host

1. **Get the kit** from a release named `node-vX.Y.Z` (not "latest"; that one is the app):

       gh release download node-v1.0.0 -R deOliveira-R/exit-vpn -p 'exit-vpn-node-*.tar.gz'
       sudo mkdir -p /opt/exit-node && sudo tar -C /opt/exit-node --strip-components 1 -xzf exit-vpn-node-1.0.0.tar.gz

   It holds:
   - `compose.yaml`;
   - `.env.example`;
   - `install.sh`;
   - this README;
   - `images.env`: the two images, pinned by digest.

2. **Verify the images** (on any machine with `gh`). This proves they were built by this
   repo's workflow from the tagged commit:

       gh attestation verify oci://<EXIT_IMAGE from images.env> --repo deOliveira-R/exit-vpn
       gh attestation verify oci://<PROBER_IMAGE from images.env> --repo deOliveira-R/exit-vpn

3. **Secrets** in `/opt/exit-node/secrets/` (the script makes the folder 0700 and the files 0600):
   - `proton_a.conf`: the exit's WireGuard config (slot a). Make it with `exitctl proton rotate --slot a` once the node runs; for the first start, use a config from the provider's dashboard.
   - `proton_b.conf`: the prober's config (slot b), a second key.
   - `ts_authkey`: a Tailscale auth key, tagged `tag:exit`, used once for the first login.

4. **Settings:** run `sudo ./install.sh` once. It creates `.env` (0600) from `.env.example`,
   with the pinned images. Set `EXIT_OWNERS` to your tailnet login (who may use the controller
   API), and check `EXIT_NODE_NAME`, `EXITNET_SUBNET` and `TS_PORT` against the host's other
   containers.

5. **Start:** `sudo ./install.sh` again. It checks the secrets and settings, pulls the
   images, starts the stack and waits until the exit container is healthy.

6. **Tailnet policy:** the node needs these entries (adapt the names):

   ```jsonc
   "tagOwners":     { "tag:exit": ["autogroup:admin"] },
   "autoApprovers": { "exitNode": ["tag:exit"] },
   "grants": [
     // your devices may use exit nodes, and reach the controller API (and ping)
     {"src": ["autogroup:member"], "dst": ["autogroup:internet"], "ip": ["*"]},
     {"src": ["autogroup:member"], "dst": ["tag:exit"], "ip": ["icmp:*", "tcp:8080"]}
     // no rule with src tag:exit: a compromised exit node reaches nothing on the tailnet
   ]
   ```

7. **From the Mac:** install exitctl (`helper/README.md`), `exitctl proton login`, then
   `exitctl proton rotate --slot a` and `--slot b` (fresh keys made and recorded by exitctl),
   and `exitctl proton servers --push`. Point ExitControl at `http://<EXIT_NODE_NAME>:8080`.

## Updates

From the Mac: `deploy/rollout.sh 1.1.0`. It downloads that release's pinned digests and
verifies both images' provenance (it stops if either fails). On the host it writes the
digests into `.env`, pulls, restarts, and waits until `/v1/status` reports `node-v1.1.0` and a
healthy exit. Volumes (keys, state, the Tailscale identity) carry over; a restart takes
about a minute, during which exit traffic fails closed.

Back out the same way: `deploy/rollout.sh <previous version>`.

## Building from source instead

    docker compose -f compose.yaml -f compose.build.yaml up -d --build

The status then reports version `dev`.

## Versions

Node releases are `node-vX.Y.Z`, separate from the app's `vX.Y.Z`. `/v1/status` reports the
node's `version` and the controller `api` version; the app works with any node of the same
`api` version.
