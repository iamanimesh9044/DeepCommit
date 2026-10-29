# gRPC payload conversion performance, correctness fixes, and contract sync

Two repositories change.

- `/app/qdrant` (contract): update the `.proto` files under `lib/api/src/grpc/proto/`.
- `/app/qdrant-client` (SDK): rework the gRPC conversion paths and regenerate the stubs.

## `/app/qdrant`: contract (proto files only)

Apply these changes. Do not touch Rust code.

- `collections.proto`:
  - Add `Turbo8 = 5` and `Turbo16 = 6` to the vector datatype enum.
  - Add `Bits8 = 4` to the quantization-bits enum.
  - Add `IdTrackerParams { optional Memory memory = 1; }`, with `optional IdTrackerParams id_tracker` on collection params (20), on the collection-create/config message (13) and on the update message (7).
  - Add `optional string nested` to the keyword, integer, float, geo, datetime, uuid and text index params, and the matching params-diff messages.
  - Add `optional TextScoringParams scoring = 12` to the text index params, with `enum TextScoringType { Bm25 = 0 }` and `message TextScoringParams { TextScoringType type = 1; }`.
  - Add `optional google.protobuf.Timestamp created_at = 13` to the collection info.
  - Mark `max_resident_memory_percent = 21` as `[deprecated = true]`.
  - Remove `max_disk_usage_percent = 22` and write `reserved 22;`.
  - Add `double time` to the responses that lack it.
- `points.proto`:
  - Add `message IdfParams { optional Filter corpus = 1; }` and `optional IdfParams idf = 6` on the sparse-vector query params.
  - Add `acosh = 20`, `MaxExpression max = 21` and `MinExpression min = 22` to the formula `Expression`, with `message MaxExpression { repeated Expression max = 1; }` and the same for `MinExpression`.
  - Add `TextQuery text = 12` to the query oneof, with `message TextQuery { string query = 1; oneof scoring { Bm25Params bm25 = 2; } }` and `message Bm25Params { optional float k = 1; optional float b = 2; }`.
  - Update the `using` field comments.
- `qdrant_common.proto`: add `SliceCondition slice = 8` to `Condition`, with `message SliceCondition { uint32 total = 1; uint32 index = 2; }`. Add `string substring = 12` to the match oneof.
- `points_service.proto`: mark `Search`, `SearchBatch`, `SearchGroups`, `Recommend`, `RecommendBatch`, `RecommendGroups`, `Discover` and `DiscoverBatch` with `option deprecated = true;`.
- Internal protos:
  - `points_internal_service.proto`: add `PointStructRaw`, `RawPayload`, `RawPayloadEncoding { JsonBytes = 0 }`, `TextScoringInternal` and the `raw_points` fields.
  - `qdrant_internal_service.proto`: add `GetQuotaUsage` and `GetConsensusAppliedLog` with their messages.
  - New `quota_internal.proto`: add `GetQuotaUsageRequest`, `GetQuotaUsageResponse` and `QuotaUsage`.
  - `collections_internal_service.proto` and `shard_snapshots_service.proto`: add `optional uint64 from_peer_id`.
  - `storage_read_service.proto`: add `optional google.protobuf.Timestamp last_modified = 3`.

## `/app/qdrant-client`: SDK

Regenerate `qdrant_client/grpc` and `qdrant_client/proto` with `tools/generate_grpc_client.sh` after updating the protos. Do not edit the generated stubs by hand. Keep the REST models and the gRPC↔REST conversions consistent with the new contract fields.

### `qdrant_client/conversions/conversion.py`

- **`json_to_value(payload) -> Value`.** It must build the whole nested `Value` tree in one construction. Types:
  - `str`, `int`, `float`, `bool` and `None` map to the matching `Value` field.
  - `list` and `tuple` become `list_value`.
  - `dict` with all-`str` keys becomes `struct_value`.
  - Subclasses encode like their plain values. Examples: `IntEnum`, `str`-`Enum`, `np.float64`, namedtuple, `OrderedDict`, `defaultdict`, and enum dict keys.
  - Anything else (uuid, datetime, date, time, timedelta, Decimal, set, frozenset, bytes, `Path`, dicts with non-str keys) is encoded exactly as the REST client's `jsonable_encoder` does. `value_to_json(json_to_value(x))` must equal the JSON-decoded REST body value.
  - Unencodable values (`object()`, `np.float32(1.0)`, `{object(): "key"}`) raise `ValueError` with a message containing `Not supported json value`.
- **`payload_to_grpc(payload)`.** It must work for every protobuf version.
  - It returns a dict of Value-constructor argument dicts when the installed protobuf accepts dicts as map values (>= 6.30).
  - Otherwise it returns a dict of `Value` messages from a module-level fallback `_payload_to_values`.
  - Both forms must produce equal `PointStruct(payload=...)` messages.
- **`value_to_json(value)`.**
  - It reads the set oneof field directly instead of using `MessageToDict`.
  - Integers are `int`.
  - Non-finite doubles return the strings `"NaN"`, `"Infinity"` and `"-Infinity"`. `-0.0` stays `-0.0`.
  - An empty struct returns `{}` and an empty list returns `[]`.
  - `null_value` returns `None`.
- **`grpc_to_payload(map)`** and **`GrpcToRest.convert_payload`** use these functions.
- **`convert_scored_point`.** `payload` is `None` when the message payload map is empty. `convert_retrieved_point` still returns `{}` for an empty payload.
- **Update operations.** In `RestToGrpc.convert_update_operation` for `SetPayloadOperation`, `OverwritePayloadOperation`, `DeletePayloadOperation` and `DeleteVectorsOperation`, select by `points is not None`, then `filter is not None`. An empty `points=[]` must produce an empty `points` selector, even when a filter is also given. If neither is set, raise `ValueError` with `invalid <OperationClassName> model: ...`. `DeleteVectorsOperation` must say `DeleteVectorsOperation`.

### `qdrant_client/_pydantic_compat.py`

On pydantic v1, `to_jsonable_python(x)` must encode like `BaseModel.json()`, so it must handle any type the REST client handles.

### `qdrant_client/uploader/grpc_uploader.py`

Add `convert_vector_struct(vector) -> grpc.Vectors`. It must serialize byte-identically (`SerializeToString(deterministic=True)`) to `RestToGrpc.convert_vector_struct` applied to the `.tolist()` form.

- It accepts lists, numpy arrays, and dicts of them.
- Plain 1-d and 2-d `np.ndarray`/`np.memmap` with dtype kind `f`, `i`, `u` or `b` and itemsize <= 8 may take a raw-bytes fast path (upb only).
- That path converts via float64 to float32, so signaling NaNs become quiet and out-of-range values become inf.
- Everything else falls back to `.tolist()`, which keeps the old results or errors. This covers empty arrays, 3-d arrays, complex arrays, masked arrays and `np.matrix`.
- `upload_batch_grpc` uses it.
- `GrpcBatchUploader.iterate_batches` keeps plain numpy batches or rows as arrays, and converts other array types with `.tolist()`.
- `BaseUploader` (REST) still converts to lists.

### `qdrant_client/embed/type_inspector.py`

Skip inspecting lists made only of numbers or lists. Behaviour is unchanged.

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
