# Upgrade rust-client to tonic 0.14 / prost 0.14 / reqwest 0.13 and regenerate gRPC stubs

The Qdrant Rust client still builds against the tonic 0.12 / prost 0.13 / reqwest 0.12 generation of crates. Move it to the current generation and regenerate the gRPC stubs with the matching codegen.

## Repository

Only `/app/rust-client` changes. The `.proto` files under `proto/` are not edited. The generated `src/qdrant.rs` must be regenerated from them.

## Dependency changes (`Cargo.toml`)

Runtime `[dependencies]`:

| Crate | Required |
|---|---|
| `tonic` | `0.14.6`, features `tls-ring`, `tls-native-roots`, `gzip` (replacing `tls`, `tls-roots`) |
| `tonic-prost` | new, `0.14.6` |
| `prost`, `prost-types` | `0.14.3` |
| `reqwest` | `0.13.4`, optional, `default-features = false`, features `stream`, `rustls`, `http2` (`rustls-tls` is renamed `rustls`) |
| `thiserror` | `2.0.18` |

- The unused `futures` dependency is dropped.
- Other crates (`anyhow`, `serde`, `serde_json`, `tokio`, `futures-util`, `semver`, `uuid`) may be bumped to current patch versions. Keep their features and optionality unchanged.

`[dev-dependencies]`:
- Replace `tonic-build` (with the `prost` feature) with `tonic-prost-build = { version = "0.14.6" }`.
- The `protos` test target calls `tonic_prost_build::configure()`. It uses `.field_attribute(...)`, `.out_dir(...)` and `.compile_protos(...)`, and imports `tonic_prost_build::Builder`. That crate must be available as a dev-dependency.

## Stub regeneration

The `protos` test target regenerates `src/qdrant.rs` with `tonic_prost_build` when the proto files or the test file are newer than the output.

- Regenerate `src/qdrant.rs` with the repository's own mechanism: run that test, `cargo test --test protos`. Do not hand-edit the generated file.
- Make sure `src/qdrant.rs` is newer than every file in `proto/` and than the source file of the `protos` test target. Otherwise the test exits early without regenerating.
- After regeneration the file must still end with the custom re-exports and `pub use prost_types::Timestamp;`, which the test appends.

Observable consequences of the new codegen:
- Generated clients and servers use `tonic_prost::ProstCodec` and `tonic::body::Body` instead of `tonic::codec::ProstCodec` and `tonic::body::BoxBody`.
- Message types whose fields allow it now derive `Eq` and `Hash` in addition to `Clone` and `PartialEq`. This includes `PointId` and its oneof `PointIdOptions`.

## Hand-written code

- Remove the manual `impl Hash for PointId` in `src/grpc_conversions/extensions.rs`. It now conflicts with the derived implementation.
- `PointId` must remain usable as a `HashMap` or `HashSet` key. Equal ids must hash equally.
- Other hand-written code that depended on the old APIs must be adapted so that the crate compiles. This covers tonic 0.14 body and TLS types, reqwest 0.13, and thiserror 2.
- Public behaviour must not change.
- The crate must build with the default features and with `--no-default-features`.

## Acceptance

- `cargo build` and `cargo test --test protos` succeed.
- `cargo test --test protos` compiles the `protos` test target against `tonic-prost-build` and passes.
- The existing unit tests still pass.

---

## Environment

Every repository below is a separate git checkout at the state this milestone starts from. Your changes from earlier milestones are still in place.

| Path | Upstream | Role | Tests |
| --- | --- | --- | --- |
| `/app/qdrant` | qdrant/qdrant | contract | none: contract files only |
| `/app/rust-client` | qdrant/rust-client | sdk | cargo test |
| `/app/qdrant-client` | qdrant/qdrant-client | sdk | pytest |

- There is no network access: do not try to fetch dependencies or the upstream repositories.
- Your work is graded by hidden tests that run in every repository. A change made in only one repository is not enough when the milestone spans several.
- Do not add or edit tests under the repositories' test directories; hidden tests replace them.
- `qdrant` is the contract repository: the other repositories consume what it defines.
- `/app/qdrant` is a sparse checkout: it holds only the contract files, there is nothing to build or test there. Edit the contract files in place.
