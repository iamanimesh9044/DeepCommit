# X004: Upstream FlexFEC support (protocol + livekit server)
DRAFT, generated from gold commit messages. Not human-verified, not dry-run.

## Overview
The media server must recover lost RTP packets using FlexFEC (FEC) protection streams, and report FEC activity in RTP statistics.

## Repos and order
1. **protocol (Go/proto), already in the workspace as context:** RTP statistics message gains FEC counters: FEC packets received, FEC bytes, FEC packets recovered, FEC packets discarded.
2. **livekit server (Go), your work:** depends on the protocol fields above. Adds FlexFEC recovery for uplink streams and fills the new counters.

## Requirements
- FR1: Negotiate FlexFEC from the SDP; FEC stream pairing must handle whitespace and malformed or missing ssrc groups without failing.
- FR2: Recover missing media packets from FEC packets, including out-of-order arrival; complete packets must not allocate recovery state.
- FR3: Validate the FEC payload type range; short TWCC extensions in FEC packets must be tolerated.
- FR4: Release finished FEC state; invoke FEC callbacks without holding locks.
- FR5: Expose the four FEC counters in RTP stats.

## Acceptance
- Tests for SDP FEC pairing (valid, whitespace, malformed, no groups) pass.
- FEC recovery tests pass; existing RTP and SFU tests still pass.
