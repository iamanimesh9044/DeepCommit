# Phase-1 Skill — Prepare Range Config

You produce a **validated source-boundary config** at `config/<owner>_<repo>_<start>_<end>.yaml`
for one repository version range. This config defines the analysis boundary that **every**
downstream phase (preprocess DAG, milestone construction, env resolution) trusts. Get it wrong and
the whole pipeline drifts silently — too-narrow `repo_src_dirs` drops real product commits; too-wide
pollutes the DAG with docs/benchmarks; incomplete `test_dirs` misclassifies tests as product logic.

Your work is **not** "fill in a YAML". It is: read the prefetch brief, draft the boundary, and run the
**deterministic validation gate** in a tight loop until it is green and you have resolved (or justified)
every warning. The gate classifies the repo through the same `SrcFileFilter` engine the real pipeline
uses, so a green gate means your config behaves identically downstream.

## The only 4 fields you author (+ 1 conditional)

```yaml
repo_src_dirs:        # product source directories (LITERAL path prefixes)
  - <dir>
test_dirs:            # test files/dirs (gitwildmatch globs, root-anchored)
  - "<glob>"
exclude:              # non-source files INSIDE repo_src_dirs to drop (globs)
  - "<glob>"
main_branch: <name>   # the trunk for this range (verify it resolves)
# tag_prefix: "..."   # ONLY if the range tags are simplified (e.g. "259.x" -> "tutanota-release-259.x")
```

`generated_patterns` / `modifiable_test_patterns` are **optional** boundary refinements — the gate accepts
them, but add them only if the repo has generated code that must ship in the build snapshot
(`**/*.pb.go`, `wire_gen.go`) or test files the agent is allowed to modify; most repos need neither.

Author nothing beyond those. Do NOT add `base_image_name`, `build_command`, `language`, `test_framework`
(Phase-3 build/runtime — they live in the workspace, not here) or `struct_field_fixes` (obsolete). The
gate flags *those* (warns on the Phase-3 fields, errors on `struct_field_fixes`).

## The matching contract — the #1 source of silent corruption

These three fields do NOT share matching semantics. The gate is your safety net, but understand it:

- **`repo_src_dirs` = LITERAL path prefixes**, matched with `str.startswith` (then trailing-slash
  normalized). **NOT globs.** Writing `sklearn/*` or `src/**` matches *nothing* (the `*` is literal) —
  the gate makes this a hard error. Use plain dirs: `sklearn`, `crates`, `src`.
- **`test_dirs` / `exclude` = gitwildmatch globs (gitignore syntax), ROOT-anchored.** `tests/**` matches
  only a *root* `tests/` dir. For tests nested in a monorepo you MUST write `**/tests/**` (any level) or
  `crates/*/tests/**` (one level). A root-anchored pattern that misses nested tests is the classic bug —
  the gate's `W_test_looks_like_src` catches it.
- **Cover tests the way the repo's language does (see the per-language matrix).** Tests live in one of two
  shapes: a *separate test tree* → a **directory glob** (Java `**/src/test/**`, Python/Rust `**/tests/**`,
  JS `**/__tests__/**`), which also catches in-tree helpers/fixtures a name pattern misses (`_test_vectors.py`,
  `tests/__init__.py`); and *co-located sibling files* next to the source → a **filename glob** (Go
  `**/*_test.go` — Go has **no** test dir, JS `**/*.test.*`, often Python `**/test_*.py`), which a directory
  glob can't separate. Most repos need both; **Go is filename-only.** The gate flags test-misclassified-as-source
  but NOT source-as-test — so when a filename glob could hit a real source file, verify it doesn't.
- **`exclude` only needs to target files INSIDE `repo_src_dirs`.** Everything outside the src dirs is
  already dropped by the prefix filter; don't list it.
- **`main_branch`**: often `main`, but `master` / `develop` are common (the gate warns if it doesn't
  resolve). Wrong trunk → the range resolves against the wrong lineage.

## Workflow (loop until green)

1. **Read the prefetch brief.** The per-top-level-dir histogram is your primary signal: source lives
   where the `src-ext` counts cluster. Dirs that are mostly `test-like` are test trees; dirs that are
   `examples` / `benchmarks` / `docs` / `tools` / CI are excluded. The changed-files-in-range breakdown
   shows where the *product* churn is.
2. **Draft the 4 fields.** `repo_src_dirs` = the source-heavy dirs (literal). `test_dirs` = test
   patterns, preferring `**/...` for nested trees. `exclude` = non-code inside the src dirs (translations,
   static assets, generated/vendored, lockfiles) — start empty and add only what the gate/histogram shows.
3. **Validate:**
   ```
   python -m deepcommit.preprocess.prepare_range_config.validate \
       --repo <repo_path> --config <candidate.yaml> --end-ref <end-tag> [--json]
   ```
   (`--end-ref` is auto-parsed from a `config/<owner_repo>_<start>_<end>.yaml` filename, but pass it
   explicitly when validating a draft at any other path.)
   Read the report. **Fix every ERROR** (exit 1 = config unusable). **Resolve every WARNING** or
   explicitly justify why it is acceptable:
   - `W_test_looks_like_src` → your `test_dirs` miss nested tests; broaden to `**/tests/**` etc.
   - `W_src_like_outside_boundary` → check the listed top-dirs; if a real source dir is among them, add it
     to `repo_src_dirs`; if they're all benchmarks/tools/examples, it's fine.
   - `W_unrecognised_src_ext` → source files in a language the classifier can't see (`.scala`/`.vue`/C/C++);
     usually unavoidable, just confirm they're genuinely the repo's source.
   - `W_phase3_field` / `E_obsolete_key` → remove the field.
4. **Re-validate until green** (0 errors, warnings resolved/justified). Only then is the config done.
5. Write the final config to `config/<owner>_<repo>_<start>_<end>.yaml`.

## Keep / exclude rubric (for repo_src_dirs + exclude)

**Keep as source** (`repo_src_dirs`): product logic that runs or is compiled/packaged (`src/`, `app/`,
`lib/`, the package dir), plus build *inputs* (Makefile, webpack/vite config, IDL/schema). Generated
*outputs* of an IDL/schema are artifacts — exclude.

**Exclude** (either outside `repo_src_dirs` entirely, or via `exclude` if inside): static assets;
generated/build artifacts/lockfiles; dependency manifests; CI/CD/deploy/containerization; docs/examples/
demos/notebooks; tests/benchmarks/perf; third-party/vendored code; IDE/lint/format config; translations.

**Packaged "sample"/"example" vs an in-tree `examples/` dir.** A separately-packaged, published
sample/example with its own build manifest (`pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod` /
`pom.xml`) + tests (e.g. `packages/foo-sample-plugin`) is **product source — keep it**; its name is
pedagogical but its form ships and is maintained. Only an in-tree `examples/` / `demos/` directory of
*unpackaged* illustrative scripts is excluded.

## Per-language matrix (Python · Go · Rust · Java · TS/JS)

Each repo follows ONE language's convention — inspect its layout and match it. A wrong-language pattern is
usually inert (a `*_test.go` rule finds nothing in a Python repo), but do NOT kitchen-sink rules from other
languages: an over-broad dir glob can wrongly capture real source — notably **singular `**/test/**`**, which
swallows a source package literally named `test/` (e.g. Django's `django/test/` test-*framework* module).
Prefer plural `**/tests/**` and the language-specific patterns below.

**Python** (pytest / unittest)
- `repo_src_dirs`: `src/<pkg>` (src-layout) or the flat package dir (`django`, `sklearn`).
- tests: a `tests/` tree and/or co-located `test_*.py` / `*_test.py`; `conftest.py` = fixtures (test infra).
- `test_dirs`: `**/tests/**`, `**/test_*.py`, `**/*_test.py`, `**/conftest.py`.

**Go** (`go test`)
- `repo_src_dirs`: the package/module dirs (`core`, `rest`, …) or the repo root (`go.mod` marks the module).
- tests: **`*_test.go` co-located in the SAME directory as the source it tests — Go has NO separate
  unit-test directory**; `testdata/` holds fixtures (the toolchain ignores it).
- `test_dirs`: `**/*_test.go`, `**/testdata/**` (+ `test/**` / `e2e/**` only if the repo adds integration there).
- ⚠ `**/tests/**` matches nothing in Go; `**/*_test.go` is the only test signal. Generated `.pb.go` /
  `wire_gen.go` go in Phase-3 `generated_patterns` (not authored here).

**Rust** (`cargo test`)
- `repo_src_dirs`: `crates` (workspace) or `src`; crate code lives at `crates/*/src/`.
- tests: integration in `<crate>/tests/`, benches in `benches/`; **unit tests are inline `#[cfg(test)]`
  inside `.rs` source files** (and doc-tests in `///` comments).
- `test_dirs`: `**/tests/**`, `crates/*/tests/**`, `**/benches/**` (+ `**/*_test.rs` if the project keeps
  test modules in `*_test.rs` files under `src/`, e.g. nushell's `example_test.rs`).
- ⚠ inline `#[cfg(test)]` and doc-tests can't be separated by path (one `.rs` file holds both product and
  test code) — the pipeline splits inline tests at hunk level; file-level treats them as source. Don't try
  to exclude them.

**Java / JVM** (JUnit; Maven / Gradle; Kotlin / Groovy / Scala)
- `repo_src_dirs`: the **module dirs** (`dubbo-common`, `dubbo-rpc`, …) or repo root. Source sits at
  `<module>/src/main/`, tests at `<module>/src/test/` (Android instrumented tests at `<module>/src/androidTest/`)
  — all fall under the module prefix.
- tests: the parallel **`src/test/` / `src/androidTest/` trees** + `*Test.java` / `*Tests.java` / `*IT.java`
  / `*TestCase.java` / `*Test.kt` / `*Spec.groovy`.
- `test_dirs`: `**/src/test/**`, `**/src/androidTest/**`, `**/*Test.java`, `**/*Tests.java`, `**/*IT.java`,
  `**/*Test.kt`, `**/*Spec.groovy` (+ whole test/demo modules, e.g. `dubbo-test/**`, `dubbo-demo/**`).
- ⚠ `repo_src_dirs` is a LITERAL prefix — you canNOT write `**/src/main`; list the module dirs and let the
  `src/test` globs carve out tests. Exclude build output: **`**/target/**` (Maven), `**/build/**` (Gradle)**.
  Kotlin `.kt` is recognized as source; **Groovy `.groovy` / Scala `.scala` *source* is NOT** (classifies as
  unchecked — harmless for Groovy/Scala *tests*, but flag it if a module's product code is in those languages).

**TS / JS** (Jest / Vitest / Mocha; Playwright / Cypress)
- `repo_src_dirs`: `src/`, `packages/*/src/`, or the package dir.
- tests: **co-located `*.test.ts` / `*.spec.ts` next to source**, and/or `__tests__/`, and/or a `test/`
  dir; E2E in `e2e/` / `playwright/` / `cypress/`; snapshots in `__snapshots__/`.
- `test_dirs`: `**/*.test.*`, `**/*.spec.*`, `**/*.cy.*`, `**/__tests__/**`, `**/__snapshots__/**`,
  `**/__mocks__/**`, `test/**`, `playwright/**`, `cypress/**`.
- ⚠ co-located `*.test.tsx` sit beside source → the filename globs are essential (a dir glob alone misses
  them). `__mocks__/` is test infra → keep it in `test_dirs`; `*.stories.*` (Storybook) is neither product
  nor test → `exclude` it (or it's outside the src dir anyway).

**Monorepos & mid-range packages** (any language): for a *src-layout* monorepo prefer the narrow per-package
prefix (`packages/<pkg>/src/<pkg>`, `crates/<c>/src`) so the package-root manifest (`pyproject.toml` /
`package.json` / `Cargo.toml` / `pom.xml`) and README aren't counted as source. A `repo_src_dir` may be
empty at the *start* tag if its package is introduced mid-range — list it anyway so it's covered from its
first commit.
