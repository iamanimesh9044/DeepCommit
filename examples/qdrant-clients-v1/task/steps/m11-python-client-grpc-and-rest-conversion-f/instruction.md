# Fix gRPC/REST conversion, upload auth and URL-escaping bugs in qdrant-client; add `Code` tokenizer to rust-client

## `/app/qdrant-client`

**1. Batch length validation.** Add `qdrant_client/common/validation.py` with `validate_batch_lengths(batch: models.Batch) -> None`.
- Every vector column must have `len(batch.ids)` entries. A column is `batch.vectors` itself, or each value of `batch.vectors` when it is a dict.
- `payloads`, when not `None`, must also have `len(batch.ids)` entries.
- On mismatch raise `ValueError` with these exact messages:
  - `number of ids and vectors must be equal ({num_ids} != {len})`
  - `number of ids and payloads must be equal ({num_ids} != {len})`
- Call it from:
  - `RestToGrpc.convert_point_insert_operation` for a `PointsBatch`.
  - The gRPC upsert path in `QdrantRemote` and `AsyncQdrantRemote` for `models.Batch`.
  - The local-mode upsert in `LocalCollection` for `models.Batch`.
- A rejected batch must leave the collection untouched. Local mode and gRPC raise `ValueError`. REST relies on the server error, which contains "number of ids and".

**2. Falsy values in conversions.**
- `RestToGrpc.convert_points_update_operation` must treat a `shard_key` of `0` or `""` as set. Test with `shard_key in (0, "")`. Use `is not None` checks for all operations: upsert, delete, set, overwrite and delete payload, clear payload, update vectors, delete vectors.
- The same applies to `shard_key` and `order_value` in the `ScoredPoint` and `RetrievedPoint` conversions.
- Proto3 optional fields use `HasField`:
  - `GrpcToRest.convert_collection_info`: `points_count` is `None` when absent and `0` when explicitly 0. Both must round-trip through `RestToGrpc.convert_collection_info` with presence preserved.
  - `GrpcToRest.convert_update_result`: `operation_id` is `None` when absent and `0` when explicitly 0. It must round-trip through `RestToGrpc.convert_update_result` with presence preserved.
  - `IntegerIndexParams`: `range` and `lookup` are `None` when absent.
  - `SparseVectorParams.index`: use a real `HasField("index")` check.
- `metadata` is `None` when the proto map is empty. This applies to both collection info and collection-config conversions.
- `GrpcToRest.convert_start_from` must raise `ValueError` for an unmapped model instead of returning `None`.
- `RestToGrpc.convert_vector_struct` with a dict containing an unsupported value must raise `ValueError` with this message: `invalid VectorStruct model: vector {key!r} is {val!r}`. For example, `invalid VectorStruct model: vector 'dense'`. The gRPC uploader's `convert_vector_struct` must surface this error and not skip the vector.

**3. Upload auth and compression.** Over gRPC, `upload_points` and `upload_collection` in the sync and async remote clients must give the uploader the client's `compression` (`self._grpc_compression`) and `auth_token_provider`. The uploader must pass `compression=` as a keyword argument to `get_channel`. The token provider must be invoked during uploads.

**4. gRPC payload index.**
- `create_payload_index` and `delete_payload_index` must convert a `models.WriteOrdering` `ordering` argument to the gRPC form.
- A gRPC `TextIndexParams` with `tokenizer == Unknown` must be sent with `Word` as the tokenizer. REST already defaults to word. The caller's params object must not be modified.

**5. REST path escaping.** In `qdrant_client/http/api_client.py` add `quote_path_params(path_params: Dict[str, Any]) -> Dict[str, str]`.
- It converts each value with `str(...)` and percent-encodes it with `quote(value, safe="")`.
- It raises `ValueError` if a value is exactly `.` or `..`.
- It must be used by both the sync and async API clients when formatting URL path templates. Names like `client_test#1` or `"user#id"` must then reach the server intact.

**6. Docs.** The `timeout` docstring in `QdrantClient` and `AsyncQdrantClient` must say `Default: 5 seconds for both REST and gRPC (when timeout is None)`.

`test_conversion_completeness` must keep passing. Every gRPC model fixture must convert and round-trip.

## `/app/rust-client`

- In `proto/collections.proto`, add `Code = 5;` to enum `TokenizerType`.
- Regenerate `src/qdrant.rs` from the proto with the repo's own generation tooling. Do not hand-edit it. Afterwards it must include `TokenizerType::Code` with value 5, string name `"Code"`, and the matching `from_str_name` case.
- In `src/serde_deser.rs`, `Value` deserialization into `f32` and `f64` (including `Option<f64>`) must also accept an integer payload value. For example, `{"price": 10}` deserializes to `10.0`, and `-3` deserializes to `-3.0`.

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
