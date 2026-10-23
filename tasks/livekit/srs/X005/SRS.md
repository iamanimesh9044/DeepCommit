# X005: Agent HTTP endpoints data plane (protocol + livekit server)
DRAFT, generated from gold commit messages. Not human-verified, not dry-run.

## Overview
Agent workers can declare HTTP routes (for example FastAPI routes) that the server serves at `/agents/{deployment}/{path}` without the worker listening on a port. Workers dial the server; the server forwards each HTTP exchange over multiplexed streams.

## Repos and order
1. **protocol (Go/proto):** agent protocol additions: an HTTP-over-stream frame model (attach handshake, opaque HTTP/1.1 data, end and reset, two-level credit), worker registration carries a route manifest, an instance id and an endpoint protocol, the response carries negotiated endpoint settings, worker status carries draining and sequence, a GoAway message. Deployment and agent-name validation uses a URL-safe character set. Access-token grants gain an agent-endpoint permission.
2. **livekit server (Go):** implements the data plane against the new protocol.

## Requirements
- FR1: Route requests by manifest (exact path matching, per-endpoint public access). Return typed 401, 404, 405 as appropriate.
- FR2: Multiplex HTTP exchanges on a pool of worker connections with credit-based flow control and a prioritised write scheduler; fence stale attaches by epoch.
- FR3: Pass through SSE and WebSocket traffic; retry per a defined retry table.
- FR4: The auth middleware reads `access_token` from the query string only (must not consume proxied request bodies).
- FR5: A hook lets a multi-node deployment adopt attaches that land on another node; a wrong-epoch attach returns a distinct error from a rejected attach.
- FR6: Agent names and deployment names are validated as single URL path segments.

## Acceptance
- Endpoint registry, router, manifest, front-end, credit and large-body tests pass.
- Existing agent and auth tests still pass.
