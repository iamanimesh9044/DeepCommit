# Where the artifacts are

| What | Where | Size |
| --- | --- | --- |
| Everything in this folder (stage outputs, export report, measurements, graph, viewer, gate results, task without large files) | this repository | 7 MB |
| The complete task, ready for `harbor run` (`csf-qdrant-clients-v1-001.zip`, with `environment/vendor/` and all hidden tests) | bucket, see below | 69 MB |

sha256 of the zip: `3d1170cfb3676963a377f77758b55a089b7bbf8147ffadfb355c4310effa3f9f`

Bucket location: _pending upload_

Use the zip:

```bash
unzip csf-qdrant-clients-v1-001.zip
harbor run -p csf-qdrant-clients-v1-001 -a oracle     # Harbor >= 0.25; every step scores 1
harbor run -p csf-qdrant-clients-v1-001 -a nop        # every step scores 0
```

Browse without running anything: open `export/task_viewer.html` (instruction, gold code and hidden tests per step) and
`export/graph.html` (commit and milestone graph) in a browser.
