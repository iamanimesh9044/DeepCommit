# LiveKit multi-repo task (end-to-end, draft)

Repos: livekit/protocol (Go + proto), livekit/livekit (Go server), livekit/rust-sdks (Rust). Window 2026-07-28 to 2026-10-23.
Built with `deepcommit/multirepo/` and `config/multirepo/livekit.yaml`.

## How the task runs
One persistent workspace holds all three repos at their base state. Milestones arrive in `TASK_QUEUE.md` order. The agent edits any repo, then runs `./submit.sh <id>`, which commits and tags every repo (`agent-impl-<id>`). The harness checks out each tag and runs the tests in Docker. A `go.work` file makes the local `protocol` checkout override the version pinned in `livekit/go.mod`. Scoring follows SWE-Milestone: recall = fixed required tests / required, precision = (fixed + 1) / (fixed + broken P2P + 1), score = their harmonic mean, resolved = all required pass and no P2P breaks.

Each repo follows its **real upstream history**. For every milestone, `ctx-<id>` is the state just before its first selected commit (all earlier upstream commits applied as context), and `agent-impl-<id>` is the gold state. Fast-forwarding along real history keeps generated protobuf files valid; cherry-picking a subset did not (it corrupted them).

## Milestones (in trajectory order)
| Order | Milestone | Agent edits | Graded | Required tests | P2P | Context commits applied before |
|---|---|---|---|---|---|---|
| 1 | X002 TEL-912 transfer response | protocol + livekit | no | - | - | protocol 19, livekit 26 |
| 2 | X001 idempotent CreateIngress | livekit | no | - | - | livekit 4 |
| 3 | X005 agent HTTP endpoints | protocol + livekit | yes | 32 + 169 | 230 + 54 | protocol 69, livekit 94 |
| 4 | X007 dependency-descriptor state | protocol + livekit | yes | 1 | 184 + 109 | protocol 8, livekit 24 |
| 5 | X006 agent threads, Rust SDK | rust-sdks | yes | 3 | 32 | none |
| 6 | X004 FlexFEC | livekit | yes | 42 | 156 | livekit 40 |

In X001, X006 and X004 the protocol commit lands inside an earlier milestone's history slice, so it is already in the workspace as context. Those milestones link to protocol but the agent only edits the dependent repo. X005 is the milestone where the agent must change protocol and livekit together (X007 and X002 also touch both, but X007's protocol side has no required tests).

## Environment (Docker)
The task runs in a container with all dependencies baked in, so it needs no network at run time.

| File | What |
|---|---|
| `docker/base/Dockerfile` | Go 1.26 + Rust 1.97.1, both base images pinned by digest, plus build packages (protoc, OpenSSL headers, clang, lld) |
| `docker/agent/Dockerfile` | The agent workspace: protocol, livekit and rust-sdks reduced to one "base state" commit (no later upstream history), Go modules and Cargo crates for **every** milestone state, `go.work`, `go.work.sum`, `submit.sh`, `prompt.md` |
| `harness/ctx/`, `harness/gold/` | Context and gold patches. They stay outside the image: context patches are applied when a milestone unlocks, gold patches are used only by the oracle run |

The published `livekit/protocol` source is deleted from the Go module cache, so the agent writes the protocol changes itself. `GOPROXY=off` and `CARGO_NET_OFFLINE=true` are set in the image.

```bash
uv run --with pyyaml python -m deepcommit.multirepo.container build --config config/multirepo/livekit.yaml --task tasks/livekit --workspace WS --ctx CTX   # needs the trunk workspace from `trunk build`
uv run --with pyyaml python -m deepcommit.multirepo.container run --config config/multirepo/livekit.yaml --task tasks/livekit --mode oracle --offline
uv run --with pyyaml python -m deepcommit.multirepo.container run --config config/multirepo/livekit.yaml --task tasks/livekit --mode nop --offline
```

## Validation (run on this trajectory)
| Run | Result |
|---|---|
| Oracle in the container, network off (gold applied at each milestone) | 4 of 4 graded milestones resolved, score 1.0 |
| No-change in the container, network off (milestone submitted unchanged, gold prefix applied between milestones) | 0 of 4 resolved, recall 0 everywhere |

No real agent has been run on it.

## Files
`manifest.json`, `milestones.csv`, `dependencies.csv`, `trunk.json` (context commits per milestone), `e2e_tests/` (required and P2P test ids per milestone and repo, derived from the trajectory), `srs/` (specs), `prompt.md` (agent instructions), `tests/` (earlier per-milestone isolated runs, superseded by `e2e_tests/`).

Commands (from the DeepCommit root):
```bash
uv run --with pyyaml python -m deepcommit.multirepo.e2e setup --config config/multirepo/livekit.yaml --task tasks/livekit --workspace WS
uv run --with pyyaml python -m deepcommit.multirepo.trunk build --config config/multirepo/livekit.yaml --task tasks/livekit --workspace WS
uv run --with pyyaml python -m deepcommit.multirepo.trunk tests --config config/multirepo/livekit.yaml --task tasks/livekit --workspace WS --out tasks/livekit/e2e_tests
uv run --with pyyaml python -m deepcommit.multirepo.e2e oracle --config config/multirepo/livekit.yaml --task tasks/livekit --workspace WS --tests-dir e2e_tests
```

## Known limits
- **No agent run.** The container flow unlocks milestones by applying the context patch (plain apply, then `git apply --3way` if the agent's tree has diverged). The three-way fallback has never been exercised, because oracle and no-change trees match gold exactly.
- The `trunk build` step still needs the upstream clones to produce the base tags and patches. The image itself needs only the files in `harness/`, `srs/` and `e2e_tests/`, but there is no script yet that builds it without those clones.
- Apt packages in the base image are not version-pinned. The built image is the pin; it has not been published (about 7 GB).
- **A large share of each milestone's start state is context.** X005 begins with 69 protocol and 94 livekit upstream commits already applied. The spec covers only the selected commits.
- Required tests exist only in packages touched by the selected commits. P2P covers only packages changed in the slice, not full suites.
- Index-named subtests (`Test/9/name`) are dropped as unstable. Other flaky tests are not detected; no replicate runs.
- livekit tests that need Docker/Redis don't run here, so X001 has no tests.
- SRS files are drafts from commit messages. No human review, no dry run.
- Milestone dependency edges come from file overlap only, and the harness ignores them (the queue is linear).
- Clone paths in `config/multirepo/livekit.yaml` point to a temporary scratchpad.
