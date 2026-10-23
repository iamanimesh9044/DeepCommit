# X007: Keep dependency-descriptor frame numbers across migration (protocol + livekit server)
DRAFT, generated from gold commit messages. Not human-verified, not dry-run.

## Overview
When a subscriber migrates between nodes, the forwarder state must carry the last dependency-descriptor frame number sent, so the first key frame after migration continues the numbering instead of being dropped as older than the subscriber's last structure.

## Repos and order
1. **protocol (Go/proto):** the RTP forwarder state message carries a dependency-descriptor forwarder state (last extended frame number).
2. **livekit server (Go):** saves the state on migration and restores it on the new node; a pending descriptor seed is passed on when migrating again.

## Requirements
- FR1: Forwarder state round-trips the descriptor frame number.
- FR2: After migration, frame numbers continue from the saved value.
- FR3: Migrating a second time before the seed is used preserves the seed.

## Acceptance
- The dependency-descriptor forwarder state test passes; the existing video layer selector tests still pass.
