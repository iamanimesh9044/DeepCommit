# Toolchains for the LiveKit multi-repo task: Go 1.26 (protocol, livekit) and Rust 1.97.1 (rust-sdks).
FROM golang:1.26@sha256:672c944cfd07599b5f1bb179869270cdc94c6254e41db4d431259693cb286b82 AS go
FROM rust:1.97.1@sha256:b1b3c9c0d921d7fa0a6d1f9ec7e4eab87f8c8ec97644c3d791450f131dec813f
COPY --from=go /usr/local/go /usr/local/go
ENV PATH=/usr/local/go/bin:/root/go/bin:$PATH \
    GOFLAGS=-buildvcs=false \
    CARGO_TARGET_DIR=/cache/target
# rust-sdks links with lld through clang; protocol codegen and TLS crates need protoc and OpenSSL headers.
RUN apt-get update \
 && apt-get install -y --no-install-recommends protobuf-compiler libssl-dev pkg-config clang lld binutils git ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && git config --global user.name agent \
 && git config --global user.email agent@example.com \
 && mkdir -p /cache/target
