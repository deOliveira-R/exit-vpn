# The prober: its own netns and tunnel (key B), curl_cffi and
# Debian's Chromium (--headless=new, unprivileged user). Build context: the repo root.
FROM debian:trixie-slim@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates iproute2 nftables wireguard-tools \
      chromium fonts-liberation fonts-noto-color-emoji \
      python3 python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --uid 1000 --create-home --shell /usr/sbin/nologin browser

COPY prober/requirements.lock /opt/prober/requirements.lock
RUN python3 -m venv /opt/prober/venv \
 && /opt/prober/venv/bin/pip install --no-cache-dir --require-hashes --no-deps \
      -r /opt/prober/requirements.lock
COPY prober/src/ /opt/prober/src/
COPY prober/entrypoint.sh /usr/local/bin/prober-entrypoint

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["/opt/prober/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9000/healthz', timeout=3)"]

# The release (node-vX.Y.Z), reported in /v1/status; "dev" for a local build
ARG VERSION=dev
ENV EXIT_VPN_VERSION=$VERSION

ENTRYPOINT ["/usr/local/bin/prober-entrypoint"]
