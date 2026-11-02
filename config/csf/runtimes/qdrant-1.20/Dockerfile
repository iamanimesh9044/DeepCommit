# Runtime image "qdrant-1.20": the Qdrant 1.20.0 server (official static musl release, amd64 and arm64, pinned by sha256)
# plus csf-run and csf-health (added by the exporter). Built by Harbor from this folder only; nothing the agent writes
# feeds this build. Harbor runs it with a read-only root filesystem, the code volume mounted read-only at /app, tmpfs at
# /build and /tmp, and a non-root user, so the service keeps its storage under /tmp (see its csf-service.toml env).
FROM debian:bookworm-slim

# ca-certificates: Qdrant builds an HTTP client at start and panics without them. curl: the download and csf-health.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

ENV HOME=/tmp

RUN set -eu; arch=$(uname -m); \
    case "$arch" in \
      x86_64)  sum=b03a64616a1f41ddab78b480a318466541765f19835318113b1821cbea8df52c ;; \
      aarch64) sum=bd8a458b8ef8006ce2c603c5bd6d1d973e39d787f794008dd959112e747c3bb7 ;; \
      *) echo "no qdrant release for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/qdrant.tgz "https://github.com/qdrant/qdrant/releases/download/v1.20.0/qdrant-$arch-unknown-linux-musl.tar.gz"; \
    echo "$sum  /tmp/qdrant.tgz" | sha256sum -c -; \
    tar -xzf /tmp/qdrant.tgz -C /usr/local/bin qdrant; \
    rm /tmp/qdrant.tgz; \
    chmod 0755 /usr/local/bin/qdrant; \
    qdrant --version

COPY csf-run csf-health /usr/local/bin/
RUN chmod 0755 /usr/local/bin/csf-run /usr/local/bin/csf-health

WORKDIR /build
ENTRYPOINT ["csf-run"]
