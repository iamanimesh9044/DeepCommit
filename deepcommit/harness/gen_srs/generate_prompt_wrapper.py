#!/usr/bin/env python3
"""
Minimal wrapper to reuse SRSCodeGenerator.generate_prompt().

This skips all Docker work: it only builds the prompt text from an SRS file
and prints it (or writes to a file). No log/ directory is created.

Usage:
  python harness/gen_srs/generate_prompt_wrapper.py \
    --srs-path path/to/SRS.md \
    --milestone-id M001 \
    --repo-name urllib3_urllib3_2.0.6_2.3.0

Optional:
  --output-dir /tmp/prompt_logs    # working dir (wrapper keeps auto_create_logs off)
  --repo-config config/xxx.yaml    # override repo config path
  --repo-src-dirs src/ urllib3/    # overrides repo_config
  --out prompt.txt                 # save prompt to a file (also prints to stdout)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from run_srs_code_gen import SRSCodeGenerator


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate prompt text from SRS without running Docker.")
    parser.add_argument("--srs-path", required=True, help="Path to SRS.md")
    parser.add_argument(
        "--output-dir",
        default="srs_prompt_output",
        help="Working/output dir (wrapper does not create log/ by default)",
    )
    parser.add_argument("--milestone-id", default="M000", help="Milestone ID used inside the prompt/tag text")
    parser.add_argument(
        "--repo-name",
        help="Repo name to auto-derive config/<repo_name>.yaml (mirrors run_srs_code_gen behavior)",
    )
    parser.add_argument("--repo-config", help="Repo config YAML to load repo_src_dirs (optional)")
    parser.add_argument(
        "--repo-src-dirs",
        nargs="+",
        default=None,
        help="Override source dirs for the prompt (default: from config or ['src/'])",
    )
    parser.add_argument("--out", help="Optional output file to save prompt")
    args = parser.parse_args()

    srs_path = Path(args.srs_path)
    if not srs_path.is_file():
        print(f"Error: SRS file not found: {srs_path}", file=sys.stderr)
        return 1

    # Auto-derive repo_config from repo_name if not explicitly provided
    repo_config = args.repo_config
    if not repo_config and args.repo_name:
        repo_config = f"config/{args.repo_name}.yaml"
        print(f"Auto-derived repo-config: {repo_config}")

    # Instantiate with minimal required fields; image/container are unused here.
    generator = SRSCodeGenerator(
        srs_path=str(srs_path),
        image_name="local",
        milestone_id=args.milestone_id,
        output_dir=args.output_dir,
        repo_config=repo_config,
        repo_src_dirs=args.repo_src_dirs,
        auto_create_logs=False,
    )

    prompt = generator.generate_prompt()

    # Always echo to stdout for quick use.
    print(prompt)

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(prompt, encoding="utf-8")
        generator.logger.info(f"Prompt saved to: {out_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
