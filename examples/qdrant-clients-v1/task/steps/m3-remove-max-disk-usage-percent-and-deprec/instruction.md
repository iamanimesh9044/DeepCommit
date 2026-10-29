# Align local mode and client typing with the server; drop `max_disk_usage_percent`

Repository to change: `/app/qdrant-client`. The Python client consumes the server's global-quota change and hardens local mode so it behaves like the server on invalid input.

## 1. Strict-mode config
- Remove `max_disk_usage_percent` from the gRPC `StrictModeConfig` message, the REST `StrictModeConfig` and `StrictModeConfigOutput` models, and both `GrpcToRest` and `RestToGrpc` conversions. Field number 22 becomes `reserved` in the proto.
- Mark `max_resident_memory_percent` deprecated in the proto (`[deprecated = true]`) and in the REST schema. Keep it working in conversions.
- Update `qdrant_client/proto/collections.proto` and `tools/generate_grpc_client.sh`. The script must also strip `quota_internal.proto`.
- Regenerate the gRPC stubs and REST models with the repository's own tooling (`tools/generate_grpc_client.sh` and the REST generator). Do not hand-edit them.

## 2. Public typing
In `qdrant_client.conversions.common_types`, add `WithPayloadInterface` (REST `WithPayloadInterface` or `grpc.WithPayloadSelector`) and `WithVector` (REST `WithVector`). Use them for the `with_payload` and `with_vectors` parameters of the client, local and remote search, query, scroll and retrieve methods.

## 3. Local mode: all raise `ValueError`, never `assert`
- **Payload and vector selectors:**
  - `with_payload` and `with_vectors` accept tuples as well as lists, in local and remote modes. Remote must convert tuples to lists.
  - A bare `str` raises `ValueError`.
- **NaN checks:** every NaN check in `distances.py`, `multi_distances.py` and `sparse.py` raises `ValueError`. Keep the existing messages, for example `"Query vector must not contain NaN"`.
- **Sparse vectors:**
  - Indices must be in `0..2**32-1`; otherwise raise `"Indices must be between 0 and 4294967295"`.
  - Mismatched lengths and duplicate indices also raise `ValueError`.
- **Collection creation:**
  - `create_collection` raises `ValueError("Sparse vector name cannot be empty")` if `""` is a key of `sparse_vectors_config`.
  - It raises `ValueError` if the name is an existing alias.
  - Invalid new names raise `ValueError`: any of `< > : " / \ | ? * \0 \x1f`, `""`, `"."`, `".."`, or longer than 255 characters. `recreate_collection` applies the same check.
  - A persistent collection path must stay inside the `collection/` folder.
- **Aliases:** `update_collection_aliases` is atomic.
  - It raises `RuntimeError` if the client is closed.
  - It works on a copy and saves before committing to memory. If the save fails (`OSError`), in-memory aliases stay unchanged, and nothing from a rejected batch is persisted.
  - `create_alias` must target an existing collection, not an alias, otherwise `ValueError`.
  - An alias may not take an existing collection's name, for create or rename.
  - Renaming a missing alias raises `ValueError`.
- **Vectors on write (upsert, batch, `update_vectors`, `batch_update_points`):**
  - Validate everything before applying anything, so a rejected point leaves the collection untouched.
  - Reject empty dense vectors, NaN values, and dimension mismatches against the collection's `size` (dense and each multivector row).
  - Reject empty multivectors and empty sub-vectors.
  - Reject a sparse vector for a dense or multivector name, and the reverse.
  - Reject unknown vector names, including in `delete_vectors`. Validate JSON-path keys in `set_payload` and `delete_payload`.
  - `update_vectors` with an `update_filter` must skip non-matching points and continue with the rest, not stop.
  - Absent multivectors are stored as a `(1, size)` array of ones, flagged deleted, not as an empty array.
- **Filters:**
  - Validate the filter before any point is touched, and also on an empty collection: `min_should.min_count < 1` raises `ValueError`.
  - Check `min_count` recursively, through nested conditions and every filter path (search, scroll, upsert and update filters).
  - A `SliceCondition` requires `total >= 1` and `0 <= index < total`.
  - Plain `is_empty` and `is_null` field conditions are honoured: `FieldCondition(is_empty=False/True)` and `is_null=False/True` compare the result to the flag.
  - Geo conditions ignore points whose `lat` or `lon` is non-finite or not a number.
- **Point-id lookup vectors:** when an example is a point ID, using a point whose vector is absent raises `ValueError`. Use this for recommend, discover, context and `lookup_from`.
- **Distance matrix:**
  - Sample only points that have the vector, using a `has_vector` filter.
  - MMR candidate selection must break exact ties like core: the last maximum wins, and the chosen candidate is removed by swapping in the last element.
- **Sparse config updates:** unset fields keep their values, and `index` merges field by field.
- **Isolation:** collection config, metadata and `get_collection` output must not share mutable state with the caller. Deep-copy on create, on `update_collection` metadata merge, and on output.
- **Uploader and batch upload:** the "same number of vectors" check raises `ValueError`. This applies to `upload_collection` and `upload_points`.

## 4. Embeddings
`Embedder` query-embed paths must pass `batch_size` through to `query_embed`.

The local congruence tests compare local-mode results and error types against a live server. Local-mode errors must be `ValueError` where the server returns an error.

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
