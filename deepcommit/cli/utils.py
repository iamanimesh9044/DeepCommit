from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, List, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "DATA"

# Matches common tag patterns:
# - v1.0.0, V1.0.0, 1.0.0 (version with optional v prefix)
# - dubbo-3.3.3, release-1.0.0 (name-version pattern)
_TAG_LIKE_RE = re.compile(r"^(?:v|V)?\d|^[a-zA-Z][\w]*-\d")
_SHA_LIKE_RE = re.compile(r"^[0-9a-f]{7,40}$")


def normalize_time_span(time_span: str) -> str:
    """Normalize the repo range string used across configs and DATA paths.

    Canonical form in this repo is: "<start>_<end>" (underscore).
    For convenience, we also accept "<start>-<end>" (single hyphen) and normalize it.

    Notes:
    - We only normalize a *single* hyphen separator to avoid ambiguity with tags like
      "v1.0.0-rc1". For such tags, users should use the underscore form.
    """
    if "_" in time_span:
        return time_span
    if time_span.count("-") == 1:
        return time_span.replace("-", "_", 1)
    return time_span


def _looks_like_tag_or_sha(text: str) -> bool:
    """Heuristic for parsing preprocessed directory names back to repo specs."""
    if not text:
        return False
    if _SHA_LIKE_RE.match(text):
        return True
    return bool(_TAG_LIKE_RE.match(text))


def get_project_root() -> Path:
    return PROJECT_ROOT


def get_data_root(preferred: str | None = None) -> Path:
    if preferred:
        return Path(preferred).expanduser().resolve()
    env_value = os.environ.get("DEEPCOMMIT_DATA_ROOT") or os.environ.get("AGENTBENCH_DATA_ROOT")
    if env_value:
        return Path(env_value).expanduser().resolve()
    return DEFAULT_DATA_ROOT


def get_trial_space_root(data_root: Path) -> Path:
    return data_root / "trial_space"


def get_result_base_root(data_root: Path) -> Path:
    return data_root / "result"


def parse_repo_spec(repo_spec: str) -> tuple[str, str]:
    if ":" not in repo_spec:
        raise ValueError("Expected repo spec in format 'owner_repo:start_end'")
    owner_repo, time_span = repo_spec.split(":", 1)
    if not owner_repo or not time_span:
        raise ValueError("Malformed repo spec; missing owner_repo or time span")
    # Replace / with _ to normalize repo_id format (e.g., urllib3/urllib3 -> urllib3_urllib3)
    return owner_repo.replace("/", "_"), normalize_time_span(time_span)


def get_next_trial_name(base_name: str, result_dir: Path) -> str:
    trial_path = Path(base_name)
    parent_dir = result_dir / trial_path.parent if trial_path.parent != Path(".") else result_dir
    short_name = trial_path.name

    if not parent_dir.exists():
        return f"{base_name}_001"

    max_num = 0
    found_existing = False

    exact = parent_dir / short_name
    if exact.exists():
        found_existing = True

    for entry in parent_dir.iterdir():
        if not entry.is_dir():
            continue
        name = entry.name
        if not name.startswith(f"{short_name}_"):
            continue
        suffix = name[len(short_name) + 1 :]
        if len(suffix) != 3 or not suffix.isdigit():
            continue
        found_existing = True
        max_num = max(max_num, int(suffix))

    if found_existing:
        new_short_name = f"{short_name}_{max_num + 1:03d}"
        if trial_path.parent != Path("."):
            return str(trial_path.parent / new_short_name)
        return new_short_name
    return f"{base_name}_001"


def _normalize_case(text: str, ignore_case: bool) -> str:
    return text.lower() if ignore_case else text


def _iter_entries(base: Path, entry_type: str) -> List[Path]:
    entries: List[Path] = []
    for child in sorted(base.iterdir(), key=lambda p: p.name.lower()):
        if entry_type == "dir" and not child.is_dir():
            continue
        if entry_type == "file" and not child.is_file():
            continue
        entries.append(child)
    return entries


def _check_required(candidate: Path, required_subpaths: Iterable[str]) -> bool:
    for rel in required_subpaths:
        if not rel:
            continue
        if not (candidate / rel).exists():
            return False
    return True


def _prompt_select(candidates: List[Path], label: str) -> Path:
    count = len(candidates)
    print(f"Multiple {label}s match your input:", file=sys.stderr)
    for idx, candidate in enumerate(candidates, 1):
        print(f"  {idx}) {candidate.name}", file=sys.stderr)

    while True:
        try:
            sys.stderr.write(f"Select {label} [1-{count} or name]: ")
            sys.stderr.flush()
            choice = input().strip()
        except EOFError:
            raise RuntimeError("Selection aborted (EOF)") from None
        except KeyboardInterrupt:
            raise RuntimeError("Selection aborted by user") from None

        if not choice:
            print("Please enter a selection.", file=sys.stderr)
            continue

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= count:
                return candidates[idx - 1]
            print(f"Index out of range. Enter a value between 1 and {count}.", file=sys.stderr)
            continue

        matches = [cand for cand in candidates if cand.name == choice]
        if matches:
            return matches[0]

        print("Invalid selection. Enter a number or exact name from the list.", file=sys.stderr)


def resolve_entry(
    base_dir: Path,
    pattern: str,
    entry_type: str,
    label: str,
    ignore_case: bool,
    required_subpaths: Iterable[str],
) -> Path:
    base_dir = base_dir.expanduser().resolve()
    if not base_dir.exists():
        raise FileNotFoundError(f"Base directory not found: {base_dir}")
    if not base_dir.is_dir():
        raise NotADirectoryError(f"Base path is not a directory: {base_dir}")

    direct = base_dir / pattern
    if direct.exists():
        if entry_type == "dir" and not direct.is_dir():
            raise NotADirectoryError(f"Expected a directory: {direct}")
        if entry_type == "file" and not direct.is_file():
            raise FileNotFoundError(f"Expected a file: {direct}")
        if not _check_required(direct, required_subpaths):
            missing = ", ".join(required_subpaths)
            raise FileNotFoundError(f"Entry '{direct}' missing required path(s): {missing}")
        return direct.resolve()

    # Handle subdirectory patterns (e.g., "experiments/trial_001")
    pattern_path = Path(pattern)
    if len(pattern_path.parts) > 1:
        parent_part = pattern_path.parent
        name_part = pattern_path.name
        search_dir = base_dir / parent_part

        if not search_dir.exists():
            raise FileNotFoundError(f"Parent directory '{parent_part}' not found under {base_dir}")
        if not search_dir.is_dir():
            raise NotADirectoryError(f"Parent path '{parent_part}' is not a directory under {base_dir}")

        # Search within the parent directory
        entries = [
            entry for entry in _iter_entries(search_dir, entry_type) if _check_required(entry, required_subpaths)
        ]
        if not entries:
            raise FileNotFoundError(f"No {label}s found under {search_dir}")

        norm_name = _normalize_case(name_part, ignore_case)
        candidates = [entry for entry in entries if norm_name in _normalize_case(entry.name, ignore_case)]

        if not candidates:
            raise FileNotFoundError(f"No {label} matched pattern '{name_part}' under {search_dir}")

        if len(candidates) == 1:
            print(f"Resolved {label}: {pattern_path.parent}/{candidates[0].name}", file=sys.stderr)
            return candidates[0].resolve()

        return _prompt_select(candidates, label).resolve()

    # Flat pattern - search in base_dir
    entries = [entry for entry in _iter_entries(base_dir, entry_type) if _check_required(entry, required_subpaths)]
    if not entries:
        raise FileNotFoundError(f"No {label}s found under {base_dir}")

    norm_pattern = _normalize_case(pattern, ignore_case)
    candidates = [entry for entry in entries if norm_pattern in _normalize_case(entry.name, ignore_case)]

    if not candidates:
        raise FileNotFoundError(f"No {label} matched pattern '{pattern}' under {base_dir}")

    if len(candidates) == 1:
        print(f"Resolved {label}: {candidates[0].name}", file=sys.stderr)
        return candidates[0].resolve()

    return _prompt_select(candidates, label).resolve()


def select_trial(pattern: str, base_dir: Path, required: Sequence[str] | None = None) -> Path:
    required = required or []
    return resolve_entry(base_dir, pattern, "dir", "trial", False, required)


def remove_path(path: Path) -> None:
    try:
        if path.is_symlink() or path.exists():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
    except FileNotFoundError:
        return


def ensure_symlink(target: Path, link_path: Path) -> None:
    remove_path(link_path)
    link_path.symlink_to(target)


def clone_repo_local(source: Path, dest: Path) -> None:
    """Clone a git repository using --local for hard-linked objects.

    This creates a fully independent clone with its own .git directory,
    allowing separate branches and commits without affecting the source.

    Args:
        source: Path to source repository
        dest: Path to destination (will be created)
    """
    import subprocess

    remove_path(dest)
    result = subprocess.run(
        ["git", "clone", "--local", str(source), str(dest)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git clone --local failed: {result.stderr.strip()}")


def copy_tree(src: Path, dst: Path, *, ignore_patterns: Sequence[str] | None = None) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src,
        dst,
        symlinks=True,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns(*(ignore_patterns or ())),
    )


def find_repo_path(repo_id: str, data_root: Path) -> Path | None:
    base = data_root / "github_data" / "repos"
    candidates = [base / repo_id, base / f"{repo_id}.git"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def preprocessed_dir_to_repo_id(name: str) -> str:
    # Canonical: <repo_id>_<start>_<end>
    parts = name.rsplit("_", 2)
    if len(parts) == 3:
        repo_id, start, end = parts
        # Only treat as canonical if start/end look like tags/SHAs.
        # This avoids mis-parsing repo_ids that themselves contain underscores.
        if _looks_like_tag_or_sha(start) and _looks_like_tag_or_sha(end):
            return f"{repo_id}:{start}_{end}"

    # Fallback: <repo_id>_<time_span> where time_span may contain hyphens etc.
    if "_" in name:
        repo_id, time_span = name.rsplit("_", 1)
        return f"{repo_id}:{time_span}"

    return name


def select_preprocessed(pattern: str, data_root: Path) -> tuple[Path, str]:
    preprocessed_root = data_root / "github_data" / "preprocessed"
    # Convert "owner_repo:start_end" into the canonical directory name
    # e.g., urllib3_urllib3:2.0.6-2.3.0 -> urllib3_urllib3_2.0.6_2.3.0
    if ":" in pattern:
        try:
            repo_id, time_span = parse_repo_spec(pattern)
            normalized_pattern = f"{repo_id}_{time_span}"
        except Exception:
            # Keep fuzzy matching behavior as a fallback
            normalized_pattern = pattern.replace(":", "_").replace("-", "_", 1)
    else:
        normalized_pattern = pattern.replace(":", "_")
    preprocessed_dir = resolve_entry(preprocessed_root, normalized_pattern, "dir", "preprocessed", False, [])
    return preprocessed_dir, preprocessed_dir_to_repo_id(preprocessed_dir.name)


def find_preprocessed_path(repo_id: str, time_span: str, data_root: Path) -> Path:
    return data_root / "github_data" / "preprocessed" / f"{repo_id}_{time_span}"


def infer_repo_id_from_trial_name(trial_name: str, data_root: Path) -> str | None:
    """Infer repo_id from trial_name by matching against preprocessed directories.

    Trial names typically follow the pattern:
        <owner>_<repo>_<start>_<end>_<suffix>

    This function checks if trial_name starts with any preprocessed directory name
    and extracts the repo_id if found.

    Args:
        trial_name: Trial name (e.g., 'element-hq_element-web_v1.11.95_v1.11.97_baseline_001')
        data_root: Root directory for DATA

    Returns:
        Inferred repo_id (e.g., 'element-hq_element-web:v1.11.95_v1.11.97') or None if not found
    """
    preprocessed_root = data_root / "github_data" / "preprocessed"
    if not preprocessed_root.exists():
        return None

    # Get all preprocessed directory names, sorted by length (longest first)
    # This ensures we match the most specific pattern first
    preprocessed_dirs = sorted(
        [d.name for d in preprocessed_root.iterdir() if d.is_dir()],
        key=len,
        reverse=True,
    )

    # Check if trial_name starts with any preprocessed directory name
    for dir_name in preprocessed_dirs:
        # Trial name should start with dir_name followed by '_' or be exactly dir_name
        if trial_name == dir_name or trial_name.startswith(f"{dir_name}_"):
            return preprocessed_dir_to_repo_id(dir_name)

    return None


def get_python_interpreter(project_root: Path) -> Path:
    venv_python = project_root / ".venv" / "bin" / "python"
    if venv_python.exists():
        return venv_python  # Keep symlink, don't resolve to avoid losing venv context
    return Path(sys.executable)


def git_rev_parse(project_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def expand_tag(tag: str, tag_prefix: str | None, local_repo: Path | None = None) -> str:
    """Expand a simplified tag to its full form using prefix.

    If the tag already exists in the repo (when local_repo is provided), return as-is.
    Otherwise, prepend the tag_prefix if provided.

    Args:
        tag: The tag name (possibly simplified, e.g., "259.241213.0")
        tag_prefix: Prefix to prepend (e.g., "tutanota-release-")
        local_repo: Optional path to local git repository for validation

    Returns:
        Expanded tag name (e.g., "tutanota-release-259.241213.0")
    """
    if not tag_prefix:
        return tag

    # If tag already starts with prefix, return as-is
    if tag.startswith(tag_prefix):
        return tag

    # If local_repo is provided, check if original tag exists
    if local_repo and local_repo.exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(local_repo), "rev-parse", f"refs/tags/{tag}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                # Original tag exists, use it
                return tag
        except Exception:
            pass

    # Prepend prefix
    expanded = f"{tag_prefix}{tag}"

    # Validate expanded tag exists if local_repo is provided
    if local_repo and local_repo.exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(local_repo), "rev-parse", f"refs/tags/{expanded}"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return expanded
            else:
                # Expanded tag doesn't exist either, return original
                print(f"  Warning: Neither '{tag}' nor '{expanded}' found as git tag", file=sys.stderr)
                return tag
        except Exception:
            pass

    return expanded


def resolve_tags_for_codemaat(
    local_repo: Path,
    start_tag: str,
    end_tag: str,
    main_branch: str = "main",
) -> tuple[str, str, bool]:
    """Resolve tags to commit SHAs for codemaat, handling diverged branches.

    If tags are not on the main branch's first-parent chain (diverged scenario),
    compute merge-base commits to use instead.

    Args:
        local_repo: Path to local git repository
        start_tag: Start tag name
        end_tag: End tag name
        main_branch: Main branch name (default: "main")

    Returns:
        Tuple of (resolved_start, resolved_end, is_diverged)
        - resolved_start: start_tag or merge-base SHA
        - resolved_end: end_tag or merge-base SHA
        - is_diverged: True if merge-base was used
    """
    try:
        import pygit2
    except ImportError:
        # pygit2 not available, return original tags
        return start_tag, end_tag, False

    try:
        repo = pygit2.Repository(str(local_repo))

        # Resolve main branch
        main_ref = None
        for ref_name in [f"refs/heads/{main_branch}", f"refs/remotes/origin/{main_branch}"]:
            try:
                main_ref = repo.references[ref_name]
                break
            except KeyError:
                continue

        if not main_ref:
            print(f"  Warning: Cannot find main branch '{main_branch}', using tags as-is")
            return start_tag, end_tag, False

        main_head = main_ref.peel(pygit2.Commit)

        # Helper to resolve tag to commit
        def resolve_tag(tag_name: str) -> pygit2.Commit:
            try:
                ref = repo.references[f"refs/tags/{tag_name}"]
                return ref.peel(pygit2.Commit)
            except KeyError:
                # Try as commit SHA
                return repo.revparse_single(tag_name).peel(pygit2.Commit)

        start_commit = resolve_tag(start_tag)
        end_commit = resolve_tag(end_tag)

        # Build main branch first-parent chain for quick lookup
        main_chain = set()
        c = main_head
        while c:
            main_chain.add(c.id)
            if not c.parents:
                break
            c = c.parents[0]

        # Check if tags are on main's first-parent chain
        start_on_main = start_commit.id in main_chain
        end_on_main = end_commit.id in main_chain

        if start_on_main and end_on_main:
            # Both on main, no need for merge-base
            return start_tag, end_tag, False

        # Diverged scenario: compute merge-base
        print("  ⚠️  Diverged branches detected!")
        print(f"      {start_tag} on main: {start_on_main}")
        print(f"      {end_tag} on main: {end_on_main}")

        start_base_oid = repo.merge_base(main_head.id, start_commit.id)
        end_base_oid = repo.merge_base(main_head.id, end_commit.id)

        if not start_base_oid or not end_base_oid:
            print("  Warning: Cannot compute merge-base, using tags as-is")
            return start_tag, end_tag, False

        resolved_start = str(start_base_oid)
        resolved_end = str(end_base_oid)

        print("  ✓ Using merge-base commits for codemaat:")
        print(f"      start: {start_tag} -> {resolved_start[:12]}")
        print(f"      end:   {end_tag} -> {resolved_end[:12]}")

        return resolved_start, resolved_end, True

    except Exception as e:
        print(f"  Warning: Failed to check diverged status: {e}")
        return start_tag, end_tag, False
