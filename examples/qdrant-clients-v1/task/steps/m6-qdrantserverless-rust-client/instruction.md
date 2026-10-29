# Add a `qdrant_client::serverless` module: QdrantServerless client and gRPC bindings

Repository to change: `/app/rust-client`. Nothing else changes.

Add an experimental client for the managed Qdrant Serverless offering. Mark the module, client and models "in development, do not use yet" in their docs. The offering has simplified, tenant-facing collection management and the same point-level API as a regular cluster.

## Proto and generated stubs

- Add `proto/serverless_collections.proto`, with `package qdrant.serverless;` and a `CollectionsService` that has four RPCs:
  - `CreateCollection`
  - `DeleteCollection`
  - `GetCollection`
  - `ListCollections`
- Messages and enums:
  - Enums: `Distance` (UNSPECIFIED, COSINE, EUCLID, DOT, MANHATTAN), `PrecisionTier` (UNSPECIFIED, LOW, MEDIUM, HIGH, FULL=4), `Tokenizer` (UNSPECIFIED, PREFIX, WHITESPACE, WORD, MULTILINGUAL).
  - Vector configs: `DenseVectorConfig` (`size` u64, `distance`, `multivector`, optional `precision_tier`) and `SparseVectorConfig` (`use_idf`, optional `precision_tier`).
  - Index messages: `KeywordIndex` (optional `KeywordPrefixParams prefix`), `IntegerIndex` (optional bools `lookup` and `range`), `FloatIndex`, `UuidIndex`, `DatetimeIndex`, `GeoIndex`, `BoolIndex`, `TextIndex`, and the `PayloadIndexConfig` oneof over all eight kinds.
  - Text-index helpers: `StopwordsSet`, `SnowballParams`, `DisabledStemmer`, `StemmingAlgorithm` (a oneof of snowball or disabled).
  - `CollectionConfig`: maps `dense_vectors`, `sparse_vectors` and `payload_indexes`.
  - Requests and responses:
    - Create and delete responses and the get response each carry `double time`.
    - Create: `collection_name`, `config`; response has `collection_name` and `result`.
    - Delete: response has `deleted`.
    - Get: response has `exists`, optional `config`, optional `point_count`.
    - List: request has optional `limit` (u32) and `offset_token`.
    - `CollectionSummary`: `collection_name` and optional `point_count`.
    - `ListCollectionsResponse`: `collections`, optional `next_offset_token`, and `time`.
- Use upstream `qdrant-cloud-public-api` `qdrant/serverless/collections.proto` as the source. Strip the `buf.validate` import and options, because they are server-side only. Add the `tools/sync_serverless_proto.sh` helper (executable) that fetches the proto, strips them, and regenerates the stubs.
- Generate `src/serverless/grpc.rs` with the repo's own tooling. Do not hand-edit it. Run `cargo test --test serverless_protos -- --ignored --nocapture`, which uses `tonic-prost-build` (client only) and writes a `// @generated` banner. A freshness test requires `src/serverless/grpc.rs` to exist and be at least as new as the proto file. If you touch the proto, regenerate afterwards. Keep the generated module `pub(crate)` so it does not leak into the public API.

## Public API (`qdrant_client::serverless`)

Add `pub mod serverless;` in `src/lib.rs`, with `client`, `conversions`, `grpc` and `models` submodules. Re-export all public items at `serverless::*`.

**Client**
- `QdrantServerless` is `Clone` and wraps a regular `Qdrant`. Reuse its connection pool and its API-key and custom-header interceptor for the collections service. Make the needed `Qdrant` internals `pub(crate)`.
- `QdrantServerless::from_url(&str) -> QdrantServerlessBuilder`, and `QdrantServerless::new(QdrantConfig) -> QdrantResult<Self>`.
- Builder methods: `api_key`, `timeout`, `connect_timeout`, `compression(Option<CompressionEncoding>)`, `header(key, value)`, `keep_alive_while_idle()` and `build() -> QdrantResult<QdrantServerless>`.
- `check_compatibility` is always false. Construction makes no network call.
- `pub const DEFAULT_SERVERLESS_GRPC_PORT: u16 = 443`. A URL with no explicit port gets `:443` appended (`https://h` becomes `https://h:443`). A URL with a port is unchanged.
- Collection methods:
  - `create_collection(name, CollectionConfig) -> CreateCollectionResult`
  - `delete_collection(name) -> DeleteCollectionResult`
  - `get_collection(name) -> CollectionInfo`. A missing collection is not an error; check `exists`.
  - `collection_exists(name) -> bool`
  - `list_collections(impl Into<ListCollections>) -> CollectionsList`
- Point methods:
  - Methods: `query`, `query_batch`, `query_groups`, `get_points`, `scroll`, `count`, `upsert_points`, `update_vectors`, `delete_vectors`, `delete_points`, `set_payload`, `overwrite_payload`, `delete_payload`, `clear_payload`, `batch_update_points`.
  - Each takes `impl Into<…>` of the regular request type and delegates to the regular client.
  - Before sending, clear the fields serverless rejects: `read_consistency`, `shard_key_selector`, `ordering`, `lookup_from`, `with_lookup`, `update_filter` and `update_mode`, wherever each exists on that request. Apply this to every sub-query in a batch.

**Models** (`models.rs`)
- All models derive `Debug` and `Clone`, and derive serde `Serialize` and `Deserialize` under the `serde` feature.
- Enums `Distance`, `PrecisionTier` (including `Full`) and `Tokenizer`, with `snake_case` serde names.
- `DenseVectorConfig::new(size, distance)` plus `.multivector(bool)` and `.precision_tier(t)`.
- `SparseVectorConfig::new()` plus `.use_idf(bool)` and `.precision_tier(t)`.
- `KeywordIndex::new().with_prefix()`, and the unit structs `KeywordPrefixParams`, `FloatIndex`, `UuidIndex`, `DatetimeIndex`, `GeoIndex` and `BoolIndex`.
- `IntegerIndex::new().lookup(b).range(b)`.
- `StopwordsSet::new().languages(..).custom(..)`.
- `SnowballParams::new(lang)`.
- `StemmingAlgorithm::{Snowball(SnowballParams), Disabled}`.
- `TextIndex::new()` with builder setters named after its fields: tokenizer, lowercase, phrase_matching, min_token_len, max_token_len, ascii_folding, stopwords, stemmer.
- `PayloadIndex`, an enum with one variant per index kind. It has `From` impls for each index struct and serde `tag = "type"`.
- `CollectionConfig` has pub fields `dense_vectors`, `sparse_vectors` (both `HashMap<String, _>`) and `payload_indexes`. Its methods are:
  - `new`
  - `dense_vector` and `sparse_vector`, which use the empty name `""`
  - `named_dense_vector` and `named_sparse_vector`
  - `payload_index(field, impl Into<PayloadIndex>)`
- Results: `CreateCollectionResult {collection_name, result, time: f64}`, `DeleteCollectionResult {deleted, time}`, `CollectionInfo {exists, config: Option<CollectionConfig>, point_count: Option<u64>, time}`, `CollectionSummary {collection_name, point_count}` and `CollectionsList {collections, next_offset_token, time}`.
- `ListCollections {limit: Option<u32>, offset_token: Option<String>}` with `new()`. `ListCollectionsBuilder` has `new`, `limit`, `offset_token` and `build`, and converts `Into<ListCollections>`.

**Conversions**
- Convert models to and from the gRPC types. Return `QdrantError::ConversionError` for unspecified or unknown enum values and for a stemmer with no variant set.
- Destructure every struct field in the conversions, so adding a field breaks compilation until the conversions are updated.

## Example and Cargo

Add `examples/serverless.rs` and a `[[example]]` entry in `Cargo.toml` named `serverless` with `required-features = ["serde"]`.

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
