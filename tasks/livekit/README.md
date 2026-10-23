# LiveKit multi-repo task (DRAFT)

Repos: livekit/protocol (Go + proto), livekit/livekit (Go server), livekit/rust-sdks (Rust). Window 2026-07-28 to 2026-10-23.

Built with `deepcommit/multirepo/` (link_prs, build_milestones, run_tests, finalize_tests, package_task).

| Milestone | Repos | Graded | New tests (N2P) | P2P |
|---|---|---|---|---|
| X001 CreateIngress return value | protocol, livekit | no (livekit service tests need Docker/Redis) | 0 | 335 |
| X002 TEL-912 transfer response | protocol, livekit | no (no new tests) | 0 | 338 + 17 |
| X004 FlexFEC | protocol, livekit | yes | 1 + 52 | 405 + 385 |
| X005 agent HTTP endpoints | protocol, livekit | yes (oversize, ~10K LoC) | 32 + 169 | 373 + 56 |
| X006 agent threads, Go proto to Rust | protocol, rust-sdks | yes | 4 + 3 | 391 + 32 |
| X007 DD forwarder state | protocol, livekit | yes (1 test) | 0 + 1 | 324 + 109 |

## Not done / known limits
- SRS files are drafts from commit messages. No human check, no agent dry-run.
- Test sets cover only packages and crates touched by the gold commits, not the full suites.
- livekit tests that need Docker/Redis are not runnable in this setup.
- A "no-change" and "gold" check holds by construction (start vs end state runs). There is no agent solve run.
- Cross-repo coupling in the Go server comes only through the `go.mod` protocol pin. An agent working in a shared workspace needs a `replace` to a local protocol checkout; not built.
- Only X006 crosses a language boundary (Go/proto to Rust).
- Milestone edges are weak: file overlap only (4 edges).
