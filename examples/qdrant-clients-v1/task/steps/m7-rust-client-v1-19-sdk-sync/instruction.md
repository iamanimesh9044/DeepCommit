# Sync rust-client to the Qdrant v1.19 API

`/app/rust-client` must be brought in line with the Qdrant v1.19 gRPC contract. No other repository changes. Prior steps already updated dependencies and the other clients.

## Protobuf and generated code

Update the `.proto` files under `proto/` to the v1.19 contract, then regenerate `src/qdrant.rs` with the repo's own proto-generation tooling. Do not hand-edit `src/qdrant.rs`. The new messages and fields are:

- **`Datatype`**: new value `Turbo4 = 4`.
- **New enum `Memory`**: `MemoryUnknown = 0`, `Cold = 1`, `Cached = 2`, `Pinned = 3`.
- **`optional Memory memory` fields**, with these numbers:

| Message | Field number |
|---|---|
| `VectorParams` | 8 |
| `VectorParamsDiff` | 4 |
| `HnswConfigDiff` | 8 |
| `SparseIndexConfig` | 4 |
| `ScalarQuantization` | 4 |
| `ProductQuantization` | 3 |
| `BinaryQuantization` | 4 |
| `TurboQuantization` | 3 |
| `KeywordIndexParams` | 5 |
| `IntegerIndexParams` | 6 |
| `FloatIndexParams` | 4 |
| `GeoIndexParams` | 3 |
| `TextIndexParams` | 11 |
| `BoolIndexParams` | 3 |
| `DatetimeIndexParams` | 4 |
| `UuidIndexParams` | 4 |

- **Deprecation**: mark the old `on_disk` and `always_ram` flags `[deprecated = true]`. This includes `on_disk_payload` on `CreateCollection`, `CollectionParams` and `CollectionParamsDiff`.
- **`PayloadStorageParams`**: new message with `optional Memory memory = 1`. It is exposed as `optional PayloadStorageParams payload` on `CreateCollection` (19), `CollectionParams` (12) and `CollectionParamsDiff` (6).
- **`StrictModeConfig`**: add `optional uint32 max_disk_usage_percent = 22`.
- **`KeywordIndexParams`**: add `optional KeywordPrefixParams prefix = 4`. `KeywordPrefixParams` is an empty message.
- **`StemmingAlgorithm`**: add oneof member `DisabledStemmer disabled = 2`. `DisabledStemmer` is an empty message.
- **`IdfParams`**: new message with `optional Filter corpus = 1`. `SearchParams` gains `optional IdfParams idf = 6`.
- **`SliceCondition`**: new message with `uint32 total = 1` and `uint32 index = 2`. `Condition` gains oneof member `slice = 8`.
- **`Match`**: add oneof member `string prefix = 11`.
- **`time` field**: add `double time` to `CollectionClusterInfoResponse` (7), `UpdateCollectionClusterSetupResponse` (2), `CreateShardKeyResponse` (2) and `DeleteShardKeyResponse` (2).
- **Deprecated RPCs**: mark the deprecated search RPCs as deprecated in the contract and generated output.

Existing code that sets deprecated fields must still compile, so add `#[allow(deprecated)]` where needed.

## Builders (`qdrant_client::qdrant`)

Every builder method below takes `impl Into<i32>` or `Into<Message>` and stores it as the value.

- **`.memory(Memory)`**: add to `VectorParamsBuilder`, `VectorParamsDiffBuilder`, `HnswConfigDiffBuilder`, `SparseIndexConfigBuilder`, `ScalarQuantizationBuilder`, `ProductQuantizationBuilder`, `BinaryQuantizationBuilder`, `TurboQuantizationBuilder`, and the keyword, integer, float, geo, text, bool, datetime and uuid index-params builders.
- **`BinaryQuantizationBuilder`**: must also implement `Default`.
- **New `PayloadStorageParamsBuilder`**: has `new()`, `Default`, `.memory(..)`, `.build()`, and converts into `PayloadStorageParams`.
- **`.payload(impl Into<PayloadStorageParams>)`**: add to `CreateCollectionBuilder` and `CollectionParamsDiffBuilder`.
- **`StrictModeConfigBuilder::max_disk_usage_percent(u32)`**: new method.
- **`KeywordIndexParamsBuilder::prefix(bool)`**: `true` sets `prefix` to `Some(KeywordPrefixParams)`, `false` sets it to `None`.
- **`TextIndexParamsBuilder::disabled_stemmer()`**: sets the stemmer to `StemmingAlgorithm` with `StemmingParams::Disabled(DisabledStemmer {})`.
- **New `IdfParamsBuilder`**: has `new()`, `Default`, `.corpus(impl Into<Filter>)`, `.build()`, and converts into `IdfParams`.
- **`SearchParamsBuilder::idf(impl Into<IdfParams>)`**: new method.

Export the new builders from `builders/mod.rs`. Mark the old `on_disk`, `always_ram` and `on_disk_payload` builder methods as deprecated in their doc comments, e.g. "Deprecated since 1.19.0, use `memory` instead".

## Filters

- **`Condition::matches_prefix(field: impl Into<String>, prefix: impl Into<String>) -> Condition`**: builds a `FieldCondition` whose match is `MatchValue::Prefix`.
- **`Condition::slice(total: u32, index: u32) -> Condition`**: builds a `Condition` from `SliceCondition { total, index }`. In debug builds, assert `total >= 1` and `index < total`.
- **Conversions**: implement `From<SliceCondition>` for both `Condition` and `condition::ConditionOneOf`.
- **Negation**: negating `MatchValue::Prefix` panics with "cannot negate a MatchValue::Prefix, use within must_not clause instead". This matches the existing behaviour of `TextAny`.

## Consistency requirements

Field names, numbers and enum values must match the Qdrant v1.19 contract exactly. The Rust API names must be exactly those above, because the hidden snippet tests and the builder-coverage test call them directly.

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
