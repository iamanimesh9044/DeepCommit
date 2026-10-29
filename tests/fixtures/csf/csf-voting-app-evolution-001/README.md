# csf-voting-app-evolution-001

A sample **Continuous Software Factory (CSF)** episode in Harbor format. One agent keeps one
multi-service system running through **four dependent milestones**. Nothing resets between them:
each milestone starts from the code the agent left behind.

## The system

[dockersamples/example-voting-app](https://github.com/dockersamples/example-voting-app) at commit
`63e9150` (Apache-2.0). Five services run as separate containers:

| Service | Stack | Role |
|---|---|---|
| vote | Python, Flask | Takes votes and pushes JSON messages onto the Redis list `votes` |
| worker | C#, .NET 7 | Moves votes from Redis into the Postgres table `votes` |
| result | Node.js, socket.io | Shows live totals |
| redis | Redis 7 | Queue |
| db | Postgres 15 | Storage |

## The episode

| Step | Ticket | Services touched | Depends on |
|---|---|---|---|
| `m1-vote-timestamps` | Add `cast_at` to every vote, end to end, with rollout safety for old messages | vote, worker, db | none |
| `m2-results-api` | `GET /api/results` JSON API with a `since` filter on `cast_at` | result, db | m1 (`cast_at` column) |
| `m3-message-contract` | Versioned message (schema 2), compatible with old and new senders, plus validation | vote, worker | m1 (message format) |
| `m4-db-failover-incident` | Incident: votes lost and services stuck after the database connection drops | worker, result | m2 (API used to confirm recovery), m3 (worker message handling) |

Milestone 4 fixes real bugs in the upstream code. When the database session drops, the worker
loses the vote it just took off the queue and then crashes, and the result service keeps using a
dead connection.

## How it runs (Harbor 0.24 features used)

- **`[[steps]]`:** four steps in one agent environment, so state persists between milestones.
- **`environment/docker-compose.yaml`:** the agent's `main` container plus vote, worker, result,
  redis and db. The services mount the agent's `/app` volume and restart within a few seconds of a
  code change (`environment/services/*/csf-run.sh`). The agent never gets Docker access. Service
  logs are in `/var/log/services/`.
- **Agent-controlled services (`[environment.service_control]`):** the agent restarts, stops and
  starts services and reads their logs with `harbor-svc` (for example `harbor-svc restart worker`).
  App services run in `CSF_RELOAD=manual` mode, so code changes take effect on restart. On vote,
  worker and result, the agent can also run any command inside the container, with that service's
  own runtime, using `harbor-svc exec <service> -- <command>`, for example to call a function it
  just edited. Redis and Postgres allow status, logs and restart. Every call is audited in the trial's
  `service_control.jsonl`. **This needs Harbor with service control:** branch
  `csf/service-control` of [deccanai-org/harbor](https://github.com/deccanai-org/harbor/tree/csf/service-control),
  design in its [`docs/service-control.md`](https://github.com/deccanai-org/harbor/blob/csf/service-control/docs/service-control.md).
  Install it with
  `pip install "git+https://github.com/deccanai-org/harbor.git@csf/service-control"`. Stock Harbor silently ignores
  the `[environment.service_control]` table: the task still runs and grades, but the agent gets
  no `harbor-svc`, and services restart only after a crash.
- **Separate verifier per step:** `steps/<step>/tests/` holds its own Compose stack. Harbor
  rebuilds every service from clean images, copies only the agent's `vote/`, `worker/` and
  `result/` folders in (task-level `artifacts`), and runs the hidden checks. The hidden tests are
  baked into the verifier image and never exist in the agent's environment.
- **Cumulative checks:** step K runs the checks for milestones 1..K, so regressions in earlier work
  are caught and attributed to the milestone that caused them.
- **`reward.json`:** `reward` (strict, 0 or 1), `current_f2p`, `current_p2p`, `prior_pass_rate`,
  `prior_failures`. True regressions (passed earlier, failing now) come from
  `../../_src/csf-voting-app-evolution-001/attribute_regressions.py <trial-dir>`. The trial-level reward is the per-key mean (`multi_step_reward_strategy = "mean"`).
- **Step workdir:** `steps/m4-db-failover-incident/workdir/` drops the incident notes into
  `/app/incidents/` before milestone 4 starts.

## Run it

```bash
# from the repo root
harbor run -p tasks/end-to-end-feature/csf-voting-app-evolution-001 -a oracle -o runs   # reference solutions: expect reward 1 on every step
harbor run -p tasks/end-to-end-feature/csf-voting-app-evolution-001 -a nop    -o runs   # no changes: expect reward 0 on every step
```

## Editing

Don't hand-edit `steps/`. It is generated from [`../../_src/csf-voting-app-evolution-001/`](../../_src/csf-voting-app-evolution-001/)
(tickets, checks, verifier template, gold patches) by `render.py` there. How the task was built and
validated is in [`process.md`](../../_src/csf-voting-app-evolution-001/process.md), and the format is
explained with diagrams in [`task-format.md`](../../_src/csf-voting-app-evolution-001/task-format.md).
