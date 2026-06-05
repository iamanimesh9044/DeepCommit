"""Iteration tree management.

A single "iteration tree" rooted at one Phase 2 trial (= one initial CSV
snapshot) lives under ``DATA/harness_workspace/<repo_range>/`` alongside
its iter directories. All mutable state is in
``<phase2_trial>_iteration_tree.json`` at the range root.

Layout after a few iterations:

    DATA/harness_workspace/django_django_4.2_5.2/
    ├── v1_001_fix/                                  initial iter
    │   ├── milestones.csv.bak  (read-only snapshot)
    │   ├── milestones.csv      (may be mutated by stage1 fix loop)
    │   ├── dependencies.csv.bak
    │   ├── dependencies.csv
    │   └── initial_source.json (provenance back to Phase 2)
    ├── v1_001_fix_iter002/                          parent=v1_001_fix
    │   ├── milestones.csv.bak  (= parent's .csv + Main Agent csv_ops)
    │   ├── milestones.csv      (= .bak + this iter's stage1 fix ops)
    │   └── ...
    └── v1_001_fix_iteration_tree.json               tree state
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .csv_ops import apply_csv_ops

logger = logging.getLogger(__name__)


# =============================================================================
# Tree state
# =============================================================================


class IterationTree:
    """Load/save/query the iteration tree for one phase2_trial."""

    def __init__(self, path: Path, repo_range: str, phase2_trial: str):
        self.path = path
        if self.path.exists():
            self.state = json.loads(self.path.read_text())
        else:
            self.state = {
                "repo_range": repo_range,
                "phase2_trial": phase2_trial,
                "iterations": {},
                "best": None,
                "next_iter_counter": 2,  # counter 1 is the initial
            }

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def save(self) -> None:
        self.path.write_text(json.dumps(self.state, indent=2, ensure_ascii=False))

    # ------------------------------------------------------------------ #
    # Iter registration / updates
    # ------------------------------------------------------------------ #

    def register_iter(
        self,
        iter_name: str,
        parent: Optional[str],
        applied_changes: List[Dict],
        rejected_changes: Optional[List[Dict]] = None,
    ) -> None:
        if iter_name in self.state["iterations"]:
            raise ValueError(f"iter {iter_name!r} already registered")
        self.state["iterations"][iter_name] = {
            "parent": parent,
            "applied_changes": applied_changes,
            "rejected_changes": rejected_changes or [],
            "stage1": None,  # filled by update_iter after stage1 runs
            "module2": None,  # filled after module 2 runs
            "health": None,  # {"healthy": N, "total": N}
            "status": "in_progress",
            "csv_sha256": {},
        }
        self.save()

    def update_iter(self, iter_name: str, **fields: Any) -> None:
        self.state["iterations"][iter_name].update(fields)
        self.save()

    def get_iter(self, iter_name: str) -> Optional[Dict]:
        return self.state["iterations"].get(iter_name)

    # ------------------------------------------------------------------ #
    # Naming / selection
    # ------------------------------------------------------------------ #

    def next_iter_name(self) -> str:
        n = self.state["next_iter_counter"]
        self.state["next_iter_counter"] = n + 1
        self.save()
        return f"{self.state['phase2_trial']}_iter{n:03d}"

    def latest_iter_name(self) -> Optional[str]:
        """Most-recently-registered iter name, or None if tree is empty."""
        names = list(self.state["iterations"].keys())
        return names[-1] if names else None

    def update_best(self) -> None:
        """Pick the iter with the highest ``healthy / total`` ratio as best.

        Ties are broken by insertion order (earlier wins) so repeated
        re-evaluations don't flap between equally-good iters.
        """
        best_name = None
        best_score = -1.0
        for name, info in self.state["iterations"].items():
            health = info.get("health") or {}
            total = health.get("total", 0) or 0
            if total == 0:
                continue
            score = (health.get("healthy", 0) or 0) / total
            if score > best_score:
                best_score = score
                best_name = name
        self.state["best"] = best_name
        self.save()

    # ------------------------------------------------------------------ #
    # Prompt support
    # ------------------------------------------------------------------ #

    def history_summary(self) -> List[Dict]:
        """Flat summary of every iter, for injection into LLM prompts."""
        return [
            {
                "name": name,
                "parent": info.get("parent"),
                "status": info.get("status"),
                "health": info.get("health"),
                "applied_changes_count": len(info.get("applied_changes", [])),
                "rejected_changes_count": len(info.get("rejected_changes", [])),
            }
            for name, info in self.state["iterations"].items()
        ]


# =============================================================================
# Bootstrap + materialize
# =============================================================================


def bootstrap_initial_iter(
    workspace_root: Path,
    repo_range: str,
    phase2_trial: str,
    initial_milestones_csv: Path,
    initial_dependencies_csv: Path,
) -> Tuple[Path, IterationTree]:
    """Create the initial iter dir if it doesn't exist. Returns ``(iter_dir, tree)``.

    Idempotent: on a rerun where the dir already exists, we reuse it and
    skip the copy — the tree is still (re)loaded so the caller gets fresh
    handle. `.bak` files are chmod'd read-only so a manual edit to the
    "immutable" snapshot is a loud failure.
    """
    range_dir = workspace_root / repo_range
    range_dir.mkdir(parents=True, exist_ok=True)

    iter_dir = range_dir / phase2_trial
    tree_path = range_dir / f"{phase2_trial}_iteration_tree.json"
    tree = IterationTree(tree_path, repo_range=repo_range, phase2_trial=phase2_trial)

    if iter_dir.exists():
        logger.info(f"Initial iter already exists: {iter_dir}; reusing")
        return iter_dir, tree

    iter_dir.mkdir(parents=True)

    bak_m = iter_dir / "milestones.csv.bak"
    bak_d = iter_dir / "dependencies.csv.bak"
    shutil.copy2(initial_milestones_csv, bak_m)
    shutil.copy2(initial_dependencies_csv, bak_d)
    os.chmod(bak_m, 0o444)
    os.chmod(bak_d, 0o444)

    live_m = iter_dir / "milestones.csv"
    live_d = iter_dir / "dependencies.csv"
    shutil.copy2(bak_m, live_m)
    shutil.copy2(bak_d, live_d)
    os.chmod(live_m, 0o644)
    os.chmod(live_d, 0o644)

    (iter_dir / "initial_source.json").write_text(
        json.dumps(
            {
                "milestones_csv": str(initial_milestones_csv),
                "dependencies_csv": str(initial_dependencies_csv),
            },
            indent=2,
        )
    )

    if phase2_trial not in tree.state["iterations"]:
        tree.register_iter(phase2_trial, parent=None, applied_changes=[])
    tree.update_iter(phase2_trial, csv_sha256=compute_csv_sha256(iter_dir))

    logger.info(f"Bootstrapped initial iter at {iter_dir}")
    return iter_dir, tree


def materialize_iteration(
    parent_dir: Path,
    new_iter_dir: Path,
    csv_ops: List[Dict],
) -> Tuple[List[Dict], List[Dict]]:
    """Create ``new_iter_dir`` from ``parent_dir`` + ``csv_ops``.

    Steps:
      1. Copy parent's live ``*.csv`` into the new dir (NOT the parent's
         ``.bak`` — stage1 fix ops done in the parent are part of the real
         starting state).
      2. Apply ``csv_ops`` in place.
      3. Freeze the result as the new iter's ``.bak`` (read-only).
      4. Keep a mutable ``.csv`` working copy for stage1 to edit.

    Returns ``(applied, rejected)`` from ``apply_csv_ops``.
    """
    if new_iter_dir.exists():
        raise FileExistsError(f"new_iter_dir already exists: {new_iter_dir}")

    new_iter_dir.mkdir(parents=True)

    for name in ("milestones.csv", "dependencies.csv"):
        src = parent_dir / name
        if not src.exists():
            raise FileNotFoundError(f"parent missing {name}: {parent_dir}")
        shutil.copy2(src, new_iter_dir / name)

    applied, rejected = apply_csv_ops(new_iter_dir, csv_ops)

    # Freeze as .bak (= "before stage1 ran").
    for name in ("milestones.csv", "dependencies.csv"):
        live = new_iter_dir / name
        bak = new_iter_dir / f"{name}.bak"
        shutil.copy2(live, bak)
        os.chmod(bak, 0o444)

    logger.info(f"Materialized {new_iter_dir.name}: {len(applied)} ops applied, {len(rejected)} rejected")
    return applied, rejected


# =============================================================================
# Hashing (drift detection)
# =============================================================================


def compute_csv_sha256(iter_dir: Path) -> Dict[str, str]:
    """16-char SHA256 prefix for each CSV, useful for detecting external drift."""
    out: Dict[str, str] = {}
    for name in ("milestones.csv", "dependencies.csv"):
        p = iter_dir / name
        if p.exists():
            out[name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return out
