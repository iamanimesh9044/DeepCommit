#!/usr/bin/env python3
"""
Generate simplified DAG PNG from enhanced JSON file.

Usage:
    python scripts/generate_dag_png.py <enhanced_json_path>
    python scripts/generate_dag_png.py DATA/github_data/preprocessed/navidrome_navidrome_v0.57.0_v0.58.0/dag/commit_dag_v3_ctx_3_pctx_3_enhanced.json

Options:
    --full    Generate full DAG with all nodes (default: only nodes with edges)
    --output  Custom output path for PNG (default: same dir as input, named commit_dag_simplified.png)
"""

import json
import subprocess
import sys
from pathlib import Path


def generate_dag_png(enhanced_json_path: str, full: bool = False, output_path: str = None):
    """Generate DAG PNG from enhanced JSON file."""
    enhanced_file = Path(enhanced_json_path)

    if not enhanced_file.exists():
        print(f"Error: File not found: {enhanced_file}")
        return 1

    with open(enhanced_file) as f:
        data = json.load(f)

    dag_dir = enhanced_file.parent

    # Determine which nodes to include
    if full:
        nodes_to_show = set(data["nodes"].keys())
        suffix = "full"
    else:
        # Only nodes with edges
        nodes_to_show = set()
        for edge in data.get("edges_dag", []):
            nodes_to_show.add(edge["source"])
            nodes_to_show.add(edge["target"])
        suffix = "simplified"

    # Generate DOT file
    dot_lines = ["digraph commit_dag {"]
    dot_lines.append("  rankdir=TB;")
    dot_lines.append('  node [shape=box, style="rounded,filled", fillcolor="#e8f5e9", fontsize=9];')
    dot_lines.append("  edge [fontsize=8];")
    dot_lines.append("  nodesep=0.3;")
    dot_lines.append("  ranksep=0.5;")
    dot_lines.append("")

    # Add nodes
    for sha in nodes_to_show:
        node = data["nodes"].get(sha, {})
        short_sha = sha[:7]
        subject = node.get("subject", "")[:30].replace('"', '\\"').replace("\n", " ")
        layer = node.get("layer", 0)
        label = f"{short_sha}\\n{subject}..."

        # Color by layer
        colors = ["#c8e6c9", "#fff9c4", "#bbdefb", "#f8bbd9", "#e1bee7"]
        color = colors[layer % len(colors)]
        dot_lines.append(f'  "{sha}" [label="{label}", fillcolor="{color}"];')

    dot_lines.append("")

    # Add edges
    for edge in data.get("edges_dag", []):
        src = edge["source"]
        tgt = edge["target"]
        if src in nodes_to_show and tgt in nodes_to_show:
            edge_type = edge.get("types", [""])[0][:10]
            dot_lines.append(f'  "{src}" -> "{tgt}" [label="{edge_type}"];')

    dot_lines.append("}")

    # Write DOT file
    dot_file = dag_dir / f"commit_dag_{suffix}.dot"
    with open(dot_file, "w") as f:
        f.write("\n".join(dot_lines))

    # Generate PNG
    if output_path:
        png_file = Path(output_path)
    else:
        png_file = dag_dir / f"commit_dag_{suffix}.png"

    try:
        subprocess.run(["dot", "-Tpng", str(dot_file), "-o", str(png_file)], check=True, capture_output=True)
    except FileNotFoundError:
        print("Error: graphviz 'dot' command not found. Please install graphviz.")
        return 1
    except subprocess.CalledProcessError as e:
        print(f"Error generating PNG: {e.stderr.decode()}")
        return 1

    # Print summary
    print(f"Project: {dag_dir.parent.name}")
    print(f"Total nodes: {len(data['nodes'])}")
    print(f"Total edges: {len(data.get('edges_dag', []))}")
    print(f"Nodes shown: {len(nodes_to_show)}")
    print(f"DOT file: {dot_file}")
    print(f"PNG file: {png_file}")

    return 0


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Generate simplified DAG PNG from enhanced JSON file.")
    parser.add_argument(
        "enhanced_json", help="Path to enhanced JSON file (e.g., commit_dag_v3_ctx_3_pctx_3_enhanced.json)"
    )
    parser.add_argument(
        "--full", action="store_true", help="Generate full DAG with all nodes (default: only nodes with edges)"
    )
    parser.add_argument("--output", "-o", help="Custom output path for PNG")

    args = parser.parse_args()

    return generate_dag_png(args.enhanced_json, args.full, args.output)


if __name__ == "__main__":
    sys.exit(main())
