# Verify Commit-Split Compilation

## Objective

Verify that a commit-split (multiple subcommits) compiles successfully when patches are applied sequentially, and the final result matches the original commit.

## Inputs

- `{{base_image_name}}` - Docker image with compilation environment
- `{{original_commit}}` - The commit being split (e.g., `00c83af`)
- `{{repo_data_path}}` - Path to repository data on host (e.g., `/path/to/repo_data`)
- `{{split_dir}}` - Path to split directory (e.g., `/path/to/split-00c83af`)

## Environment Setup

### 1. Start Container
```bash
docker run -d --name verify-split-{{original_commit}} {{base_image_name}} tail -f /dev/null
```

### 2. Copy Repository Data
```bash
docker exec verify-split-{{original_commit}} rm -rf /testbed
docker cp {{repo_data_path}} verify-split-{{original_commit}}:/testbed
```

### 3. Configure Git
```bash
docker exec verify-split-{{original_commit}} git config --global --add safe.directory /testbed
```

### 4. Verify Parent Compiles (Environment Check)
```bash
docker exec verify-split-{{original_commit}} bash -c "cd /testbed && \
  git checkout {{original_commit}}^ && \
  go build ./cmd/..."
```
If this fails, fix environment issues before proceeding.

## Verification Steps

### 1. Create Test Branch from Parent
```bash
docker exec verify-split-{{original_commit}} bash -c "cd /testbed && \
  git checkout {{original_commit}}^ && \
  git checkout -B verify-split"
```

### 2. Copy Patches into Container
```bash
docker cp {{split_dir}}/patches verify-split-{{original_commit}}:/tmp/patches
```

### 3. Apply and Test Each Patch (in order)
```bash
docker exec verify-split-{{original_commit}} bash -c "cd /testbed && \
  git apply /tmp/patches/<sha>.patch && \
  go build ./cmd/... && \
  git add -A && git commit -m 'Applied <sha>'"
```

### 4. Verify Final Result
```bash
docker exec verify-split-{{original_commit}} bash -c "cd /testbed && git diff {{original_commit}} ."
# Should produce empty output
```

### 5. Cleanup
```bash
docker stop verify-split-{{original_commit}} && docker rm verify-split-{{original_commit}}
```

## Handling Compilation Failures

If a patch fails to compile, analyze the error and fix the split. You have full autonomy to:

- **Re-partition code** between patches to resolve dependencies
- **Merge patches** that have tightly coupled changes
- **Reorder patches** to ensure definitions come before usages
- **Regenerate patches** after restructuring commits

The goal is to produce a valid split where every intermediate state compiles. Iterate until all patches pass, then verify the final result matches the original commit.

## Success Criteria

- Parent commit compiles (environment verified)
- All patches apply cleanly
- Build succeeds after each patch
- Final diff against original is empty
