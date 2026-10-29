"""Cross-repo edge detection for a workspace.

The single-repo commit DAG (``commit_dag_v3``) is built from ``git blame`` overlap, so it can never see a
dependency between two repos: their files do not share history. This module adds the missing edges from
signals that do cross repos. Direction is always ``source -> target`` = *target depends on source*.

Edge classes
------------
``hard``  enforced downstream (path completeness, dependency inference treats them as real)
``soft``  recorded as hints only; never enforced, the LLM may cut them

Signals (strongest first)
-------------------------
``xrepo_contract``  a contract-repo commit adds identifiers (proto message/field/rpc/enum value, ...), and a
                    later commit in another repo uses them in hand-written (non-generated) code.
``xrepo_pin``       a commit bumps a dependency pin (go.mod pseudo-version, git submodule gitlink, release tag)
                    to a specific commit of another workspace repo. Hard only when that commit changes the
                    contract; a pin bump alone is not a feature link.
``xrepo_pr_link``   an explicit ``owner/repo#N`` or GitHub URL reference to another workspace repo's PR.
``xrepo_time``      same author in two repos within a short window. Always soft.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from .workspace import RepoSpec, Workspace

HARD = "hard"
SOFT = "soft"

EDGE_CONTRACT = "xrepo_contract"
EDGE_PIN = "xrepo_pin"
EDGE_PR_LINK = "xrepo_pr_link"
EDGE_TIME = "xrepo_time"

# Words too generic to prove a link by themselves.
STOP_WORDS = frozenset(
    "id ids name names type types value values data request response message messages error errors status result "
    "results string bool int list map key keys time count options option config info version field fields user users "
    "state create update delete get set enabled timeout description metadata item items text json size level mode "
    "kind label labels path url uri token event events request_id test tests none true false null".split()
)


@dataclass
class CommitRec:
    sha: str  # 7-char key
    repo: str  # workspace repo name
    message: str = ""
    full_sha: str = ""
    committed_at: Optional[datetime] = None
    author_key: str = ""
    pr_number: Optional[int] = None
    files: List[str] = field(default_factory=list)  # repo-relative paths (may be empty if unknown)
    patch: Callable[[], str] = lambda: ""

    def full(self) -> str:
        return self.full_sha or self.sha


@dataclass
class PRRec:
    repo: str
    number: int
    title: str = ""
    body: str = ""
    merge_commit_sha: str = ""
    commit_shas: List[str] = field(default_factory=list)
    merged_at: Optional[datetime] = None


@dataclass
class XEdge:
    source: str
    target: str
    source_repo: str
    target_repo: str
    edge_types: List[str]
    confidence: float
    edge_class: str
    evidence: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "source": self.source,
            "target": self.target,
            "source_repo": self.source_repo,
            "target_repo": self.target_repo,
            "edge_types": sorted(set(self.edge_types)),
            "confidence": round(self.confidence, 3),
            "edge_class": self.edge_class,
            "evidence": self.evidence[:8],
        }


# --------------------------------------------------------------------------- diff helpers


def split_diff(patch: str) -> Dict[str, List[str]]:
    """Map new-path -> added lines (without the leading '+') for every file in a unified diff."""
    out: Dict[str, List[str]] = defaultdict(list)
    cur: Optional[str] = None
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", line)
            cur = m.group(2) if m else None
            if cur is not None:
                out.setdefault(cur, [])
        elif line.startswith("+++ "):
            p = line[4:].strip()
            if p == "/dev/null":
                continue
            cur = p[2:] if p.startswith("b/") else p
            out.setdefault(cur, [])
        elif line.startswith("+") and not line.startswith("+++") and cur is not None:
            out[cur].append(line[1:])
    return dict(out)


def glob_match(path: str, patterns: Iterable[str]) -> bool:
    """gitwildmatch-ish matching; ``**/`` matches zero or more directories."""
    for pat in patterns:
        regex = _glob_to_regex(pat)
        if regex.match(path):
            return True
    return False


_GLOB_CACHE: Dict[str, "re.Pattern[str]"] = {}


def _glob_to_regex(pat: str) -> "re.Pattern[str]":
    cached = _GLOB_CACHE.get(pat)
    if cached is not None:
        return cached
    i, n, out = 0, len(pat), ""
    while i < n:
        c = pat[i]
        if pat.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pat.startswith("/**", i) and i + 3 == n:
            out += "(?:/.*)?"
            i += 3
        elif pat.startswith("**", i):
            out += ".*"
            i += 2
        elif c == "*":
            out += "[^/]*"
            i += 1
        elif c == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(c)
            i += 1
    rx = re.compile("^" + out + "$")
    _GLOB_CACHE[pat] = rx
    return rx


# --------------------------------------------------------------------------- identifiers

_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def words_of(token: str) -> Tuple[str, ...]:
    return tuple(w.lower() for w in _WORD_RE.findall(token))


_PROTO_PATTERNS = [
    re.compile(r"^\s*(?:message|enum|service|oneof)\s+([A-Za-z_]\w*)"),
    re.compile(r"^\s*rpc\s+([A-Za-z_]\w*)"),
    # field: [repeated|optional] type name = N;
    re.compile(r"^\s*(?:repeated\s+|optional\s+)?[\w.<>, ]+?\s+([A-Za-z_]\w*)\s*=\s*\d+"),
    # enum value: NAME = N;
    re.compile(r"^\s*([A-Za-z_]\w*)\s*=\s*\d+\s*(?:\[.*\])?\s*;"),
]


def extract_contract_identifiers(patch: str, contract_paths: List[str]) -> Set[Tuple[str, ...]]:
    """Identifiers (as word tuples) added by a contract commit, restricted to ``contract_paths`` files."""
    idents: Set[Tuple[str, ...]] = set()
    for path, added in split_diff(patch).items():
        if not glob_match(path, contract_paths):
            continue
        is_proto = path.endswith(".proto")
        for line in added:
            if is_proto:
                s = line.split("//")[0]
                for rx in _PROTO_PATTERNS:
                    m = rx.match(s)
                    if m:
                        idents.add(words_of(m.group(1)))
                        break
            else:
                # openapi/json-schema/yaml: take snake/camel identifiers with >= 2 words, or long single words
                for tok in _TOKEN_RE.findall(line):
                    w = words_of(tok)
                    if len(w) >= 2 or (len(w) == 1 and len(w[0]) >= 6):
                        idents.add(w)
    return {i for i in idents if _distinctive(i)}


def _distinctive(words: Tuple[str, ...]) -> bool:
    if not words:
        return False
    joined = "".join(words)
    if len(joined) < 5:
        return False
    if len(words) == 1 and words[0] in STOP_WORDS:
        return False
    if all(w in STOP_WORDS for w in words):
        return False
    return True


def _ngrams_of_text(lines: Iterable[str], max_n: int = 4) -> Set[Tuple[str, ...]]:
    grams: Set[Tuple[str, ...]] = set()
    for line in lines:
        for tok in _TOKEN_RE.findall(line):
            w = words_of(tok)
            for n in range(1, min(max_n, len(w)) + 1):
                for i in range(len(w) - n + 1):
                    grams.add(w[i : i + n])
    return grams


# --------------------------------------------------------------------------- signals


def _hours(a: Optional[datetime], b: Optional[datetime]) -> Optional[float]:
    if a is None or b is None:
        return None
    try:
        return (b - a).total_seconds() / 3600.0
    except TypeError:  # naive vs aware
        return None


# df statistics are only meaningful with enough consumer commits; below this every identifier counts fully
MIN_CONSUMER_COMMITS_FOR_DF = 20
UNIQUE_DF_FRACTION = 0.05
RARE_DF_FRACTION = 0.15


def _consumer_grams(
    cons: CommitRec, generated_globs: List[str], contract_globs: List[str]
) -> Set[Tuple[str, ...]]:
    lines: List[str] = []
    for path, ls in split_diff(cons.patch()).items():
        if cons.files and path not in cons.files:
            continue
        if glob_match(path, generated_globs) or glob_match(path, contract_globs):
            continue
        lines.extend(ls)
    return _ngrams_of_text(lines) if lines else set()


def _ident_weight(n_introducing: int, df_fraction: float) -> float:
    """How much one matched identifier proves a link.

    1.0  introduced by exactly one contract commit and rare in consumer code (e.g. ``acosh``)
    0.5  introduced by a few commits and still fairly rare
    0.0  generic for this domain (``payload``, ``vectors``): many contract commits add it or it is everywhere
    """
    if n_introducing == 1 and df_fraction < UNIQUE_DF_FRACTION:
        return 1.0
    if n_introducing <= 3 and df_fraction < RARE_DF_FRACTION:
        return 0.5
    return 0.0


def contract_edges(
    ws: Workspace,
    commits: Dict[str, List[CommitRec]],
) -> List[XEdge]:
    cfg = ws.xrepo
    contract_repos = [r for r in ws.repos if r.contract_paths]
    all_contract_globs = [g for r in ws.repos for g in r.contract_paths]
    edges: List[XEdge] = []
    consumer_cache: Dict[str, Set[Tuple[str, ...]]] = {}
    for cr in contract_repos:
        ident_by_commit: List[Tuple[CommitRec, Set[Tuple[str, ...]]]] = []
        for c in commits.get(cr.name, []):
            ids = extract_contract_identifiers(c.patch(), cr.contract_paths)
            if ids:
                ident_by_commit.append((c, ids))
        if not ident_by_commit:
            continue
        intro_count: Dict[Tuple[str, ...], int] = defaultdict(int)
        for _, ids in ident_by_commit:
            for i in ids:
                intro_count[i] += 1

        consumers = [c for other in ws.repos if other.name != cr.name for c in commits.get(other.name, [])]
        for cons in consumers:
            if cons.sha not in consumer_cache:
                consumer_cache[cons.sha] = _consumer_grams(cons, cfg.generated_globs, all_contract_globs)
        use_df = len(consumers) >= MIN_CONSUMER_COMMITS_FOR_DF
        df: Dict[Tuple[str, ...], int] = defaultdict(int)
        if use_df:
            for cons in consumers:
                for g in consumer_cache[cons.sha]:
                    df[g] += 1

        for cons in consumers:
            grams = consumer_cache[cons.sha]
            if not grams:
                continue
            for cc, ids in ident_by_commit:
                if cc.repo == cons.repo:
                    continue
                matched = []
                score = 0.0
                for i in sorted(ids):
                    if i not in grams:
                        continue
                    frac = df[i] / len(consumers) if use_df else 0.0
                    w = _ident_weight(intro_count[i], frac)
                    if w > 0:
                        matched.append(i)
                        score += w
                if not matched:
                    continue
                conf = 0.9 if score >= 2 else 0.75 if score >= 1 else 0.5
                dt = _hours(cc.committed_at, cons.committed_at)
                if dt is not None and 0 <= dt <= cfg.pin_lag_days * 24:
                    conf = min(1.0, conf + 0.1)
                ev = (
                    f"{cc.sha}({cc.repo}) adds {', '.join('_'.join(m) for m in matched[:5])}; "
                    f"used in {cons.sha}({cons.repo})"
                )
                edges.append(
                    XEdge(
                        source=cc.sha,
                        target=cons.sha,
                        source_repo=cc.repo,
                        target_repo=cons.repo,
                        edge_types=[EDGE_CONTRACT],
                        confidence=conf,
                        edge_class=HARD if conf >= cfg.link_confidence_threshold else SOFT,
                        evidence=[ev],
                    )
                )
    return edges


_PSEUDO_RE = r"v[0-9][\w.+-]*?[-.]\d{14}-([0-9a-f]{12})"
_SUBMODULE_RE = re.compile(r"Subproject commit ([0-9a-f]{40})")


def _pins_in_patch(patch: str, target: RepoSpec, pin_files: Iterable[str]) -> Tuple[Set[str], Set[str]]:
    """Return (pinned sha prefixes, pinned version tags) that a patch's pin-file hunks set for ``target``."""
    shas: Set[str] = set()
    tags: Set[str] = set()
    pin_basenames = set(pin_files)
    mod = re.escape(f"github.com/{target.owner}/{target.repo}")
    pseudo = re.compile(rf"{mod}(?:/v\d+)?\s+{_PSEUDO_RE}", re.I)
    tagged = re.compile(rf"{mod}(?:/v\d+)?\s+(v[0-9][\w.+-]*)", re.I)
    cur_path = ""
    for path, added in split_diff(patch).items():
        base = path.rsplit("/", 1)[-1]
        text = "\n".join(added)
        if base == "go.mod":
            for m in pseudo.finditer(text):
                shas.add(m.group(1).lower())
            for m in tagged.finditer(text):
                if not re.search(r"[-.]\d{14}-[0-9a-f]{12}", m.group(1)):
                    tags.add(m.group(1))
        elif base in pin_basenames or base.endswith(".toml") or base == "package.json":
            for m in re.finditer(rf"{re.escape(target.full_name)}[^\n]*?\brev\s*=\s*\"([0-9a-f]{{7,40}})\"", text, re.I):
                shas.add(m.group(1).lower())
            for m in re.finditer(rf"{re.escape(target.full_name)}[^\n]*?#([0-9a-f]{{7,40}})\b", text, re.I):
                shas.add(m.group(1).lower())
        # git submodule gitlink: path ends with the repo name (or matches .gitmodules text)
        if "Subproject commit" in text and (
            path.rsplit("/", 1)[-1].lower() in {target.repo.lower(), target.name.lower()}
            or target.repo.lower() in path.lower()
        ):
            for m in _SUBMODULE_RE.finditer(text):
                shas.add(m.group(1))
        cur_path = path
    del cur_path
    return shas, tags


def pin_edges(
    ws: Workspace,
    commits: Dict[str, List[CommitRec]],
    releases: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[XEdge]:
    cfg = ws.xrepo
    releases = releases or {}
    edges: List[XEdge] = []
    by_prefix: Dict[str, List[CommitRec]] = {t.name: commits.get(t.name, []) for t in ws.repos}
    for consumer in ws.repos:
        for c in commits.get(consumer.name, []):
            patch = c.patch()
            if not patch or "go.mod" not in patch and "Subproject" not in patch and ".toml" not in patch and "package.json" not in patch:
                continue
            for target in ws.repos:
                if target.name == consumer.name:
                    continue
                shas, tags = _pins_in_patch(patch, target, cfg.pin_files)
                for tag in tags:
                    rel = releases.get(target.name, {})
                    sha = rel.get(tag) or rel.get(tag.lstrip("v"))
                    if sha:
                        shas.add(sha.lower())
                for prefix in shas:
                    pinned = next((t for t in by_prefix[target.name] if t.full().lower().startswith(prefix[:40])), None)
                    if pinned is None:
                        continue
                    touches_contract = bool(target.contract_paths) and any(
                        glob_match(p, target.contract_paths) for p in split_diff(pinned.patch())
                    )
                    hard = touches_contract
                    edges.append(
                        XEdge(
                            source=pinned.sha,
                            target=c.sha,
                            source_repo=target.name,
                            target_repo=consumer.name,
                            edge_types=[EDGE_PIN],
                            confidence=0.85 if hard else 0.5,
                            edge_class=HARD if hard else SOFT,
                            evidence=[f"{c.sha}({consumer.name}) pins {pinned.sha}({target.name})"
                                      + (" which changes the contract" if hard else "")],
                        )
                    )
    return edges


_REF_RES = [
    re.compile(r"(?<![\w/.-])([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#(\d+)"),
    re.compile(r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:pull|issues)/(\d+)"),
]


def scan_references(text: str, ws: Workspace) -> List[Tuple[str, int]]:
    """Return ``(repo_name, number)`` for every reference to another workspace repo found in ``text``."""
    by_full = {r.full_name.lower(): r.name for r in ws.repos}
    found: List[Tuple[str, int]] = []
    for rx in _REF_RES:
        for m in rx.finditer(text or ""):
            name = by_full.get(f"{m.group(1)}/{m.group(2)}".lower())
            if name:
                found.append((name, int(m.group(3))))
    return list(dict.fromkeys(found))


def pr_link_edges(
    ws: Workspace,
    commits: Dict[str, List[CommitRec]],
    prs: Dict[str, List[PRRec]],
) -> List[XEdge]:
    edges: List[XEdge] = []
    pr_index: Dict[Tuple[str, int], PRRec] = {(r, p.number): p for r, ps in prs.items() for p in ps}
    commit_index: Dict[Tuple[str, str], CommitRec] = {(r, c.sha): c for r, cs in commits.items() for c in cs}
    for repo, cs in commits.items():
        for c in cs:
            pr = pr_index.get((repo, c.pr_number)) if c.pr_number is not None else None
            text = "\n".join([c.message, pr.title if pr else "", pr.body if pr else ""])
            for tgt_repo, number in scan_references(text, ws):
                if tgt_repo == repo:
                    continue
                tpr = pr_index.get((tgt_repo, number))
                if tpr is None:
                    continue
                shas = [tpr.merge_commit_sha[:7]] if tpr.merge_commit_sha else []
                shas = [s for s in shas if (tgt_repo, s) in commit_index] or [
                    s[:7] for s in tpr.commit_shas if (tgt_repo, s[:7]) in commit_index
                ][:1]
                for s in shas:
                    ref = commit_index[(tgt_repo, s)]
                    src, dst = ref, c
                    # the referenced side is the prerequisite unless time clearly says otherwise
                    dt = _hours(ref.committed_at, c.committed_at)
                    if dt is not None and dt < 0 and ws.get(tgt_repo).role != "contract":
                        src, dst = c, ref
                    edges.append(
                        XEdge(
                            source=src.sha,
                            target=dst.sha,
                            source_repo=src.repo,
                            target_repo=dst.repo,
                            edge_types=[EDGE_PR_LINK],
                            confidence=0.9,
                            edge_class=HARD,
                            evidence=[f"{c.sha}({repo}) references {ws.get(tgt_repo).full_name}#{number}"],
                        )
                    )
    return edges


_BOT_RE = re.compile(r"(bot|noreply|dependabot|renovate)", re.I)


def time_edges(ws: Workspace, commits: Dict[str, List[CommitRec]]) -> List[XEdge]:
    cfg = ws.xrepo
    if not cfg.emit_soft_edges:
        return []
    window = timedelta(days=cfg.time_window_days)
    edges: List[XEdge] = []
    flat = [c for cs in commits.values() for c in cs if c.committed_at and c.author_key and not _BOT_RE.search(c.author_key)]
    flat.sort(key=lambda c: c.committed_at)
    for i, a in enumerate(flat):
        best: Dict[str, CommitRec] = {}
        for b in flat[i + 1 :]:
            try:
                if b.committed_at - a.committed_at > window:
                    break
            except TypeError:
                break
            if b.repo != a.repo and b.author_key == a.author_key and b.repo not in best:
                best[b.repo] = b
        for b in best.values():
            edges.append(
                XEdge(
                    source=a.sha,
                    target=b.sha,
                    source_repo=a.repo,
                    target_repo=b.repo,
                    edge_types=[EDGE_TIME],
                    confidence=0.3,
                    edge_class=SOFT,
                    evidence=[f"same author within {cfg.time_window_days:g}d: {a.sha}({a.repo}) -> {b.sha}({b.repo})"],
                )
            )
    return edges


def merge_edges(edges: Iterable[XEdge]) -> List[XEdge]:
    """Combine duplicate (source, target) pairs: union types and evidence, max confidence, hard wins."""
    merged: Dict[Tuple[str, str], XEdge] = {}
    for e in edges:
        key = (e.source, e.target)
        cur = merged.get(key)
        if cur is None:
            merged[key] = XEdge(**{**e.__dict__, "edge_types": list(e.edge_types), "evidence": list(e.evidence)})
            continue
        cur.edge_types = sorted(set(cur.edge_types) | set(e.edge_types))
        cur.evidence.extend(x for x in e.evidence if x not in cur.evidence)
        cur.confidence = max(cur.confidence, e.confidence)
        if e.edge_class == HARD:
            cur.edge_class = HARD
    return list(merged.values())


def build_xrepo_edges(
    ws: Workspace,
    commits: Dict[str, List[CommitRec]],
    prs: Optional[Dict[str, List[PRRec]]] = None,
    releases: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[XEdge]:
    """All cross-repo edges for a workspace. Returns [] for a single-repo workspace."""
    if not ws.is_multi:
        return []
    edges: List[XEdge] = []
    edges += contract_edges(ws, commits)
    edges += pin_edges(ws, commits, releases)
    edges += pr_link_edges(ws, commits, prs or {})
    edges += time_edges(ws, commits)
    merged = merge_edges(edges)
    if not ws.xrepo.emit_soft_edges:
        merged = [e for e in merged if e.edge_class == HARD]
    merged.sort(key=lambda e: (e.edge_class != HARD, -e.confidence, e.source, e.target))
    return merged
