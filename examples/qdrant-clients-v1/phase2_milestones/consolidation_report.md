# Milestone Consolidation Report — qdrant_clients_v1

## Workspace Summary

| Metric | Value |
|--------|-------|
| Repositories | 3 (qdrant, rust-client, qdrant-client) |
| Total commits | 216 |
| DAG layers | 13 |
| DAG edges | 226 |
| Hard cross-repo edges | 75 |
| Soft cross-repo edges | 73 |
| Input seeds | 38 |

## Final Milestone Set

| Metric | Value |
|--------|-------|
| Milestones | 30 |
| Explicit dependencies | 15 |
| Inferred dependencies | 37 |
| Coverage | 100% (216/216) |
| Incomplete chains | 0 |
| Cycles | 0 |

### Milestones by Repository

| Repository | Milestones | Commits |
|------------|-----------|---------|
| qdrant (contract) | M01, M05, M06, M07, M09, M11, M13, M16, M17, M27, M28, M29, M30 | 70 |
| rust-client (SDK) | M02, M03, M10, M14, M18, M20, M25, M36 | 14 |
| qdrant-client (SDK) | M04, M08, M15, M19, M21, M22, M24, M34, M35 | 132 |

### Milestones by Category

| Category | Count | Milestones |
|----------|-------|-----------|
| Feature | 14 | M03, M04, M07, M09, M10, M11, M16, M17, M18, M19, M20, M22 (quality), M25, M29 |
| Breaking | 4 | M05, M08, M13, M14, M21 |
| Quality | 6 | M15, M22, M24, M28, M34, M36 |
| Platform | 4 | M01, M02, M27, M30, M35 |
| Security | 1 | M06 |

### Milestone Size Distribution

| Size bucket | Count | IDs |
|-------------|-------|-----|
| 1 commit | 12 | M01, M02, M06, M08, M10, M11, M14, M16, M20, M21, M25, M36(2) |
| 2-5 commits | 9 | M03(4), M04(3), M05(2), M09(2), M13(2), M15(2), M18(3), M19(3), M24(5) |
| 6-12 commits | 4 | M07(6), M17(12), M28(9), M29(9), M34(12) |
| 13-30 commits | 3 | M27(7), M30(17), M35(24) |
| 30+ commits | 1 | M22(81) |

## Pipeline Execution

### Phase 1: Seed Clustering

38 milestone seeds grouped into 9 batches (G01-G09) by topological proximity and cross-repo edge connectivity:

| Group | Seeds | Theme |
|-------|-------|-------|
| G01 | 3 | Tonic/prost dependency upgrade chain |
| G02 | 4 | TurboQuant + memory placement contract |
| G03 | 4 | Slice filter, deprecated search, keyword prefix |
| G04 | 3 | Quota lifecycle (strict mode → global quotas → SDK removal) |
| G05 | 5 | Serverless client (Rust + Python) |
| G06 | 4 | Substring match + nested indexes + text matching |
| G07 | 5 | BM25 text search + id tracker + formula expressions |
| G08 | 6 | Python client local mode correctness |
| G09 | 4 | SDK v1.19/v1.20 sync mega-commits |

### Phase 2: Sub-Agent Integration

9 sub-agents expanded seed groups into milestone proposals. Key integration decisions:

1. **Mega-sync hub isolation**: Commits 3631300 (rust v1.19 sync) and 589aece (python v1.19 sync) act as hub nodes consuming all qdrant v1.19 features. Initially bundled with qdrant contract milestones, they were split into separate milestones (M25, M26) to prevent path-completeness cascading. M26 was later absorbed into M22 (see Phase 3).

2. **Dense qdrant-client DAG**: The 105+ local-mode python commits touch overlapping files (local_collection.py, payload_filters.py, conversion.py), creating bidirectional dependencies when split by theme. Five thematic milestones (M22, M23, M31, M32, M33) were merged into a single M22 "Correctness Sweep" of 77 commits.

3. **Uncovered commit sweep**: After initial sub-agent integration covered only 67 commits, an automated classification pass categorized 149 remaining commits by repo and subject-line keywords into M27-M30 (qdrant) and M34-M36 (SDK).

### Phase 3: Validation and Fixes (5 iterations)

| Iteration | Coverage | Chains | Cycles | Action |
|-----------|----------|--------|--------|--------|
| 1 | 31% | — | — | Integrate uncovered commits (M27-M36) |
| 2 | 100% | 0 | 0 | Coverage pass; conflicts/partitioning pass |
| 3 | 100% | 44 | 14 | Merge M22+M23+M31+M32+M33 → M22(77) |
| 4 | 100% | 61 | 9 | Absorb M26, M12, partial M15 → M22(81) |
| 5 | 100% | 0 | 0 | **All validations pass** |

#### Key Fixes Applied

**Iteration 3 — qdrant-client sweep cycle (14 cycles)**
- Root cause: M22/M23/M31/M32/M33 formed bidirectional deps through shared files
- Fix: Merged all 5 into single M22 (77 commits)

**Iteration 4 — M22↔M26 cycle (9 cycles, 61 incomplete chains)**
- Root cause: M22 python-client commits straddle mega-sync 589aece (M26) — some precede it, some follow it. Same pattern with M12 (de3c432, 6a6fd05) and 693c6a3 from M15.
- Fix: Absorbed M26 (589aece), M12 (de3c432, 6a6fd05), and 693c6a3 from M15 into M22. Removed M12 and M26 as separate milestones. M22 grew from 77 → 81 commits.

## Dependency Graph

```
Layer 0 (no prerequisites):
  M02, M06, M16, M20, M21, M27, M28, M36

Layer 1 (depends on layer 0):
  M01 ← [via M16 inferred]
  M07 ← M16
  M11 ← M01

Layer 2:
  M05 ← M01
  M13 ← M07
  M17 ← M07, M28

Layer 3:
  M09 ← M17
  M22 ← M05, M06, M11, M13
  M25 ← M02, M05, M13
  M29 ← M05
  M30 ← M07

Layer 4:
  M08 ← M22
  M14 ← M25
  M15 ← M22
  M18 ← M17, M25
  M24 ← M22
  M34 ← M22

Layer 5:
  M03 ← M02, M29, M30
  M04 ← M30
  M10 ← M18
  M19 ← M17, M22, M30, M34, M35
  M35 ← M34
```

## Cross-Repo Dependency Patterns

Three distinct patterns of cross-repo dependency:

1. **Contract → SDK sync** (most common): Qdrant proto changes consumed by SDK regeneration commits.
   - M01→M02 (tonic upgrade)
   - M09→M10 (nested indexes)
   - M11→M22 (substring match)
   - M13→M14, M13→M25 (slice filter/deprecated search)

2. **Contract → SDK mega-sync**: Multiple qdrant features consumed by a single SDK sync commit.
   - M07+M13+M16+M17 → M25 (rust v1.19 sync)
   - M17 → M18 (rust v1.20 sync)
   - M17 → M19 (python v1.20 sync)

3. **SDK intra-repo chain**: SDK sync versions building on each other.
   - M25→M18 (rust v1.19→v1.20)

## Design Decisions

1. **M22 as mega-milestone (81 commits)**: The python-client local-mode subsystem is a single tightly-coupled component where commits share files bidirectionally. Splitting by theme created unavoidable cycles. A single milestone preserves correct dependency semantics at the cost of granularity.

2. **Qdrant-only milestones without cross-repo edges**: M27 (version bumps), M28 (validation), M29 (gRPC protocol), M30 (tooling) are self-contained qdrant milestones with no SDK consumption — they represent internal improvements not exposed through the API contract.

3. **Explicit vs inferred dependencies**: 15 explicit dependencies document cross-repo contract→SDK relationships with hard xrepo edge rationale. 37 total dependencies inferred from DAG structure. Explicit deps are a curated subset focused on the most architecturally significant relationships.

## Output Files

| File | Description |
|------|-------------|
| `milestones.csv` | 30 milestones, 216 commits, 100% coverage |
| `dependency_proposals.csv` | 15 explicit cross-repo dependencies |
| `consolidation_report.md` | This report |
