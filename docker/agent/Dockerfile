# The agent's workspace: three repos at their base state, with NO upstream history after the base commit,
# and every dependency for every milestone state already in the image so the task runs with the network off.
# Build context is produced by `container.py build`.
ARG BASE=csf-livekit-base

FROM ${BASE} AS deps
# Manifests of every milestone state: deps/go/<repo>/<tag>/{go.mod,go.sum}, plus the rust-sdks tree at the first and last state.
COPY deps/ /deps/
# Go resolves the combined livekit + protocol graph in workspace mode, so download per milestone state inside a
# workspace (deps/go/<tag>/{livekit,protocol}) and merge the go.work.sum files it writes.
RUN set -e; mkdir -p /out; touch /out/go.work.sum; \
    for d in /deps/go/*/; do \
      (cd "$d" && printf 'go 1.26.0\n\nuse (\n\t./livekit\n\t./protocol\n)\n' > go.work && go mod download \
        && cat go.work.sum >> /out/go.work.sum); \
    done; sort -u /out/go.work.sum -o /out/go.work.sum
RUN cd /deps/rust-base && cargo fetch
RUN cd /deps/rust-final && cargo fetch
# The task asks the agent to write the protocol changes, so the published protocol source must not be in the image.
# (go.work makes the local protocol checkout win; only the .mod/.info files stay for module-graph resolution.)
RUN rm -rf /root/go/pkg/mod/github.com/livekit/protocol@* \
 && find /root/go/pkg/mod/cache/download/github.com/livekit/protocol -name '*.zip*' -delete 2>/dev/null || true

FROM ${BASE}
COPY --from=deps /root/go/pkg/mod /root/go/pkg/mod
COPY --from=deps /usr/local/cargo/registry /usr/local/cargo/registry
COPY --from=deps /usr/local/cargo/git /usr/local/cargo/git
COPY --from=deps /out/go.work.sum /workspace/go.work.sum
ENV GOPROXY=off GOSUMDB=off GOFLAGS=-buildvcs=false CARGO_NET_OFFLINE=true
WORKDIR /workspace
COPY repos/ /workspace/
COPY go.work submit.sh prompt.md TASK_QUEUE.md /workspace/
RUN chmod +x /workspace/submit.sh && mkdir -p /workspace/srs \
 && for r in protocol livekit rust-sdks; do \
      cd /workspace/$r && git init -q -b main && git add -A \
      && git -c user.name=base -c user.email=base@example.com commit -qm "base state" \
      && git tag agent-impl-BASE; \
    done
