# X006: Agent threads and database clients (protocol Go to Rust SDK)
DRAFT, generated from gold commit messages. Not human-verified, not dry-run.

## Overview
Agents hold durable conversations ("threads") with people. The protocol repo defines the thread data model and auth grant; the Rust SDK consumes it and exposes clients. This milestone crosses a language boundary (Go/proto to Rust).

## Repos and order
1. **protocol (Go/proto):** thread messages (the durable conversation), a thread grant carried in access tokens, new id prefixes for threads and their items; the agent-db service moves from Twirp to internal gRPC (its Twirp service is removed).
2. **rust-sdks (Rust):** the Rust protocol crate exposes the agent package; the token crate supports agent and thread grants; the API crate gains thread and database service clients. The Rust protocol submodule pin moves to a protocol commit that includes step 1.

## Requirements
- FR1 (protocol): thread grant round-trips through access tokens (encode and decode).
- FR2 (rust token): agent grant claim names serialize as the protocol defines them.
- FR3 (rust api): thread client exposes get-thread and related requests; every thread action can be granted to key-pair tokens.
- FR4 (rust api): public API base URL can be derived from a project URL.

## Acceptance
- Rust tests for the API URL derivation, agent grant claim names and the thread request pass.
- Existing protocol, token and API tests still pass.
