#!/usr/bin/env python3
"""
Minimal Milestone DAG visualization server.

Serves the mstone React SPA and provides data-scanning APIs
for milestones.csv / dependencies.csv files.

Usage:
    python visualization/app.py
    # → http://localhost:5005/mstone
"""

import os
import re
import csv
import hashlib
from pathlib import Path
from flask import Flask, jsonify, send_from_directory, request, Response
from flask_cors import CORS
import markdown

app = Flask(__name__, static_folder="static")
CORS(app)

BASE_DIR = Path(__file__).parent
PROJECT_ROOT = BASE_DIR.parent
DATA_DIR = PROJECT_ROOT / "DATA"

# ── Configurable data roots ──────────────────────────────────────────────
HARNESS_WORKSPACE_PATH = Path(os.environ.get(
    "HARNESS_WORKSPACE_PATH", str(DATA_DIR / "harness_workspace")))
HARNESS_TRIAL_SPACE_PATH = Path(os.environ.get(
    "HARNESS_TRIAL_SPACE_PATH", str(DATA_DIR / "harness_trial_space")))
RESULT_PATH = Path(os.environ.get(
    "RESULT_PATH", str(DATA_DIR / "result")))
TRIAL_SPACE_PATH = Path(os.environ.get(
    "TRIAL_SPACE_PATH", str(DATA_DIR / "trial_space")))

_CANDIDATE_ROOTS = [
    ("harness_trial_space", HARNESS_TRIAL_SPACE_PATH),
    ("harness_workspace", HARNESS_WORKSPACE_PATH),
    ("trial_space", TRIAL_SPACE_PATH),
]
DATA_ROOTS: list[tuple[str, Path]] = [(n, p) for n, p in _CANDIDATE_ROOTS if p.exists()]
if not DATA_ROOTS:
    DATA_ROOTS = _CANDIDATE_ROOTS[:1]

MSTONE_DIST_DIR = BASE_DIR / "static" / "mstone_dist"


# ── Page routes ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    links = [("/mstone", "Milestone DAG"), ("/prompts", "Prompt Viewer"),
             ("/spec", "Specs"), ("/graphify", "Graphify")]
    items = " &middot; ".join(f'<a href="{u}">{n}</a>' for u, n in links)
    return (
        '<!doctype html><meta charset="utf-8"><title>DeepCommit</title>'
        '<style>body{font-family:system-ui;display:flex;justify-content:center;'
        'align-items:center;height:100vh;gap:1.5rem;font-size:1.1rem}'
        'a{text-decoration:none;color:#2d2d5e}a:hover{text-decoration:underline}</style>'
        f'<div>{items}</div>'
    )


@app.route("/mstone")
@app.route("/mstone/")
def mstone_index():
    """Serve the mstone SPA index.html with the highlight-toggle script injected.

    The injected script (`/static/mstone_highlight.js`) adds a toolbar
    toggle that dims unrelated milestones and highlights both the selected
    milestones and their prerequisite-only closure nodes, without filtering
    anything out. Activates only when `selected_milestone_ids.txt` exists
    for the current dataset.
    """
    html = (MSTONE_DIST_DIR / "index.html").read_text()
    snippet = '<script src="/static/mstone_highlight.js?v=non-graded-1" defer></script>'
    if "</body>" in html and snippet not in html:
        html = html.replace("</body>", f"  {snippet}\n  </body>", 1)
    return Response(html, mimetype="text/html")


@app.route("/mstone/<path:filename>")
def mstone_static(filename):
    return send_from_directory(MSTONE_DIST_DIR, filename)


# ── Graphify routes ──────────────────────────────────────────────────────

GRAPHIFY_DIR = PROJECT_ROOT / "graphify-out"


@app.route("/graphify")
@app.route("/graphify/")
def graphify_index():
    return send_from_directory(GRAPHIFY_DIR, "graph.html")


@app.route("/graphify/<path:filename>")
def graphify_static(filename):
    return send_from_directory(GRAPHIFY_DIR, filename)


# ── HTML spec routes ─────────────────────────────────────────────────────

SPEC_DIR = PROJECT_ROOT / "docs" / "spec"


@app.route("/spec")
@app.route("/spec/")
def spec_index():
    """Auto-generated index of all HTML specs under docs/spec/."""
    if not SPEC_DIR.exists():
        return "<h1>No specs yet</h1><p>docs/spec/ does not exist.</p>", 404
    rows = []
    for html in sorted(SPEC_DIR.rglob("*.html")):
        rel = html.relative_to(SPEC_DIR)
        rows.append(f'<li><a href="/spec/{rel}">{rel}</a></li>')
    body = "\n".join(rows) or "<li><em>(no .html specs yet)</em></li>"
    return (
        '<!doctype html><meta charset="utf-8"><title>Specs</title>'
        '<style>body{font-family:system-ui;max-width:60ch;margin:3rem auto;padding:1rem}'
        'li{margin:.3rem 0}a{text-decoration:none}a:hover{text-decoration:underline}</style>'
        f'<h1>DeepCommit HTML Specs</h1><ul>{body}</ul>'
    )


@app.route("/spec/<path:filename>")
def spec_static(filename):
    return send_from_directory(SPEC_DIR, filename)


# ── Prompt template viewer ───────────────────────────────────────────────

PROMPTS_DIR = PROJECT_ROOT / "deepcommit" / "harness" / "prepare_images" / \
    "build_milestone_image" / "prompts"

_MD_EXTENSIONS = ["tables", "fenced_code", "toc", "codehilite", "sane_lists"]


def _resolve_includes(text: str, base_dir: Path, seen: set[str] | None = None) -> list[tuple[str, str]]:
    if seen is None:
        seen = set()
    sections: list[tuple[str, str]] = []
    parts = re.split(r'\{%-?\s*include\s+"([^"]+)"\s*-?%\}', text)
    for i, part in enumerate(parts):
        if i % 2 == 0:
            stripped = part.strip()
            if stripped:
                sections.append(("", stripped))
        else:
            inc_name = part
            if inc_name in seen:
                sections.append((inc_name, f"*[circular include: {inc_name}]*"))
                continue
            inc_path = base_dir / inc_name
            if not inc_path.exists():
                for sub in ("_partials", "languages", "repos"):
                    candidate = base_dir / sub / inc_name
                    if candidate.exists():
                        inc_path = candidate
                        break
            if inc_path.exists():
                seen.add(inc_name)
                child_sections = _resolve_includes(inc_path.read_text(), base_dir, seen)
                sections.append((inc_name, None))
                sections.extend(child_sections)
                sections.append((f"/{inc_name}", None))
            else:
                sections.append((inc_name, f"*[file not found: {inc_name}]*"))
    return sections


def _escape_jinja_vars(text: str) -> str:
    return re.sub(r'\{\{(.+?)\}\}', r'<code class="jinja-var">{{\1}}</code>', text)


def _strip_jinja_blocks(text: str) -> str:
    text = re.sub(r'\{%-?\s*if\s+.+?-?%\}', '', text)
    text = re.sub(r'\{%-?\s*else\s*-?%\}', '', text)
    text = re.sub(r'\{%-?\s*endif\s*-?%\}', '', text)
    text = re.sub(r'\{%-?\s*for\s+.+?-?%\}', '', text)
    text = re.sub(r'\{%-?\s*endfor\s*-?%\}', '', text)
    text = re.sub(r'\{%-?\s*set\s+.+?-?%\}', '', text)
    text = re.sub(r'\{#.*?#\}', '', text, flags=re.DOTALL)
    return text


PROMPT_PAGE_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       color: #d4d4dc; background: #0d1117; }
.layout { display: flex; min-height: 100vh; }
.sidebar { width: 260px; background: #010409; color: #c8c8d4; padding: 1.2rem 0;
           position: fixed; top: 0; left: 0; bottom: 0; overflow-y: auto;
           border-right: 1px solid #30363d; }
.sidebar h2 { color: #e6edf3; font-size: .95rem; padding: .6rem 1.2rem; margin-bottom: .3rem;
              letter-spacing: .04em; text-transform: uppercase; }
.sidebar a { display: block; padding: .45rem 1.2rem; color: #8b949e; text-decoration: none;
             font-size: .85rem; border-left: 3px solid transparent; transition: all .15s; }
.sidebar a:hover { color: #e6edf3; background: rgba(177,186,196,.08); }
.sidebar a.active { color: #6ee7b7; border-left-color: #6ee7b7; background: rgba(110,231,183,.10); }
.sidebar .sep { border-top: 1px solid #30363d; margin: .6rem 1.2rem; }
.sidebar .sub { padding-left: 2rem; font-size: .8rem; color: #6e7681; }
.main { margin-left: 260px; flex: 1; padding: 2rem 2.5rem; max-width: 960px; }
.main h1 { font-size: 1.5rem; margin-bottom: .3rem; color: #e6edf3; }
.main .subtitle { color: #8b949e; margin-bottom: 1.5rem; font-size: .9rem; }
.module-banner { background: linear-gradient(135deg, #1f6feb 0%, #388bfd 100%);
                 color: #f0f6fc; padding: .5rem 1rem; border-radius: 6px; margin: 1.8rem 0 .5rem;
                 font-family: monospace; font-size: .85rem; font-weight: 600;
                 display: flex; align-items: center; gap: .5rem;
                 box-shadow: 0 0 0 1px rgba(56,139,253,.4); }
.module-banner.end { background: transparent; color: #6e7681; font-weight: 400;
                     border: 1px dashed #30363d; margin-top: .2rem; box-shadow: none; }
.content { line-height: 1.7; color: #d4d4dc; }
.content h1 { font-size: 1.4rem; margin: 1.5rem 0 .5rem; padding-bottom: .3rem;
              border-bottom: 2px solid #30363d; color: #e6edf3; }
.content h2 { font-size: 1.2rem; margin: 1.3rem 0 .4rem; color: #79c0ff; }
.content h3 { font-size: 1.05rem; margin: 1rem 0 .3rem; color: #a5d6ff; }
.content h4 { font-size: .95rem; margin: .8rem 0 .2rem; color: #c9d1d9; }
.content p { margin: .5rem 0; }
.content a { color: #58a6ff; text-decoration: none; }
.content a:hover { text-decoration: underline; }
.content ul, .content ol { margin: .4rem 0 .4rem 1.5rem; }
.content li { margin: .2rem 0; }
.content code { background: #161b22; padding: .15rem .35rem; border-radius: 3px;
                font-size: .88em; color: #ff7b72; border: 1px solid #30363d; }
.content pre { background: #161b22; color: #c9d1d9; padding: 1rem; border-radius: 6px;
               overflow-x: auto; margin: .6rem 0; font-size: .85rem; line-height: 1.5;
               border: 1px solid #30363d; }
.content pre code { background: transparent; color: inherit; padding: 0; border: none; }
.content table { border-collapse: collapse; margin: .6rem 0; width: 100%; font-size: .88rem; }
.content th, .content td { border: 1px solid #30363d; padding: .45rem .7rem; text-align: left; }
.content th { background: #161b22; font-weight: 600; color: #e6edf3; }
.content tr:nth-child(even) td { background: rgba(110,118,129,.05); }
.content blockquote { border-left: 3px solid #6ee7b7; padding: .3rem .8rem;
                      margin: .5rem 0; background: rgba(110,231,183,.08); color: #d4d4dc; }
.content strong { color: #e6edf3; }
.content em { color: #c9d1d9; }
.content hr { border: none; border-top: 1px solid #30363d; margin: 1rem 0; }
.jinja-var { background: rgba(245,194,68,.15) !important; color: #f5c244 !important;
             border: 1px solid rgba(245,194,68,.4); border-radius: 3px; padding: .1rem .3rem;
             font-size: .85em; font-family: monospace; }
.nav-top { margin-bottom: 1.5rem; }
.nav-top a { color: #58a6ff; text-decoration: none; font-size: .85rem; }
.nav-top a:hover { text-decoration: underline; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: #0d1117; }
::-webkit-scrollbar-thumb { background: #30363d; border-radius: 5px; }
::-webkit-scrollbar-thumb:hover { background: #484f58; }
"""


def _build_sidebar_html(files: list[tuple[str, str]], active: str) -> str:
    items = ['<h2>Prompts</h2>']
    cls = "active" if active == "composed" else ""
    items.append(f'<a href="/prompts/composed" class="{cls}">Full Composed View</a>')
    items.append('<div class="sep"></div>')

    items.append(f'<a href="/prompts/base.md.j2" class="{"active" if active == "base.md.j2" else ""}">base.md.j2 (entry)</a>')
    items.append('<div class="sep"></div>')

    categories = {"_partials": [], "languages": [], "repos": [], "top": []}
    for fname, display in files:
        if fname == "base.md.j2":
            continue
        if fname.startswith("_partials/"):
            categories["_partials"].append((fname, display))
        elif fname.startswith("languages/"):
            categories["languages"].append((fname, display))
        elif fname.startswith("repos/"):
            categories["repos"].append((fname, display))
        else:
            categories["top"].append((fname, display))

    for fname, display in categories["top"]:
        cls = "active" if active == fname else ""
        items.append(f'<a href="/prompts/{fname}" class="{cls}">{display}</a>')

    if categories["_partials"]:
        items.append('<div class="sep"></div>')
        items.append('<a class="sub" style="pointer-events:none">_partials/</a>')
        for fname, display in categories["_partials"]:
            cls = "active" if active == fname else ""
            items.append(f'<a href="/prompts/{fname}" class="sub {cls}">{display.split("/")[-1]}</a>')

    if categories["languages"]:
        items.append('<div class="sep"></div>')
        items.append('<a class="sub" style="pointer-events:none">languages/</a>')
        for fname, display in categories["languages"]:
            cls = "active" if active == fname else ""
            items.append(f'<a href="/prompts/{fname}" class="sub {cls}">{display.split("/")[-1]}</a>')

    if categories["repos"]:
        items.append('<div class="sep"></div>')
        items.append('<a class="sub" style="pointer-events:none">repos/</a>')
        for fname, display in categories["repos"]:
            cls = "active" if active == fname else ""
            items.append(f'<a href="/prompts/{fname}" class="sub {cls}">{display.split("/")[-1]}</a>')

    return "\n".join(items)


def _list_prompt_files() -> list[tuple[str, str]]:
    files = []
    for p in sorted(PROMPTS_DIR.rglob("*.j2")):
        rel = str(p.relative_to(PROMPTS_DIR))
        files.append((rel, rel))
    return files


def _render_prompt_page(title: str, subtitle: str, body_html: str, active: str) -> str:
    files = _list_prompt_files()
    sidebar = _build_sidebar_html(files, active)
    return f"""<!doctype html><html><head>
<meta charset="utf-8"><title>{title} - DeepCommit Prompts</title>
<style>{PROMPT_PAGE_CSS}</style>
</head><body>
<div class="layout">
<nav class="sidebar">{sidebar}</nav>
<div class="main">
<h1>{title}</h1>
<div class="subtitle">{subtitle}</div>
<div class="content">{body_html}</div>
</div></div></body></html>"""


@app.route("/prompts")
@app.route("/prompts/")
def prompts_index():
    return '<meta http-equiv="refresh" content="0; url=/prompts/composed">'


@app.route("/prompts/composed")
def prompts_composed():
    if not PROMPTS_DIR.exists():
        return "<h1>Prompts directory not found</h1>", 404
    base_path = PROMPTS_DIR / "base.md.j2"
    if not base_path.exists():
        return "<h1>base.md.j2 not found</h1>", 404

    sections = _resolve_includes(base_path.read_text(), PROMPTS_DIR)
    html_parts = []
    for label, content in sections:
        if content is None:
            if label.startswith("/"):
                html_parts.append(
                    f'<div class="module-banner end">end include: {label[1:]}</div>')
            else:
                html_parts.append(
                    f'<div class="module-banner" id="mod-{label.replace("/", "-")}">'
                    f'&#9654; include: {label}</div>')
            continue
        cleaned = _strip_jinja_blocks(content)
        rendered = markdown.markdown(cleaned, extensions=_MD_EXTENSIONS)
        rendered = _escape_jinja_vars(rendered)
        if label:
            html_parts.append(
                f'<div class="module-banner" id="mod-{label.replace("/", "-")}">'
                f'&#9654; {label}</div>')
        html_parts.append(rendered)

    return _render_prompt_page(
        "Full Composed Prompt",
        "base.md.j2 with all includes resolved. Yellow highlights are Jinja2 variables. "
        "Blue banners mark module boundaries.",
        "\n".join(html_parts),
        "composed",
    )


@app.route("/prompts/<path:filename>")
def prompts_single(filename):
    file_path = PROMPTS_DIR / filename
    if not file_path.exists() or not file_path.is_file():
        return f"<h1>Not found: {filename}</h1>", 404
    raw = file_path.read_text()
    cleaned = _strip_jinja_blocks(raw)

    include_pattern = re.compile(r'\{%-?\s*include\s+"([^"]+)"\s*-?%\}')
    def replace_include(m):
        inc = m.group(1)
        return f'\n\n> **Include:** [{inc}](/prompts/{inc})\n\n'
    cleaned = include_pattern.sub(replace_include, cleaned)

    rendered = markdown.markdown(cleaned, extensions=_MD_EXTENSIONS)
    rendered = _escape_jinja_vars(rendered)
    return _render_prompt_page(
        filename,
        f"deepcommit/harness/prepare_images/build_milestone_image/prompts/{filename}",
        rendered,
        filename,
    )


# ── Data scanning helpers ────────────────────────────────────────────────

def _parse_range_name(range_name: str) -> tuple[str, str]:
    """Parse directory name into (group_key, variant_suffix).

    e.g. 'apache_dubbo_dubbo-3.3.3_dubbo-3.3.6_baseline' →
         ('apache/dubbo (dubbo-3.3.3 → dubbo-3.3.6)', 'baseline')
    """
    parts = range_name.split("_")
    if len(parts) >= 4:
        owner, repo = parts[0], parts[1]
        versions = []
        version_end_idx = 2
        for i, part in enumerate(parts[2:], start=2):
            if any(c.isdigit() for c in part) and \
               ("." in part or part.isdigit() or part.startswith("v")):
                versions.append(part)
                version_end_idx = i + 1
            if len(versions) >= 2:
                break
        if len(versions) >= 2:
            group_key = f"{owner}/{repo} ({versions[0]} → {versions[1]})"
            suffix = "_".join(parts[version_end_idx:])
            return group_key, suffix
    return range_name, ""


def _find_csv_pair(subdir: Path):
    """Find milestones.csv + dependencies.csv in subdir or subdir/result/."""
    for candidate in [subdir, subdir / "result"]:
        m = candidate / "milestones.csv"
        d = candidate / "dependencies.csv"
        if m.exists() and d.exists():
            return m, d
    return None


def _scan_flat_dir(root_dir: Path, root_key: str) -> dict:
    """Scan flat layout: root/{range_trial}/[result/]milestones.csv"""
    groups: dict[str, dict] = {}
    for subdir in sorted(root_dir.iterdir()):
        if not subdir.is_dir() or subdir.name.startswith("."):
            continue
        pair = _find_csv_pair(subdir)
        if not pair:
            continue
        m_file, d_file = pair
        csv_dir = m_file.parent
        rel = csv_dir.relative_to(root_dir)
        group_key, variant = _parse_range_name(subdir.name)
        variant_name = variant or "default"
        if group_key not in groups:
            groups[group_key] = {"id": group_key, "name": group_key, "variants": []}
        groups[group_key]["variants"].append({
            "id": str(rel),
            "name": variant_name,
            "basePath": f"/DATA/{root_key}/{rel}",
            "milestonesSize": m_file.stat().st_size,
            "dependenciesSize": d_file.stat().st_size,
            "mtime": m_file.stat().st_mtime,
        })
    return groups


def _scan_harness_dir(root_dir: Path, root_key: str) -> dict:
    """Scan two-level layout: root/{range}/{trial}/milestones.csv"""
    groups: dict[str, dict] = {}
    for range_dir in sorted(root_dir.iterdir()):
        if not range_dir.is_dir() or range_dir.name.startswith("."):
            continue
        group_key, _ = _parse_range_name(range_dir.name)
        for trial_dir in sorted(range_dir.iterdir()):
            if not trial_dir.is_dir():
                continue
            pair = _find_csv_pair(trial_dir)
            if not pair:
                continue
            m_file, d_file = pair
            csv_dir = m_file.parent
            rel = csv_dir.relative_to(root_dir)
            if group_key not in groups:
                groups[group_key] = {"id": group_key, "name": group_key, "variants": []}
            groups[group_key]["variants"].append({
                "id": f"{range_dir.name}/{trial_dir.name}",
                "name": trial_dir.name,
                "basePath": f"/DATA/{root_key}/{rel}",
                "milestonesSize": m_file.stat().st_size,
                "dependenciesSize": d_file.stat().st_size,
                "mtime": m_file.stat().st_mtime,
            })
    return groups


def _detect_layout(root_dir: Path) -> str:
    """Detect 'harness' (two-level) vs 'flat' layout."""
    try:
        for child in root_dir.iterdir():
            if not child.is_dir():
                continue
            for gc in child.iterdir():
                if not gc.is_dir() or gc.name == "result":
                    continue
                if (gc / "milestones.csv").exists():
                    return "harness"
            break  # only check first child
    except Exception:
        pass
    return "flat"


# ── API routes ───────────────────────────────────────────────────────────

@app.route("/api/mstone/datasets")
def mstone_datasets():
    try:
        root_key = request.args.get("root", "")
        custom_path = request.args.get("path", "").strip()
        layout_hint = request.args.get("layout", "").strip()
        custom_id = request.args.get("id", "").strip()

        # Custom path mode
        if custom_path:
            result_dir = Path(custom_path).expanduser().resolve()
            if not result_dir.exists() or not result_dir.is_dir():
                return jsonify({"error": "Path not found", "path": str(result_dir)}), 404
            if custom_id and custom_id.startswith("custom_"):
                key = custom_id
            else:
                key = "custom_" + hashlib.md5(str(result_dir).encode()).hexdigest()[:10]
            found = False
            for i, (n, _) in enumerate(DATA_ROOTS):
                if n == key:
                    DATA_ROOTS[i] = (key, result_dir)
                    found = True
                    break
            if not found:
                DATA_ROOTS.append((key, result_dir))
            root_key = key

        if not root_key:
            root_key = DATA_ROOTS[0][0] if DATA_ROOTS else "result"

        root_map = {n: p for n, p in DATA_ROOTS}
        if root_key not in root_map:
            return jsonify({"error": "Unknown root", "available": list(root_map.keys())}), 400

        result_dir = root_map[root_key]
        if not result_dir.exists():
            return jsonify({"error": "Directory not found", "path": str(result_dir)}), 404

        # Detect layout
        is_harness = root_key in ("harness_workspace", "harness_trial_space")
        if root_key.startswith("custom_") or root_key in ("trial_space",):
            if layout_hint == "harness":
                is_harness = True
            elif layout_hint == "flat":
                is_harness = False
            else:
                is_harness = _detect_layout(result_dir) == "harness"

        groups = _scan_harness_dir(result_dir, root_key) if is_harness \
            else _scan_flat_dir(result_dir, root_key)

        groups_list = sorted(groups.values(), key=lambda x: x["name"])
        sources = [{"id": n, "name": n, "path": str(p)} for n, p in DATA_ROOTS]

        return jsonify({
            "status": "success",
            "root": root_key,
            "rootPath": str(result_dir),
            "groups": groups_list,
            "groupCount": len(groups_list),
            "sources": sources,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/mstone/selected-ids")
def mstone_selected_ids():
    try:
        base_path = request.args.get("basePath", "")
        if not base_path:
            return jsonify({"exists": False, "error": "Missing basePath"}), 400
        actual_path = _resolve_mstone_base_path(base_path)
        if not actual_path:
            return jsonify({"exists": False, "error": "Invalid basePath"}), 400

        def _read_id_list(name: str) -> list[str]:
            f = actual_path / name
            if not f.exists():
                return []
            return [l.strip() for l in f.read_text().splitlines()
                    if l.strip() and not l.startswith("#")]

        # selected = whitelist (only these are graded); non-graded = blacklist
        # (still built/run but excluded from the eval metrics — marked, not closed over).
        ids = _read_id_list("selected_milestone_ids.txt")
        non_graded_ids = _read_id_list("non-graded_milestone_ids.txt")
        if not ids and not non_graded_ids:
            return jsonify({"exists": False})
        closure = (
            _build_selected_prerequisite_closure(actual_path, ids) if ids
            else {"closure_ids": [], "prerequisite_ids": [],
                  "selected_edges": [], "prerequisite_edges": []}
        )
        return jsonify({"exists": True, "ids": ids, "non_graded_ids": non_graded_ids, **closure})
    except Exception as e:
        return jsonify({"exists": False, "error": str(e)}), 500


def _resolve_mstone_base_path(base_path: str) -> Path | None:
    relative_path = base_path[6:] if base_path.startswith("/DATA/") else base_path
    for root_name, root_path in DATA_ROOTS:
        if relative_path.startswith(root_name + "/"):
            return root_path / relative_path[len(root_name) + 1:]
    return None


def _dependency_row_endpoints(row: dict[str, str]) -> tuple[str, str]:
    source = (row.get("prerequisite_id") or row.get("source_id") or "").strip()
    target = (row.get("dependant_id") or row.get("target_id") or "").strip()
    return source, target


def _build_selected_prerequisite_closure(actual_path: Path, selected_ids: list[str]) -> dict:
    dep_file = actual_path / "dependencies.csv"
    selected_set = set(selected_ids)
    edges: list[dict[str, str]] = []
    incoming: dict[str, list[str]] = {}

    if dep_file.exists():
        with dep_file.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                source, target = _dependency_row_endpoints(row)
                if not source or not target:
                    continue
                edges.append({"source": source, "target": target})
                incoming.setdefault(target, []).append(source)

    closure_set = set(selected_ids)
    prerequisite_ids: list[str] = []
    queue = list(selected_ids)
    while queue:
        target = queue.pop(0)
        for source in incoming.get(target, []):
            if source in closure_set:
                continue
            closure_set.add(source)
            prerequisite_ids.append(source)
            queue.append(source)

    selected_edges: list[dict[str, str]] = []
    prerequisite_edges: list[dict[str, str]] = []
    for edge in edges:
        source = edge["source"]
        target = edge["target"]
        if source not in closure_set or target not in closure_set:
            continue
        if source in selected_set and target in selected_set:
            selected_edges.append(edge)
        else:
            prerequisite_edges.append(edge)

    return {
        "closure_ids": selected_ids + prerequisite_ids,
        "prerequisite_ids": prerequisite_ids,
        "selected_edges": selected_edges,
        "prerequisite_edges": prerequisite_edges,
    }


def _serve_csv_with_legacy_dep_headers(file_path: Path) -> Response | None:
    """For dependencies.csv only: rewrite prerequisite_id/dependant_id headers
    back to legacy source_id/target_id so the bundled mstone frontend (which
    reads b.source_id / b.target_id directly) keeps working after the CSV
    column rename. Other CSVs are served unmodified by the caller.
    """
    if file_path.name != "dependencies.csv":
        return None
    text = file_path.read_text()
    nl = text.find("\n")
    if nl == -1:
        return None
    header = text[:nl]
    if "prerequisite_id" not in header and "dependant_id" not in header:
        return None  # already legacy headers; let send_from_directory handle it
    rewritten = (
        header.replace("prerequisite_id", "source_id")
              .replace("dependant_id", "target_id")
        + text[nl:]
    )
    return Response(rewritten, mimetype="text/csv")


@app.route("/DATA/<path:filename>")
def serve_data_files(filename):
    """Serve CSV/data files from registered DATA_ROOTS."""
    for root_name, root_path in DATA_ROOTS:
        prefix = root_name + "/"
        if filename.startswith(prefix):
            file_path = root_path / filename[len(prefix):]
            if file_path.exists() and file_path.is_file():
                rewritten = _serve_csv_with_legacy_dep_headers(file_path)
                if rewritten is not None:
                    return rewritten
                return send_from_directory(file_path.parent, file_path.name)
    # Legacy fallback
    file_path = DATA_DIR / filename
    if file_path.exists() and file_path.is_file():
        rewritten = _serve_csv_with_legacy_dep_headers(file_path)
        if rewritten is not None:
            return rewritten
        return send_from_directory(file_path.parent, file_path.name)
    return jsonify({"error": "File not found"}), 404


# ── Main ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("Milestone DAG Visualization Server")
    print("=" * 60)
    for name, path in DATA_ROOTS:
        exists = "✓" if path.exists() else "✗"
        print(f"  [{exists}] {name}: {path}")
    print(f"\n  → http://localhost:5005/mstone")
    print(f"  → http://localhost:5005/prompts")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5005, debug=True, use_reloader=True)
