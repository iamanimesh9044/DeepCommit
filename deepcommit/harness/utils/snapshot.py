"""Snapshot creation utilities."""

from typing import Dict, List, Optional, Set

# Root build/config files to include in snapshots.
# These files define workspace/module dependencies and version numbers.
# Including them ensures agent's code is self-consistent (e.g., crate versions
# match root Cargo.toml). SRS constrains agents not to add new external
# dependencies, so base image's pre-installed deps remain sufficient.
ROOT_BUILD_FILES: List[str] = [
    # Rust
    "Cargo.toml",
    "Cargo.lock",
]

# Extra root build files per language, used only by workspace (multi-repo) mode via ``root_build_files_for``.
# Rust stays exactly ``ROOT_BUILD_FILES``.
LANGUAGE_ROOT_BUILD_FILES: Dict[str, List[str]] = {
    "rust": ["Cargo.toml", "Cargo.lock"],
    "go": ["go.mod", "go.sum"],
    "javascript": ["package.json"],
    "typescript": ["package.json", "tsconfig.json"],
    "python": ["pyproject.toml", "uv.lock"],
    "proto": ["buf.yaml", "buf.gen.yaml"],
}


def root_build_files_for(languages: Optional[List[str]] = None) -> List[str]:
    """Root build files for the given languages; ``None`` -> legacy Rust list (single-repo behaviour)."""
    if languages is None:
        return list(ROOT_BUILD_FILES)
    out: List[str] = []
    for lang in languages:
        for f in LANGUAGE_ROOT_BUILD_FILES.get(lang.lower(), []):
            if f not in out:
                out.append(f)
    return out


def get_workspace_snapshot_paths(
    repo_name: str,
    repo_src_dirs: List[str],
    languages: Optional[List[str]] = None,
    existing_root_files: Optional[Set[str]] = None,
    existing_src_dirs: Optional[Set[str]] = None,
    extra_globs: Optional[List[str]] = None,
) -> List[str]:
    """Per-repo snapshot paths (repo-relative; the caller runs ``git archive`` inside ``repo_name``'s checkout).

    ``.proto`` files are added as ``*.proto`` globs when the languages include ``proto``.
    """
    paths = get_snapshot_paths(
        repo_src_dirs, existing_root_files, existing_src_dirs, _root_files=root_build_files_for(languages)
    )
    if languages and any(lang.lower() == "proto" for lang in languages):
        paths.append("*.proto")
    paths.extend(extra_globs or [])
    return paths


def get_snapshot_paths(
    repo_src_dirs: List[str],
    existing_root_files: Optional[Set[str]] = None,
    existing_src_dirs: Optional[Set[str]] = None,
    _root_files: Optional[List[str]] = None,
) -> List[str]:
    """Get all paths to include in snapshot.

    Includes source directories and root build files. Root build files ensure
    version consistency between agent's crate configs and workspace config.

    Args:
        repo_src_dirs: Source directories (e.g., ["src/", "crates/"])
        existing_root_files: Optional set of root build files that exist.
            If provided, only files in this set are included.
            If None, all root build files are included (legacy behavior).
        existing_src_dirs: Optional set of source directories that exist.
            If provided, only directories in this set are included.
            If None, all source directories are included (legacy behavior).

    Returns:
        List of paths for git archive command
    """
    root_files = ROOT_BUILD_FILES if _root_files is None else _root_files
    # Filter source directories if existence check was performed
    if existing_src_dirs is not None:
        paths = [d for d in repo_src_dirs if d in existing_src_dirs]
    else:
        paths = list(repo_src_dirs)

    if existing_root_files is not None:
        # Only include root build files that exist
        for f in root_files:
            if f in existing_root_files:
                paths.append(f)
    else:
        # Legacy behavior: include all root build files
        paths.extend(root_files)

    return paths
