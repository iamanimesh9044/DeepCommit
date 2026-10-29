# Align local-mode qdrant-client with server semantics: filters, payload, UUID ids, deletes, persistence

Repository to change: `/app/qdrant-client` only (Python SDK, local mode: `:memory:` and `path=`). Local mode must behave like a remote server. Sync and async clients must share the behaviour.

## Filters and conversion
- Slice condition validation: `ValueError` message must read `Slice index must be in [0;{total - 1}], got {index}`.
- `prefix` on keyword payload-index params: `prefix=True` converts to gRPC `KeywordPrefixParams` and back. `prefix=False` is sent as absent and returns as `None`.

## Collections
- `collection_exists("")` raises `ValueError("Collection name must not be empty")` on both clients.
- `update_collection(metadata=...)` merges like the server: a `None` value removes that key.

## Payload semantics
- `set_payload` (with or without `key=`) treats a `None` value as key removal, at any nesting level it merges into.
- `value_by_key` and payload filters: a path ending in `[]` or `[i]` unwraps a selected array one level when `flat=True`. For example, `double-nest-array[0]` gives `[1, 2]` and `double-nest-array[]` gives `[1, 2, 3, 4, 5, 6]`. With `flat=False` arrays are kept (`[[1, 2], [3, 4], [5, 6]]` and `[[1, 2]]`).
- `with_payload` selectors (include list, `PayloadSelectorExclude`) must work on quoted keys such as `"a.b"` and on `l[].c`. `a` includes everything below it, and `a[0]` matches nothing. `[]` or `with_payload=[]` selects no fields, it does not disable the payload. An invalid selector must raise even when no points match. This applies to retrieve, scroll, query, group queries and `with_lookup`.
- `with_vectors` given as a list returns only the named vectors. A `str` raises `ValueError`.

## UUID identity
Point ids that are the same UUID in any spelling (upper-case, 32 hex digits, `{...}`, `urn:uuid:...`, `uuid.UUID` objects) are the same point in upsert, delete, retrieve, scroll offset, payload ops, `HasIdCondition`, query-by-id and the async client. Stored ids are canonical lowercase hyphenated. Malformed spellings (e.g. `{{...}}`, `uuid:...`, doubled hyphens) never match an existing point. As a point id in an upsert they raise `ValueError("Point id ... is not a valid UUID")`. Callers' point objects, vectors and filters must not be mutated.

## Missing and deleted points
- A deleted point counts as absent, as does an unknown id.
- `set_payload`, `overwrite_payload`, `delete_payload`, `clear_payload`, `update_vectors` and `delete_vectors` apply to every other listed point, then raise `KeyError(first_missing_id)`.
- Recommend or query-by-id from an absent point raises.
- Empty requests raise `ValueError`: empty id lists or `points=[]` for payload ops, delete, `clear_payload`, upsert, `update_vectors`, `delete_vectors`. Also raised for empty vector-name lists in `delete_vectors`, and for set/delete payload with neither points nor filter.
- Empty uploads (`upload_points`, `upload_collection` with no points) are no-ops.
- Inside `batch_update_points`, empty operations raise `ValueError` only when reached. Operations ahead of the bad one stay applied, and so does an unknown vector name in `DeleteVectors`: `Not existing vector name error: <name>`.
- Dense vectors must be one-dimensional, otherwise `ValueError`.

## Sparse vectors and IDF
- Stored sparse vectors share no lists with caller objects, in both directions. They are stored sorted by index, including after a reload.
- IDF statistics count only live points that have the vector. Delete points, delete vectors, update vectors and re-upsert must keep them consistent. Persisted collections must give the same IDF after reopening.

## Persistence (`path=`)
- Write to storage before mutating memory, so a failed storage write can be retried and leaves memory unchanged. This covers upsert (insert-only and update-filter), payload ops, vector update/delete, `delete_vector_name` and point deletion. Unreached points stay visible after a failed delete.
- Failed `load_vectors` closes the storage and re-raises. A rejected upsert stores nothing.
- Key pickle protocol is pinned to 4. Commit in a transaction.
- Loading handles legacy rows (other UUID spellings, other pickle protocols). The latest row (highest rowid) wins and the older copies are deleted with a `UserWarning` about removing outdated copies. Delete, update and upsert of such points must not resurrect old rows. Migration must be transactional.

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
