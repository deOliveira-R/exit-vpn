# The exit node: WireGuard to the VPN provider, the kill switch, tailscaled and the
# controller, supervised by s6-overlay. Build context: the repo root (.dockerignore).
# Downloads are verified with sha256sum (the same file builds on the host and in CI).
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

ARG S6_VERSION=3.2.3.2
ARG S6_SHA256_NOARCH=5379750ed30a84bbd2e2dd74847ba6b5bd29cd0b2e3ea2ec58049b57eb2eda12
ARG S6_SHA256_AARCH64=b17f17a82e7a515c682a91edaf2ffdabb73f891981b6c1fd712115693a2f8b4c
ARG S6_SHA256_X86_64=e6befcc96a437a3831386ecfc51808c5d3e939dc5fe3c02ae9284599e8aa2408
ARG TS_VERSION=1.104.2
ARG TS_SHA256_ARM64=5776aaa901b5ef1916e66be5bb54b9f481cd399c9c28db123a8cab620efa237b
ARG TS_SHA256_AMD64=512f613ed7d04fc7282f7dc5bd76aa105aae28168fbb9357eb749ebcbc29f0ae

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils \
      iproute2 nftables conntrack wireguard-tools iputils-ping bind9-dnsutils procps \
      python3 python3-venv \
 && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    arch=$(dpkg --print-architecture); \
    case "$arch" in \
      arm64) s6_arch=aarch64; s6_sum=$S6_SHA256_AARCH64; ts_sum=$TS_SHA256_ARM64 ;; \
      amd64) s6_arch=x86_64;  s6_sum=$S6_SHA256_X86_64;  ts_sum=$TS_SHA256_AMD64 ;; \
      *) echo "unsupported architecture $arch" >&2; exit 1 ;; \
    esac; \
    cd /tmp; \
    base=https://github.com/just-containers/s6-overlay/releases/download/v$S6_VERSION; \
    curl -fsSLO "$base/s6-overlay-noarch.tar.xz"; \
    curl -fsSLO "$base/s6-overlay-$s6_arch.tar.xz"; \
    curl -fsSLO "https://pkgs.tailscale.com/stable/tailscale_${TS_VERSION}_$arch.tgz"; \
    printf '%s  %s\n' \
      "$S6_SHA256_NOARCH" s6-overlay-noarch.tar.xz \
      "$s6_sum" "s6-overlay-$s6_arch.tar.xz" \
      "$ts_sum" "tailscale_${TS_VERSION}_$arch.tgz" | sha256sum -c -; \
    tar -C / -Jxpf s6-overlay-noarch.tar.xz; \
    tar -C / -Jxpf "s6-overlay-$s6_arch.tar.xz"; \
    tar -xzf "tailscale_${TS_VERSION}_$arch.tgz"; \
    install -m 755 "tailscale_${TS_VERSION}_$arch/tailscale" "tailscale_${TS_VERSION}_$arch/tailscaled" /usr/local/bin/; \
    rm -rf /tmp/*

# Controller: dependencies pinned by hash; the package itself runs from source
COPY controller/requirements.lock /opt/exit-vpn/requirements.lock
RUN python3 -m venv /opt/exit-vpn/venv \
 && /opt/exit-vpn/venv/bin/pip install --no-cache-dir --require-hashes --no-deps \
      -r /opt/exit-vpn/requirements.lock
COPY controller/src/ /opt/exit-vpn/src/

COPY exit/rootfs/ /

# Fail closed: if any init step fails, stop the container (compose restarts it) rather
# than run half-configured. Init steps have no time limit (s6 default is 5 s).
ENV S6_BEHAVIOUR_IF_STAGE2_FAILS=2 \
    S6_CMD_WAIT_FOR_SERVICES_MAXTIME=0 \
    S6_READ_ONLY_ROOT=1 \
    S6_VERBOSITY=1

HEALTHCHECK --interval=30s --timeout=15s --start-period=60s --retries=3 \
  CMD ["/usr/local/lib/exit-vpn/healthcheck"]

# The release (node-vX.Y.Z), reported in /v1/status; "dev" for a local build
ARG VERSION=dev
ENV EXIT_VPN_VERSION=$VERSION

ENTRYPOINT ["/init"]
