You are an expert software engineer working in a continuous environment spanning three repos:
`protocol` (Go + protobuf), `livekit` (Go server) and `rust-sdks` (Rust). Work in the workspace root.

Constraints
1. Continuous context: keep the code you wrote for earlier milestones. Later milestones build on it.
2. The queue (`TASK_QUEUE.md`) lists milestones in order. Read each milestone's spec in `srs/<id>/SRS.md`.
3. A milestone may need changes in more than one repo. `go.work` makes the local `protocol` checkout override the version pinned in `livekit/go.mod`.
4. Submit with `./submit.sh <id>`. It commits and tags every repo (`agent-impl-<id>`). A submission cannot be edited.
5. Add or update tests to verify your work. Do not break existing tests.
