# X001: Idempotent CreateIngress on SDK retry (livekit server; protocol change already in trunk)
DRAFT, generated from gold commit messages. Not human-verified, not dry-run. Non-graded context milestone.

## Overview
Ingress creation must be safe to retry. The internal ingress RPC returns the created ingress info instead of an empty reply, and the server derives resource ids from the request id so a retried request returns the same record.

## Requirements
- FR1: The io service returns the created ingress info from the create call (protocol: `CreateIngress` with a return value replaces `CreateIngress2`).
- FR2: Resource ids are derived from the request id, so a retried request does not create a second resource.
- FR3: On retry, if an ingress with the same id exists, return the existing record. The stream key must not be regenerated.

## Acceptance
- The server compiles against the updated protocol; existing tests pass.
