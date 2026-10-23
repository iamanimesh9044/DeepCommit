# X002: SIP transfer returns a response with reason and status (protocol + livekit server)
DRAFT, generated from gold commit messages. Not human-verified, not dry-run. Non-graded context milestone.

## Overview
`TransferSIPParticipant` currently returns an empty reply. It should return a response carrying a reason and a status.

## Requirements
- FR1 (protocol): add a transfer response message with a reason and a status; add an internal variant of the transfer RPC.
- FR2 (livekit): the SIP transfer call returns the new response type; the ingress create call returns its response type instead of an empty one.

## Acceptance
- The server compiles against the updated protocol; existing tests pass.
