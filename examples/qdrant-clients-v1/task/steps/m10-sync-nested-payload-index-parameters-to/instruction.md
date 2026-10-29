# Nested payload index parameter for all payload index types in rust-client

Qdrant's payload index parameters now accept a `nested` option. It indexes each object of an array as its own entry, for example `offers` for the field `offers[].price`. The contract and the Rust SDK must stay consistent on this option.

## Repository

`/app/rust-client`: the Rust SDK. It vendors the proto contract in `proto/` and the generated gRPC types in `src/qdrant.rs`.

## Contract change (`proto/collections.proto`)

Add `optional string nested` to each index-params message. Field numbers:

| Message | `nested` tag |
|---|---|
| `KeywordIndexParams` | 6 |
| `IntegerIndexParams` | 7 |
| `FloatIndexParams` | 5 |
| `GeoIndexParams` | 4 |
| `TextIndexParams` | 13 |
| `BoolIndexParams` | 4 |
| `DatetimeIndexParams` | 5 |
| `UuidIndexParams` | 5 |

Give each field a doc comment:
- **Meaning:** the option indexes every object of the array as a separate entry, e.g. `offers` for the field `offers[].price`.
- **Constraint:** the field must be inside the array's elements.
- **`is_empty` behaviour:** `is_empty` on the field matches a point when one of its elements lacks the field, and never a point without elements.
- **Default:** disabled.
- **Text index only:** also say that every query on the field, not only nested filters, treats each element as its own document. A condition matches a point when a single element matches it, and BM25 scores elements separately.

Two comment updates go with it:
- `TextIndexParams.scoring`: drop the sentence "Implies `phrase_matching: true`." Keep "Changing it rebuilds the index." and "Default: disabled."
- `HnswConfigDiff`: document that `m = 0` disables the main HNSW graph. Payload-aware links may still be built when the effective `payload_m` is positive. `m = 0` alone does not guarantee exact search. For `payload_m`, say it defaults to `m` when unset, an effective 0 disables the extra links, and a positive value allows them for indexed payload fields with HNSW enabled even when `m` is 0.

## Generated code (`src/qdrant.rs`)

Regenerate the file from the proto with the repository's own proto codegen tooling. Do not hand-edit it.

After regeneration:
- Each of the eight structs has `pub nested: Option<String>` with prost tag as above.
- `KeywordIndexParams`, `IntegerIndexParams`, `FloatIndexParams`, `GeoIndexParams`, `BoolIndexParams`, `DatetimeIndexParams` and `UuidIndexParams` are no longer `Copy`, because they now hold a `String`. They still derive `Clone`, `PartialEq`, `Eq` and `Hash`.
- Fix any code in the crate that relied on these types being `Copy`.

## Builders (`src/builders/`)

Each of these builders gets a `nested` setter:
- `KeywordIndexParamsBuilder`
- `IntegerIndexParamsBuilder`
- `FloatIndexParamsBuilder`
- `GeoIndexParamsBuilder`
- `TextIndexParamsBuilder`
- `BoolIndexParamsBuilder`
- `DatetimeIndexParamsBuilder`
- `UuidIndexParamsBuilder`

The setter has this signature:

```rust
pub fn nested(self, value: impl Into<String>) -> Self
```

Behaviour:
- Calling it sets `nested` to `Some(value)` on the built params.
- If it is never called, the built params have `nested == None`.
- Each builder's `build()` output, `Default` and `new`/empty construction include the new field. Existing setters keep working unchanged.
- Add a doc comment to each setter that mentions the `offers` / `offers[].price` example and the "field must be inside the array's elements" rule. The text builder's example uses `offers[].title`.
- Remove the "Implies `phrase_matching: true`" sentence from the doc comments of `TextIndexParamsBuilder::scoring` and `bm25_scoring`.

## Expected behaviour

The crate must compile. Building each index-params type with `.nested("offers")` and passing it through `CreateFieldIndexCollectionBuilder` / `PayloadIndexParams` must yield params with `nested == Some("offers".to_string())`. A documentation snippet that creates a nested-field index with the builders must compile and run. It is exercised by the `test_create_nested_field_index` snippet test.

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
