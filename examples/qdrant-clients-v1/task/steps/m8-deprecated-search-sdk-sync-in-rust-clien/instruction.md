# Serverless client: collection management, gRPC stubs, models and async mirror; Rust SDK deprecations

## `/app/qdrant-client`

Add an experimental `qdrant_client.serverless` package. Mark every module and public class "in development, do not use yet".

### Proto and generated stubs

- Source: `qdrant_client/serverless/proto/serverless_collections.proto`, package `qdrant.serverless`, service `CollectionsService`.
- RPCs: `CreateCollection`, `DeleteCollection`, `GetCollection`, `ListCollections`.
- Stubs go in `qdrant_client/serverless/grpc/`. Importing `qdrant_client.serverless.grpc.serverless_collections_pb2` as `pb2` must work.
- Generate them with `tools/generate_serverless_grpc_client.sh`, which pins the tool versions. Do not hand-edit the `_pb2*` files.
- Enums:
  - `Distance`: `COSINE=1`, `EUCLID=2`, `DOT=3`, `MANHATTAN=4`.
  - `PrecisionTier`: `LOW=1`, `MEDIUM=2`, `HIGH=3`, `FULL=4`.
  - `Tokenizer`: `PREFIX=1`, `WHITESPACE=2`, `WORD=3`, `MULTILINGUAL=4`.
  - Each enum also has a `*_UNSPECIFIED = 0` member.
- Messages:
  - `DenseVectorConfig`: `size` (u64), `distance`, `multivector` (bool), optional `precision_tier`.
  - `SparseVectorConfig`: `use_idf`, optional `precision_tier`.
  - `KeywordIndex`: optional `prefix` (`KeywordPrefixParams`, empty).
  - `IntegerIndex`: optional `lookup` and `range`.
  - `FloatIndex`, `UuidIndex`, `DatetimeIndex`, `GeoIndex`, `BoolIndex`: all empty.
  - `StopwordsSet`: `languages` and `custom`, both repeated strings.
  - `SnowballParams`: `language`.
  - `DisabledStemmer`: empty.
  - `StemmingAlgorithm`: oneof `stemming_params` with `snowball` or `disabled`.
  - `TextIndex`: optional `tokenizer`, `lowercase`, `phrase_matching`, `min_token_len`, `max_token_len`, `ascii_folding`, `stopwords`, `stemmer`.
  - `PayloadIndexConfig`: oneof `index` with `keyword`, `integer`, `float`, `uuid`, `datetime`, `text`, `geo`, `bool`.
  - `CollectionConfig`: maps `dense_vectors`, `sparse_vectors` and `payload_indexes`, keyed by name or field.
- Requests and responses (every field name is checked against the pydantic result models, so names must match exactly):
  - `CreateCollectionRequest`: `collection_name`, `config`.
  - `CreateCollectionResponse`: `collection_name`, `result`, `time` (double).
  - `DeleteCollectionRequest`: `collection_name`.
  - `DeleteCollectionResponse`: `deleted`, `time`. There is no `objects_deleted`.
  - `GetCollectionRequest`: `collection_name`.
  - `GetCollectionResponse`: `exists`, optional `config`, optional `point_count` (u64), `time`.
  - `ListCollectionsRequest`: optional `limit` (u32), optional `offset_token`.
  - `CollectionSummary`: `collection_name`, optional `point_count`.
  - `ListCollectionsResponse`: repeated `collections`, optional `next_offset_token`, `time`.

### `qdrant_client.serverless.models` (pydantic)

- `Distance` and `TokenizerType` are re-exported from `qdrant_client.http.models`.
- `PrecisionTier` is a str enum: `LOW="low"`, `MEDIUM="medium"`, `HIGH="high"`, `FULL="full"`.
- Every model field name equals the proto field name, except the `type` tag on index kinds.
- `DenseVectorConfig(size, distance, multivector=False, precision_tier=None)`.
- `SparseVectorConfig(use_idf=False, precision_tier=None)`.
- `KeywordPrefixParams` has no fields.
- Index models, each with a literal `type` tag whose value is the oneof name:
  - `KeywordIndex(prefix=None)`
  - `IntegerIndex(lookup=None, range=None)`
  - `FloatIndex`, `UuidIndex`, `DatetimeIndex`, `GeoIndex`, `BoolIndex`
  - `TextIndex` with all eight optional fields defaulting to `None`
- `StopwordsSet(languages=[], custom=[])`.
- `SnowballParams(language)`.
- `StemmingAlgorithm(snowball=None, disabled=None)`, where `disabled` is an optional bool.
- `PayloadIndex` is a `Union` of the eight index models.
- `CollectionConfig(dense_vectors={}, sparse_vectors={}, payload_indexes={})`.
- Result models:
  - `CreateCollectionResult(collection_name, result, time)`.
  - `DeleteCollectionResult(deleted, time)`.
  - `CollectionInfo(exists, config=None, point_count=None, time)`.
  - `CollectionSummary(collection_name, point_count=None)`.
  - `CollectionsList(collections, next_offset_token=None, time)`.

### `qdrant_client.serverless.conversions`

- Module-level enum maps `_DISTANCE_TO_GRPC`, `_PRECISION_TO_GRPC` and `_TOKENIZER_TO_GRPC`. Each maps model member to proto value.
- Each map covers every non-`UNSPECIFIED` proto member, including `FULL`. Model member names equal proto member names.
- Converter pairs `<stem>_to_grpc` and `<stem>_from_grpc` for exactly these stems:
  - `dense_vector`
  - `sparse_vector`
  - `stopwords`
  - `stemmer`
  - `payload_index`
  - `collection_config`
- The `stopwords` and `stemmer` converters may be underscore-prefixed.
- No other function in the module may end in `_to_grpc` or `_from_grpc`.
- Presence must round-trip. An unset optional stays unset. A falsy value that was set (`range=False`, `lowercase=False`, `min_token_len=0`, `disabled=True`) stays set. An empty `KeywordPrefixParams` stays present.
- A `StemmingAlgorithm` with neither `snowball` nor `disabled=True` raises `ValueError`.

### Clients

`QdrantServerless` is in `qdrant_client.serverless.client`. `AsyncQdrantServerless` is in `qdrant_client.serverless.async_client`. Both are exported from `qdrant_client.serverless`.

- Constructor: `QdrantServerless(url, api_key=None, grpc_port=443, timeout=None, grpc_options=None, **kwargs)`.
- It delegates to a `QdrantRemote` held as `_remote`. gRPC is preferred and the compatibility check is off.
- Construction must not touch the network.
- `_remote._grpc_headers` contains `("api-key", key)`, `_remote._grpc_port == 443`, and `_remote._https` is true for an `https` URL.
- `close(grpc_grace=None)` closes the connections.
- Collection methods, each with an optional `timeout`:
  - `create_collection(collection_name, dense_vectors=None, sparse_vectors=None, payload_indexes=None, timeout=None)`. A single `DenseVectorConfig` or `SparseVectorConfig` registers under the unnamed `""` vector. A dict is keyed by name.
  - `delete_collection`
  - `get_collection`
  - `collection_exists`
  - `get_collections(limit=None, offset_token=None, timeout=None)`
- Point operations such as `query_points` and `upsert` delegate to `_remote`, without consistency, shard-key or write-ordering parameters.
- The async client must expose the same public and private method names as the sync one. Every non-dunder method must be a coroutine function, except the helpers `_collections` and `_collections_timeout`, `__init__` and `close`, which keep the sync client's behaviour.
- Generate the async client with `tools/generate_async_client.sh`. Add a serverless generator there, and do not hand-write the file.

Also add `examples/serverless_client.py`, and exclude `qdrant_client/serverless/grpc` and `examples` from `mypy.ini`.

## `/app/rust-client`

Mirror the qdrant contract's deprecations.

- In `proto/points_service.proto`, mark the RPCs `Search`, `SearchBatch`, `SearchGroups`, `Recommend`, `RecommendBatch`, `RecommendGroups`, `Discover` and `DiscoverBatch` with `option deprecated = true`. Add a "Deprecated: use … instead" comment on each, pointing at `Query`, `QueryBatch` or `QueryGroups`.
- In `src/qdrant_client/search.rs`, put `#[deprecated(since = "1.20.0", …)]` on the eight wrapper methods. They are `search_points`, `search_batch_points`, `search_groups`, `recommend`, `recommend_batch`, `recommend_groups`, `discover` and `discover_batch`. Each note points to the Query API replacement. They keep working.
- In `proto/collections.proto`, deprecate `max_resident_memory_percent` and reserve field 22, removing `max_disk_usage_percent`. Drop that field from `StrictModeConfigBuilder`.
- Add `proto/quota_internal.proto` with `GetQuotaUsageRequest`, `GetQuotaUsageResponse` and `QuotaUsage`.
- Regenerate `src/qdrant.rs` with the repo's tonic/prost tooling. Do not hand-edit it.

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
