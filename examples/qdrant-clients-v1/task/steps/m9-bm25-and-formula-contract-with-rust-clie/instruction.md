# Sync Rust client with BM25 text query, id tracker, Turbo, formula and substring-match contract

Update the Rust SDK in `/app/rust-client` to the new Qdrant API contract. The `.proto` files under `proto/` change first. Regenerate `src/qdrant.rs` from them with the repository's own protobuf codegen tooling. Do not hand-edit the generated file. Then add the hand-written builders and helpers below. All of it must compile, because the snippet tests call these APIs.

## Proto contract (`proto/*.proto`)

**`collections.proto`**
- Import `google/protobuf/timestamp.proto`.
- `Datatype` gains `Turbo8 = 5` and `Turbo16 = 6`.
- `TurboQuantBitSize` gains `Bits8 = 4`.
- New message `IdTrackerParams { optional Memory memory = 1; }`. Documented meaning:
  - `Cold` keeps the point id mapping on disk and reads it on demand.
  - `Cached` keeps it on disk but primes the page cache on load.
  - `Pinned` keeps it in RAM.
- New field `optional IdTrackerParams id_tracker` on these messages:
  - `CreateCollection` = 20
  - `CollectionParams` = 13
  - `CollectionParamsDiff` = 7
- `TextIndexParams` gains `optional TextScoringParams scoring = 12`.
- New enum `TextScoringType { Bm25 = 0; }`.
- New message `TextScoringParams { TextScoringType type = 1; }`.
- `CollectionInfo` gains `optional google.protobuf.Timestamp created_at = 13`. In Rust this is `Option<prost_types::Timestamp>`.

**`points.proto`**
- `Expression` oneof gains `Expression acosh = 20`, `MaxExpression max = 21` and `MinExpression min = 22`.
- New messages `MaxExpression { repeated Expression max = 1; }` and `MinExpression { repeated Expression min = 1; }`.
- `Query` oneof gains `TextQuery text = 12`.
- New message `TextQuery { string query = 1; oneof scoring { Bm25Params bm25 = 2; } }`.
- New message `Bm25Params { optional float k = 1; optional float b = 2; }`. The defaults are k = 1.2 and b = 0.75, applied server-side.
- Update the comments on `using` in `PrefetchQuery`, `QueryPoints` and `QueryPointGroups`. For a `text` query, `using` names the payload field whose text index is searched.
- Update the `IdfParams` and `SearchParams.idf` comments to say IDF also applies to BM25 over a text index.
- Update the `DenseVectorCreationConfig.datatype` comment to list Turbo8 and Turbo16.

**`qdrant_common.proto`**
- `Match` oneof gains `string substring = 12`.

Also fix the "larger than this size" comment typo on `OptimizersConfigDiff`.

## Rust SDK additions

All of these are exported under `qdrant_client::qdrant`.

- **`IdTrackerParamsBuilder`**
  - `new()` and `Default`.
  - `.memory(impl Into<i32>)`, so `Memory::Pinned` works.
  - `.build() -> IdTrackerParams`, and `From<IdTrackerParamsBuilder> for IdTrackerParams`.
- **Collection builders:** `CreateCollectionBuilder::id_tracker(impl Into<IdTrackerParams>)` and `CollectionParamsDiffBuilder::id_tracker(impl Into<IdTrackerParams>)`. Both must pass the value through to the built message. `CreateCollectionBuilder` must accept `.id_tracker(IdTrackerParamsBuilder::default().memory(Memory::Pinned))` directly.
- **`TextIndexParamsBuilder`**
  - `.scoring(impl Into<TextScoringParams>)`.
  - `.bm25_scoring()`, which is equivalent to `.scoring(TextScoringType::Bm25)`.
  - `From<TextScoringType> for TextScoringParams`.
  - `.bm25_scoring()` must be chainable after `.lowercase(true)`, and the result must be usable as `field_index_params(...)`.
- **`TextQueryBuilder`**
  - `new(impl Into<String>)`.
  - `.k(f32)` and `.b(f32)`.
  - `.build() -> TextQuery`.
  - Behaviour of `build()`: if neither `k` nor `b` is set, `scoring` is `None`. Otherwise `scoring` is `Some(text_query::Scoring::Bm25(Bm25Params { k, b }))`, carrying only the values that were set.
  - `From<TextQueryBuilder> for TextQuery`.
- **Query conversions:**
  - `Query::new_text(impl Into<TextQuery>) -> Query`, producing `query::Variant::Text`.
  - `From<TextQuery> for Query` and `From<TextQueryBuilder> for Query`.
  - `impl<T: Into<String>> From<T> for TextQuery`, so `Query::new_text("vector database")` works with a plain `&str`.
  - `Query::new_text(TextQueryBuilder::new("vector database").k(1.5).b(0.5))` must work, and the result must be accepted by `QueryPointsBuilder::query(...)`.
- **Expressions:**
  - `Expression::acosh(expr)`.
  - `Expression::max(MaxExpression)` and `Expression::min(MinExpression)`.
  - `Expression::max_with(iter)` and `Expression::min_with(iter)`, which build from an iterator of items convertible into `Expression`, like the existing `sum_with`.
- **Filters:**
  - `Condition::matches_substring(field: impl Into<String>, substring: impl Into<String>)`. It builds a field condition whose match is `MatchValue::Substring`.
  - Negating `MatchValue::Substring` panics with `cannot negate a MatchValue::Substring, use within must_not clause instead`, the same pattern as `Prefix`.

## Must stay consistent

The hand-written builders must produce exactly the proto field numbers and oneof variants above. In the generated code, the enum values `Turbo8` = 5, `Turbo16` = 6 and `Bits8` = 4 must also have working `as_str_name` and `from_str_name` entries.

The tests call every new API from compiled snippets: id tracker on create collection, BM25 text index, text query with and without `k`/`b` plus a filter, and substring match in scroll. Any compile failure in the snippet module fails every test.

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
