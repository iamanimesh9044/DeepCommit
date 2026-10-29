# Remove deprecated fastembed methods and bring the SDK in line with the v1.19/v1.20 contract

Repository: `/app/qdrant-client`. `/app/qdrant` is the contract repo and is read-only. Its `lib/api/src/grpc/proto` files are the source for the gRPC stubs.

## 1. Remove the deprecated fastembed API
Remove these from `QdrantFastembedMixin` and `AsyncQdrantFastembedMixin`, and so from `QdrantClient` and `AsyncQdrantClient`:
- `add`, `query`, `query_batch`
- `set_model`, `set_sparse_model`
- `embedding_model_name` and `sparse_embedding_model_name`
- `get_vector_field_name`, `get_sparse_vector_field_name`
- `get_fastembed_vector_params`, `get_fastembed_sparse_vector_params`
- the `DEFAULT_EMBEDDING_MODEL` class constant

Keep the following:
- `list_text_models`, `list_image_models` and `list_sparse_models`.
- `DEFAULT_BATCH_SIZE`.
- `get_embedding_size(model_name: str) -> int`. The argument is now required, with no default.
  - `"BAAI/bge-base-en-v1.5"` gives 768.
  - `"Qdrant/resnet50-onnx"` gives 2048.
  - `"colbert-ir/colbertv2.0"` gives 128.
  - `"Qdrant/bm25"` raises `ValueError("Sparse embeddings do not return fixed embedding size and distance type")`.

Update `tools/async_client_generator/fastembed_generator.py` and regenerate the async mixin with the repo's generator. Do not hand-edit it. `models.Document` and `models.Image` inference through `upsert`, `query_points` and `search` is unchanged.

## 2. Regenerate gRPC stubs for the v1.19 protos
Regenerate `qdrant_client/grpc/*` with the repo's gRPC generation script from the `/app/qdrant` protos. Do not edit the stubs by hand. `qdrant_client.grpc` must expose these names (tests build them):
- `grpc.Memory` with `Cold`, `Cached` and `Pinned`.
- `grpc.PayloadStorageParams(memory=...)`.
- A `memory` field on `VectorParams`, `HnswConfigDiff`, `ScalarQuantization`, `BinaryQuantization`, `TurboQuantization`, product quantization, the sparse index params and the payload index params. Those index params are integer, float, bool, geo, datetime, uuid, keyword and text.
- `grpc.SliceCondition(total, index)`, usable as a filter `Condition` (`slice`).
- `grpc.IdfParams(corpus=Filter)`, set as `SearchParams.idf`.
- `grpc.KeywordPrefixParams` and `TokenizerType.Prefix`.
- `grpc.DisabledStemmer`, as `StemmingAlgorithm(disabled=...)`.
- `grpc.Modifier.Idf`.
- `max_resident_memory_percent`.

REST/gRPC conversion must round-trip all of these. That includes `models.Memory`, `models.PayloadStorageParams`, `models.SliceCondition`/`models.Slice(total, index)`, `models.IdfScope.GLOBAL`, `models.IdfParams` and `models.IdfCorpusParams(corpus=Filter)`. Pass `models.Memory.CACHED/PINNED/COLD` through `create_collection` and `update_collection`, and read them back unchanged. This applies to `payload=models.PayloadStorageParams(memory=...)`, `vectors_config.memory`, quantization `memory`, `hnsw_config.memory` and `sparse_vectors_config[...].index.memory`.

## 3. Numpy vectors fast path
Add `qdrant_client/_numpy_vectors.py` exporting `NumpyVectorsToLists` and `_vectors_to_lists(vectors)`.
- A numpy array becomes `.tolist()`.
- A list of arrays becomes a list of lists.
- A dict of named vectors converts each value. Sparse vectors and anything else pass through unchanged.

The generated `models.PointStruct.vector`, `models.PointVectors.vector` and `models.Batch.vectors` carry `NumpyVectorsToLists` in their field `metadata`. They accept numpy arrays (float32 widens exactly: `np.float32([0.1])` gives `[0.10000000149011612]`) and memmaps.
- This also covers ragged multivectors and named dicts.
- Invalid input must still raise `ValidationError`, for example a dict with a non-str key.
- `models.PointsBatch(batch={...})` parses the same way.

## 4. Local mode correctness
Local mode must agree with a real server in the congruence tests:
- **Slice filters.** `SliceCondition` is supported in scroll, query and nested filters. Assignment is deterministic by a siphash-2-4 of the point id: the id is serialized as little-endian numeric, or as RFC 4122 byte order for a UUID that is given as `str` or `uuid.UUID`. Slices must be disjoint, must cover all points, and must be nested across totals. `total=1` takes everything. Invalid slices are rejected with the server's error (`total <= 0`, `index < 0`, `index >= total`).
- **IDF scope.** `SearchParams.idf` is honoured in sparse search, prefetch and grouped queries. `None` and `IdfScope.GLOBAL` use all points. `IdfCorpusParams(corpus=filter)` computes IDF statistics over the matching points only. Points without the sparse vector are ignored.
- **Filter matching.**
  - `MatchValue` and array payloads match when any element matches.
  - Bool, int and float values never cross-match.
  - `MatchExcept` does not match null.
  - `IsNull` matches null inside arrays.
  - `MatchText` on non-string fields and unindexed fields behaves like the server.
  - Nested filters accept all payload shapes: `company`, `company[]`, `company[0]` and `company[1]`.
- **Payload.**
  - `set_payload` with wildcard keys keeps array elements independent.
  - It must not leak across points.
  - Nested-key deletion works.
  - Non-JSON-able payloads raise.
- **Datetimes and order_by.**
  - Datetime parsing follows server leniency (` UTC`, lowercase `z`, nanosecond fractions truncated to microseconds, a space before the offset, hour-only offsets).
  - Truncated values such as `"2024-06-15 12"` are rejected.
  - `order_by` and scroll accept a bare `date` as UTC midnight and a naive datetime.
  - Bool values are skipped.
  - Facets keep scalar types distinct and normalize UUIDs.
- **Queries.**
  - A flat `query=` vector or sparse vector becomes a nearest query.
  - Prefetch is resolved once.
  - `search_params` and `score_threshold` apply to RRF and DBSF fusion.
  - Query validation errors (invalid offset, limit, rrf_k, group_size and the like) match the remote errors.
  - `delete_collection` returns `False` for a missing collection.
- **Formula.**
  - `$score[n]` accepts only ASCII digits.
  - `pow` raises when the result is non-finite.
  - Decay midpoints are range-checked.
- **Cosine.** `cosine_similarity` does not mutate the query.

## 5. Packaging
Set the version to `1.20.0` and add an optional `orjson` extra. Set `fastembed` to `^0.9`.

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
