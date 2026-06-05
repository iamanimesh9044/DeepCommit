#!/usr/bin/env python3
"""
Export git diffs for milestones, including ENV patches and milestone changes.

This script exports:
1. START ENV patches: original commit_sha_start → current tag_name_start
2. END ENV patches: original commit_sha_end → current tag_name_end
3. Milestone diff: tag_name_start → tag_name_end (actual milestone changes)
"""

import json
import subprocess
import sys
from pathlib import Path


def export_tag_patches(milestone_id: str, metadata_path: str, output_dir: Path, image_name: str = None) -> bool:
    """
    Export tag patches for a milestone from Docker container (only if tags were moved):
    1. START tag patches: original commit_sha_start → current tag_name_start
    2. END tag patches: original commit_sha_end → current tag_name_end

    Note: Milestone diff (start → end) is NOT exported as it's redundant - users can
    run `git diff milestone-M001-start milestone-M001-end` directly if needed.

    This uses the metadata.json to get the original commit SHA and runs
    git commands inside the Docker container to get the actual tag positions.

    Args:
        milestone_id: Milestone ID (e.g., "M001")
        metadata_path: Path to metadata.json file
        output_dir: Directory to save diff files
        image_name: Docker image name (optional, will be constructed if not provided)

    Returns:
        True if diff export succeeded, False otherwise
    """
    try:
        if not Path(metadata_path).exists():
            print(f"⚠️  metadata.json not found at {metadata_path}")
            return False

        # Load metadata
        with open(metadata_path) as f:
            metadata = json.load(f)

        # Find this milestone
        milestone_data = None
        for m in metadata["milestones"]:
            if m["id"] == milestone_id:
                milestone_data = m
                break

        if not milestone_data:
            print(f"⚠️  Milestone {milestone_id} not found in metadata.json")
            return False

        original_start_sha = milestone_data["commit_sha_start"]
        original_end_sha = milestone_data["commit_sha_end"]
        start_tag = milestone_data["tag_name_start"]
        end_tag = milestone_data["tag_name_end"]

        output_dir.mkdir(parents=True, exist_ok=True)

        # Construct image name if not provided
        if image_name is None:
            repo_name = metadata.get("repo_name", "").replace("/", "_")
            # Try to infer test scenario from metadata path
            # Assume metadata is in: .../test_scenario_name/metadata.json
            metadata_parent = Path(metadata_path).parent.name
            image_name = f"{repo_name}/{metadata_parent}/{milestone_id.lower()}:latest"

        # Helper function to run git commands in Docker container
        def run_git_in_docker(git_cmd: str) -> tuple[bool, str]:
            """Run git command inside Docker container"""
            # Use --init to properly reap zombie child processes
            full_cmd = f'docker run --rm --init {image_name} bash -c "cd /testbed && {git_cmd}"'
            result = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=60)
            return result.returncode == 0, result.stdout

        # Get current tag SHAs from Docker container
        success, current_start_sha = run_git_in_docker(f"git rev-parse {start_tag}")
        if not success:
            print(f"⚠️  Tag {start_tag} not found in Docker container")
            return False
        current_start_sha = current_start_sha.strip()

        success, current_end_sha = run_git_in_docker(f"git rev-parse {end_tag}")
        if not success:
            print(f"⚠️  Tag {end_tag} not found in Docker container")
            return False
        current_end_sha = current_end_sha.strip()

        print(f"\n{'=' * 70}")
        print(f"📦 {milestone_id}: {milestone_data['title']}")
        print(f"{'=' * 70}")

        # 1. Export START ENV patches (if tag was moved)
        if original_start_sha != current_start_sha:
            print(f"📝 Exporting START ENV patches ({original_start_sha[:8]} → {start_tag})...")

            # Save env patch diff from Docker container
            success, diff_output = run_git_in_docker(f"git diff {original_start_sha} {start_tag}")
            if success:
                env_patch_file = output_dir / f"{milestone_id}_start_env_patches.diff"
                env_patch_file.write_text(diff_output)
            else:
                print("⚠️  Failed to export START diff")
                return False

            # Save commit log from Docker container
            success, log_output = run_git_in_docker(f"git log --oneline {original_start_sha}..{start_tag}")
            if success and log_output.strip():
                env_log_file = output_dir / f"{milestone_id}_start_env_patches_commits.txt"
                env_log_file.write_text(log_output)

            print(f"✅ START ENV patches exported to {env_patch_file.name}")
            if success and log_output.strip():
                print(f"   Commits: {env_log_file.name}")
        else:
            print("✅ No START ENV patches (tag at original position)")

        # 2. Export END ENV patches (if tag was moved)
        if original_end_sha != current_end_sha:
            print(f"📝 Exporting END ENV patches ({original_end_sha[:8]} → {end_tag})...")

            # Save env patch diff from Docker container
            success, diff_output = run_git_in_docker(f"git diff {original_end_sha} {end_tag}")
            if success:
                env_patch_file = output_dir / f"{milestone_id}_end_env_patches.diff"
                env_patch_file.write_text(diff_output)
            else:
                print("⚠️  Failed to export END diff")
                return False

            # Save commit log from Docker container
            success, log_output = run_git_in_docker(f"git log --oneline {original_end_sha}..{end_tag}")
            if success and log_output.strip():
                env_log_file = output_dir / f"{milestone_id}_end_env_patches_commits.txt"
                env_log_file.write_text(log_output)

            print(f"✅ END ENV patches exported to {env_patch_file.name}")
            if success and log_output.strip():
                print(f"   Commits: {env_log_file.name}")
        else:
            print("✅ No END ENV patches (tag at original position)")

        # Note: Milestone diff (start → end) is NOT exported
        # Users can run: git diff milestone-{ID}-start milestone-{ID}-end
        print()

        return True

    except subprocess.TimeoutExpired:
        print(f"⚠️  Diff export timed out for {milestone_id}")
        print()
        return False
    except subprocess.CalledProcessError as e:
        print(f"⚠️  Diff export failed for {milestone_id}: {e.stderr if e.stderr else str(e)}")
        print()
        return False
    except Exception as e:
        print(f"❌ Diff export error for {milestone_id}: {e}")
        print()
        return False


def main():
    if len(sys.argv) < 3:
        print("Usage: python show_env_patches.py <metadata.json> <output_dir> [milestone_id]")
        print("\nExamples:")
        print("  # Export all milestones")
        print("  python show_env_patches.py metadata.json ./output")
        print()
        print("  # Export specific milestone")
        print("  python show_env_patches.py metadata.json ./output M003")
        sys.exit(1)

    metadata_path = sys.argv[1]
    output_dir = Path(sys.argv[2])
    milestone_id = sys.argv[3] if len(sys.argv) > 3 else None

    # Load metadata to get milestone list
    with open(metadata_path) as f:
        metadata = json.load(f)

    milestones = metadata["milestones"]

    # Filter to specific milestone if requested
    if milestone_id:
        milestones = [m for m in milestones if m["id"] == milestone_id]
        if not milestones:
            print(f"❌ Milestone {milestone_id} not found")
            sys.exit(1)

    # Export diffs for each milestone
    success_count = 0
    fail_count = 0

    for m in milestones:
        m_id = m["id"]
        milestone_output_dir = output_dir / m_id

        if export_tag_patches(m_id, metadata_path, milestone_output_dir):
            success_count += 1
        else:
            fail_count += 1

    # Summary
    print("=" * 70)
    print("📊 Export Summary")
    print("=" * 70)
    print(f"Total: {len(milestones)}")
    print(f"✅ Success: {success_count}")
    print(f"❌ Failed: {fail_count}")
    print()

    if fail_count > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
