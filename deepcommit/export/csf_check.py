"""Conformance checker for CSF (Harbor multi-step) task folders.

Pure stdlib. ``check_task(path)`` validates a task root (``tasks/<family>/<task-id>/``) against the CSF task format
and returns a :class:`CheckReport`. Three flavours are auto-detected from ``task.toml``:

* ``history`` (a): single repo, ``[[verifier.collect]] csf-collect``, shared ``tests/test.sh`` + grader + ``step.env``.
* ``services`` (b): multi-service, ``artifacts = [...]``, per-step verifier stacks (``steps/<s>/tests/test.sh``).
* ``multirepo`` (c): DeepCommit extension, family ``cross-repo-change``, ``/app/<repo>`` checkouts.

Where the real tasks (ground truth) differ from the written spec, the real tasks win; each such decision is
recorded in a ``REAL:`` comment next to the rule.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import tomllib

FAMILIES = {"feature-evolution", "api-migration", "cross-repo-change", "end-to-end-feature", "scope-traps"}
SCHEMA_VERSION = "1.4"
STEP_RE = re.compile(r"^m(\d+)-([a-z0-9]+(?:-[a-z0-9]+)*)$")
TASK_NAME_RE = re.compile(r"^csf/([a-z0-9]+(?:-[a-z0-9]+)*)-(\d{3})$")
FOLDER_RE = re.compile(r"^csf-([a-z0-9]+(?:-[a-z0-9]+)*)-(\d{3})$")
LEAK_DIRS = {"tests", "steps", "solution", "hidden", "_src"}
SUITE_KEYS = ("repo", "workdir", "language", "framework", "run_cmd")


@dataclass(frozen=True)
class Issue:
    rule_id: str
    severity: str  # "error" | "warning"
    path: str
    message: str

    def format(self) -> str:
        return f"{self.severity.upper():7} {self.rule_id} {self.path}: {self.message}"


@dataclass
class CheckReport:
    root: str
    flavour: str = "unknown"
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self) -> str:
        lines = [i.format() for i in self.issues]
        lines.append(f"{self.root} [{self.flavour}]: {len(self.errors)} error(s), {len(self.warnings)} warning(s)")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "root": self.root,
            "flavour": self.flavour,
            "ok": self.ok,
            "errors": [vars(i) for i in self.errors],
            "warnings": [vars(i) for i in self.warnings],
        }


class _Checker:
    def __init__(self, root: Path, allow_missing_steps: bool):
        self.root = root
        self.allow_missing_steps = allow_missing_steps
        self.report = CheckReport(root=str(root))
        self.toml: dict = {}
        self.meta: dict = {}
        self.repos: list[str] = []
        self.step_names: list[str] = []
        self.steps_dir = root / "steps"
        self.has_steps = False
        self.cross_repo_steps = 0
        self.contract_only: list[str] = []

    # -- helpers -----------------------------------------------------------------------------------------------
    def err(self, rule: str, path: str | Path, msg: str) -> None:
        self.report.issues.append(Issue(rule, "error", self._rel(path), msg))

    def warn(self, rule: str, path: str | Path, msg: str) -> None:
        self.report.issues.append(Issue(rule, "warning", self._rel(path), msg))

    def _rel(self, p: str | Path) -> str:
        try:
            return str(Path(p).relative_to(self.root)) or "."
        except ValueError:
            return str(p)

    @staticmethod
    def _text(p: Path) -> str:
        return p.read_text(errors="replace")

    def _executable(self, rule: str, p: Path) -> bool:
        if not os.access(p, os.X_OK):
            self.err(rule, p, "file is not executable; run `chmod +x`")
            return False
        return True

    # -- driver --------------------------------------------------------------------------------------------------
    def run(self) -> CheckReport:
        if not self.root.is_dir():
            self.err("CSF001", self.root, "task directory does not exist")
            return self.report
        if not self.load_toml():
            return self.report
        self.detect_flavour()
        self.check_header()
        self.check_metadata()
        self.check_steps_table()
        self.check_dag()
        self.check_environment_table()
        self.check_collect_or_artifacts()
        self.check_dockerfile()
        self.check_collect_script()
        self.locate_steps()
        self.check_root_tests()
        if self.has_steps:
            self.check_step_dirs()
        self.check_env_leak()
        self.check_readme()
        return self.report

    def load_toml(self) -> bool:
        p = self.root / "task.toml"
        if not p.is_file():
            self.err("CSF001", p, "task.toml is missing")
            return False
        try:
            self.toml = tomllib.loads(self._text(p))
        except tomllib.TOMLDecodeError as e:
            self.err("CSF001", p, f"task.toml does not parse: {e}")
            return False
        self.meta = self.toml.get("metadata", {}) or {}
        repos = self.meta.get("repos")
        self.repos = [r for r in repos if isinstance(r, str)] if isinstance(repos, list) else []
        self.step_names = [s.get("name", "") for s in self.toml.get("steps", []) if isinstance(s, dict)]
        return True

    def detect_flavour(self) -> None:
        collect = self.toml.get("verifier", {}).get("collect")
        if self.meta.get("family") == "cross-repo-change" or "repos" in self.meta:
            self.report.flavour = "multirepo"
        elif "artifacts" in self.toml and not collect:
            self.report.flavour = "services"
        else:
            self.report.flavour = "history"

    @property
    def flavour(self) -> str:
        return self.report.flavour

    # -- task.toml -----------------------------------------------------------------------------------------------
    def check_header(self) -> None:
        t, p = self.toml, self.root / "task.toml"
        if t.get("schema_version") != SCHEMA_VERSION:
            self.err("CSF002", p, f'schema_version must be "{SCHEMA_VERSION}", got {t.get("schema_version")!r}')
        if t.get("multi_step_reward_strategy") != "mean":
            self.err(
                "CSF003", p, f'multi_step_reward_strategy must be "mean", got {t.get("multi_step_reward_strategy")!r}'
            )
        name = t.get("task", {}).get("name", "")
        m = TASK_NAME_RE.match(name) if isinstance(name, str) else None
        if not m:
            self.err("CSF004", p, f"[task].name must look like `csf/<slug>-<NNN>`, got {name!r}")
        fm = FOLDER_RE.match(self.root.name)
        if not fm:
            self.err("CSF004", self.root, f"folder name must look like `csf-<slug>-<NNN>`, got {self.root.name!r}")
        elif m and (m.group(1), m.group(2)) != (fm.group(1), fm.group(2)):
            self.err("CSF004", self.root, f"folder `{self.root.name}` does not match [task].name `{name}`")
        kw = t.get("task", {}).get("keywords")
        if not isinstance(kw, list) or "csf" not in kw:
            self.err("CSF005", p, '[task].keywords must be a list that includes "csf"')

    def check_metadata(self) -> None:
        p, meta = self.root / "task.toml", self.meta
        if not meta:
            self.err("CSF006", p, "[metadata] table is missing")
            return
        if meta.get("family") not in FAMILIES:
            self.err("CSF006", p, f"metadata.family must be one of {sorted(FAMILIES)}, got {meta.get('family')!r}")
        langs = meta.get("languages")
        if not isinstance(langs, list) or not langs:
            self.err("CSF006", p, "metadata.languages must be a non-empty list")
        planned, built = meta.get("milestones_planned"), meta.get("milestones_built")
        if not isinstance(planned, int) or not isinstance(built, int):
            self.err("CSF006", p, "metadata.milestones_planned and milestones_built must be integers")
        else:
            if planned < built:
                self.err("CSF006", p, f"milestones_planned ({planned}) must be >= milestones_built ({built})")
            if built != len(self.step_names):
                self.err(
                    "CSF006",
                    p,
                    f"milestones_built ({built}) must equal the number of [[steps]] ({len(self.step_names)})",
                )
        derived = meta.get("csf_task_source", "derived") != "authored"
        if self.flavour == "multirepo":
            self.check_multirepo_metadata()
        elif derived:
            for key in ("source_repo", "source_commit"):
                if not meta.get(key):
                    self.err(
                        "CSF006",
                        p,
                        f'metadata.{key} is required for derived tasks (set csf_task_source = "authored" otherwise)',
                    )
        src = meta.get("csf_task_source")
        if src is not None and src not in ("authored", "derived"):
            self.err("CSF006", p, f'metadata.csf_task_source must be "authored" or "derived", got {src!r}')

    def check_multirepo_metadata(self) -> None:
        p, meta = self.root / "task.toml", self.meta
        self.contract_only = list(meta.get("contract_only_repos") or [])
        if meta.get("family") != "cross-repo-change":
            self.err("CSF028", p, 'multi-repo tasks (metadata.repos) must have family = "cross-repo-change"')
        if len(self.repos) < 2:
            self.err("CSF028", p, "metadata.repos must list at least two repo names")
        if len(set(self.repos)) != len(self.repos):
            self.err("CSF028", p, "metadata.repos contains duplicates")
        for r in self.contract_only:
            if r not in self.repos or (meta.get("repo_roles") or {}).get(r) != "contract":
                self.err(
                    "CSF028", p, f"contract_only_repos entry {r!r} must be a metadata.repos entry with role contract"
                )
        if meta.get("csf_task_source") != "derived":
            self.err("CSF028", p, 'multi-repo tasks must set metadata.csf_task_source = "derived"')
        roles = meta.get("repo_roles")
        if not isinstance(roles, dict) or set(roles) != set(self.repos):
            self.err(
                "CSF028", p, "metadata.repo_roles must be a table with exactly one entry per repo in metadata.repos"
            )
        srcs = meta.get("source_repos")
        if not isinstance(srcs, list) or {s.get("name") for s in srcs if isinstance(s, dict)} != set(self.repos):
            self.err("CSF028", p, "metadata.source_repos must be a list of {name, url, commit}, one per repo")
        else:
            for s in srcs:
                if not s.get("url") or not s.get("commit"):
                    self.err("CSF028", p, f"source_repos entry {s.get('name')!r} needs url and commit")
        if not meta.get("csf_milestone_dag"):
            self.err("CSF007", p, "multi-repo tasks must record metadata.csf_milestone_dag")

    def check_steps_table(self) -> None:
        p = self.root / "task.toml"
        names = self.step_names
        if not names:
            self.err("CSF008", p, "no [[steps]] defined")
            return
        if any(not isinstance(n, str) or not n for n in names):
            self.err("CSF008", p, "every [[steps]] entry needs a name")
            return
        if len(set(names)) != len(names):
            self.err("CSF008", p, "[[steps]] names must be unique")
        for k, n in enumerate(names, start=1):
            m = STEP_RE.match(n)
            if not m:
                self.err("CSF008", p, f"step name {n!r} must look like `m<k>-<slug>` (lowercase, hyphenated)")
            elif int(m.group(1)) != k:
                self.err("CSF008", p, f"step {n!r} is at position {k}; step numbers must be consecutive 1..N in order")

    def _resolve(self, token: str) -> str | None:
        token = token.strip()
        if token in self.step_names:
            return token
        # REAL: voting-app uses short ids ("m1 -> m3-message-contract"), so `m<k>` resolves to step number k.
        if re.fullmatch(r"m\d+", token):
            hits = [n for n in self.step_names if n.split("-", 1)[0] == token]
            return hits[0] if len(hits) == 1 else None
        return None

    def check_dag(self) -> None:
        p = self.root / "task.toml"
        dag = self.meta.get("csf_milestone_dag")
        if dag is None:
            # REAL: chatwoot-automation-001 has no csf_milestone_dag, so it is only a warning outside multi-repo.
            if self.flavour != "multirepo":
                self.warn(
                    "CSF007", p, 'metadata.csf_milestone_dag is not set; add e.g. "m1 -> m2" to record dependencies'
                )
            return
        if not isinstance(dag, str) or not dag.strip():
            self.err(
                "CSF007", p, 'csf_milestone_dag must be a non-empty string like "m1 -> m2; m1 -> m3; m2 + m3 -> m4"'
            )
            return
        index = {n: i for i, n in enumerate(self.step_names)}
        seen: set[str] = set()
        for edge in (e for e in dag.split(";") if e.strip()):
            groups = [g for g in edge.split("->")]
            for g in groups:
                for tok in g.split("+"):
                    if not tok.strip():
                        self.err("CSF007", p, f"csf_milestone_dag edge {edge.strip()!r} has an empty node")
                        return
            resolved: list[list[str]] = []
            for g in groups:
                row = []
                for tok in g.split("+"):
                    r = self._resolve(tok)
                    if r is None:
                        self.err(
                            "CSF007", p, f"csf_milestone_dag names {tok.strip()!r}, which is not a step in [[steps]]"
                        )
                        return
                    row.append(r)
                    seen.add(r)
                resolved.append(row)
            for a, b in zip(resolved, resolved[1:], strict=False):
                for src in a:
                    for dst in b:
                        if index[src] >= index[dst]:
                            self.err(
                                "CSF007",
                                p,
                                f"csf_milestone_dag edge {src} -> {dst} is not in topological order of [[steps]]; "
                                "reorder the steps so dependencies come first",
                            )
        missing = [n for n in self.step_names if n not in seen]
        if missing:
            self.err(
                "CSF007",
                p,
                f"csf_milestone_dag does not mention step(s) {missing}; it must reference exactly the steps",
            )

    def check_environment_table(self) -> None:
        p = self.root / "task.toml"
        env = self.toml.get("environment", {})
        for key in ("build_timeout_sec", "cpus", "memory_mb"):
            if not isinstance(env.get(key), (int, float)):
                self.err("CSF009", p, f"[environment].{key} is required and must be a number")
        # REAL: voting-app has no [environment].workdir (agent workdir comes from the Dockerfile), so absent is a warning.
        if "workdir" not in env:
            self.warn("CSF009", p, '[environment].workdir is not set (spec: workdir = "/app")')
        elif env["workdir"] != "/app":
            self.err("CSF009", p, f'[environment].workdir must be "/app", got {env["workdir"]!r}')
        if not isinstance(self.toml.get("agent", {}).get("timeout_sec"), (int, float)):
            self.err("CSF009", p, "[agent].timeout_sec is required")
        # REAL: voting-app sets environment_mode only per step ([steps.verifier]); chatwoot sets it globally.
        glob = self.toml.get("verifier", {}).get("environment_mode")
        steps = [s for s in self.toml.get("steps", []) if isinstance(s, dict)]
        modes = [(s.get("name"), s.get("verifier", {}).get("environment_mode", glob)) for s in steps]
        if glob is None and not steps:
            modes = [("[verifier]", None)]
        for name, mode in modes:
            if mode != "separate":
                self.err("CSF011", p, f'verifier environment_mode must be "separate" for {name}, got {mode!r}')
        if not isinstance(self.toml.get("verifier", {}).get("timeout_sec", 0), (int, float)):
            self.err("CSF009", p, "[verifier].timeout_sec must be a number")

    def check_collect_or_artifacts(self) -> None:
        p = self.root / "task.toml"
        collect = self.toml.get("verifier", {}).get("collect")
        artifacts = self.toml.get("artifacts")
        if not collect and not artifacts:
            self.err(
                "CSF010",
                p,
                "neither [[verifier.collect]] nor top-level `artifacts = [...]` is defined; "
                "Harbor then has nothing to hand to the verifier",
            )
            return
        for c in collect or []:
            if not isinstance(c, dict) or not c.get("command"):
                self.err("CSF010", p, "[[verifier.collect]] entry needs a `command`")
        for a in artifacts or []:
            src = a.get("source") if isinstance(a, dict) else a
            if not isinstance(src, str) or not src.startswith("/app"):
                self.err("CSF010", p, f"artifacts source must be a path under /app, got {src!r}")

    # -- environment/ --------------------------------------------------------------------------------------------
    def _dockerfile_instructions(self, text: str) -> list[tuple[str, str]]:
        joined = re.sub(r"\\\s*\n", " ", text)
        out = []
        for line in joined.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            kw, _, rest = line.partition(" ")
            out.append((kw.upper(), rest.strip()))
        return out

    def check_dockerfile(self) -> None:
        env = self.root / "environment"
        df = env / "Dockerfile"
        if not df.is_file():
            self.err("CSF012", df, "environment/Dockerfile is missing")
            return
        ins = self._dockerfile_instructions(self._text(df))
        if not ins:
            self.err("CSF012", df, "Dockerfile has no instructions")
            return
        # A leading ARG is legal Docker (ARG before FROM), so only that is tolerated ahead of the first FROM.
        first = next((kw for kw, _ in ins if kw != "ARG"), None)
        if first != "FROM":
            self.err("CSF012", df, "the first instruction must be FROM")
        for kw, rest in ins:
            if kw not in ("COPY", "ADD"):
                continue
            args = [a for a in rest.split() if not a.startswith("--")]
            if any(a.startswith("--from=") for a in rest.split()) or len(args) < 2:
                continue
            for src in args[:-1]:
                s = src.lstrip("./").lstrip("/") if src not in (".", "./") else ""
                top = s.split("/")[0]
                if top in LEAK_DIRS:
                    self.err(
                        "CSF013",
                        df,
                        f"`{kw} {rest}` copies `{src}` into the image; hidden tests, steps and "
                        "solutions must never be baked into environment/ (leak)",
                    )
                if top == "base.patch":
                    self.err(
                        "CSF014",
                        df,
                        f"`{kw}` of base.patch puts it in an image layer; use "
                        "`RUN --mount=type=bind,source=base.patch,target=/tmp/csf/base.patch ...`",
                    )
                if "solution" in s.split("/") or s.endswith("gold.patch"):
                    self.err("CSF013", df, f"`{kw} {rest}` copies a solution into the image (leak)")
        patch = env / "base.patch"
        text = self._text(df)
        if "base.patch" in text:
            if not patch.is_file():
                self.err("CSF014", patch, "Dockerfile references base.patch but environment/base.patch is missing")
            if not re.search(r"--mount=type=bind[^\n]*source=base\.patch", re.sub(r"\\\s*\n", " ", text)):
                self.err(
                    "CSF014", df, "base.patch must be applied through a RUN --mount=type=bind,source=base.patch mount"
                )
        elif patch.is_file():
            self.warn("CSF014", patch, "environment/base.patch exists but the Dockerfile never applies it")

    def check_collect_script(self) -> None:
        collect = self.toml.get("verifier", {}).get("collect") or []
        uses = any(isinstance(c, dict) and c.get("command") == "csf-collect" for c in collect)
        script = self.root / "environment" / "csf-collect"
        if not uses and not script.exists():
            return
        if uses and not script.is_file():
            self.err("CSF015", script, "[[verifier.collect]] runs csf-collect but environment/csf-collect is missing")
            return
        if not script.is_file():
            return
        dockerfile = self.root / "environment" / "Dockerfile"
        if dockerfile.is_file() and "csf-collect" not in self._text(dockerfile):
            self.err("CSF015", dockerfile, "csf-collect is never COPY'd into the image")
        self._executable("CSF015", script)
        text = self._text(script)
        m = re.search(r"GIT_DIR=(\S+)", text)
        if not m:
            self.err("CSF015", script, "csf-collect must diff against a reference repo via GIT_DIR=/opt/csf/...")
        elif m.group(1).strip("\"'").startswith("/app"):
            self.err(
                "CSF015",
                script,
                f"GIT_DIR={m.group(1)} is inside /app, where the agent can rewrite it; "
                "use a reference repo outside /app (e.g. /opt/csf/base.git)",
            )
        if "/logs/artifacts" not in text or not re.search(r"model[^\s\"']*\.patch", text):
            self.err("CSF015", script, "csf-collect must write /logs/artifacts/model*.patch")
        if self.flavour == "multirepo":
            for repo in self.repos:
                if repo not in text:
                    self.err(
                        "CSF016",
                        script,
                        f"csf-collect does not handle repo {repo!r}; it must loop over every repo "
                        "in metadata.repos and write /logs/artifacts/model.<repo>.patch",
                    )
            if "model." not in text or "$" not in text:
                self.err("CSF016", script, "multi-repo csf-collect must write one model.<repo>.patch per repo")

    # -- steps/ ----------------------------------------------------------------------------------------------------
    def locate_steps(self) -> None:
        self.has_steps = self.steps_dir.is_dir()
        if self.has_steps:
            return
        src_dirs = [self.root.parent / "_src" / self.root.name, self.root.parent.parent / "_src" / self.root.name]
        has_src = any(d.is_dir() for d in src_dirs)
        if self.allow_missing_steps:
            note = "found generator source in _src/" if has_src else "no sibling _src/<task-id>/ generator found either"
            self.warn("CSF019", self.steps_dir, f"steps/ is missing (allowed: generated tree); {note}")
        else:
            self.err("CSF019", self.steps_dir, "steps/ is missing; render it from _src/ (or pass allow_missing_steps)")

    def check_root_tests(self) -> None:
        tests = self.root / "tests"
        shared = tests / "test.sh"
        per_step = self.flavour == "services"
        if shared.is_file():
            self.check_test_sh(shared)
        elif not per_step:
            self.err("CSF017", shared, "tests/test.sh is missing")
        if shared.is_file() or not per_step:
            self.check_grader(tests)
        p2p = tests / "p2p_core.txt"
        if p2p.is_file():
            self.check_p2p_core(p2p)
        elif not per_step:
            self.warn("CSF024", p2p, "tests/p2p_core.txt is missing (core regression list run at every step)")
        if self.flavour == "multirepo":
            self.check_suites()

    def check_test_sh(self, p: Path) -> None:
        text = self._text(p)
        self._executable("CSF017", p)
        if shutil.which("bash"):
            r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
            if r.returncode != 0:
                self.err(
                    "CSF017",
                    p,
                    f"`bash -n` failed: {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else 'syntax error'}",
                )
        # REAL: voting-app's per-step test.sh does not source step.env (it derives K from baked file names).
        if self.flavour != "services" and not re.search(r"(\.|source)\s+/tests/step\.env", text):
            self.err("CSF017", p, "test.sh must source /tests/step.env (STEP, F2P, P2P_EARLIER)")
        if not re.search(r"give_up|--error|infra_invalid|echo\s+'?\"?\{\s*\\?\"reward\\?\"\s*:\s*0", text):
            self.err(
                "CSF017",
                p,
                "test.sh has no fail-safe: on setup errors it must still write reward 0 "
                '(e.g. `grade --error` or an initial reward.json of {"reward": 0})',
            )
        if not re.search(r"/tests/(grade|score)[\w.]*|(ruby|python3?|node|bash)\s+\S*(grade|score)[\w.]*", text):
            self.err("CSF017", p, "test.sh never calls a grader (grade.* / score.*)")
        if self.flavour == "multirepo":
            for repo in self.repos:
                if repo not in text and "suites.json" not in text and "model." not in text:
                    self.err("CSF017", p, f"multi-repo test.sh does not handle repo {repo!r}")
            if "suites.json" not in text:
                self.warn("CSF017", p, "multi-repo test.sh should drive the suites listed in tests/suites.json")

    def check_grader(self, tests: Path) -> None:
        graders = sorted(g for g in tests.glob("*") if re.match(r"(grade|score)", g.name)) if tests.is_dir() else []
        if not graders:
            self.err("CSF018", tests, "no grader file (tests/grade.* or tests/score.*) found")
            return
        text = "\n".join(self._text(g) for g in graders)
        if "reward" not in text:
            self.err("CSF018", graders[0], "grader never writes `reward`")
            return
        spec = ("f2p_total", "f2p_passed", "p2p_total", "p2p_passed")
        # REAL: voting-app's score.py reports current_f2p/current_p2p/prior_pass_rate/prior_failures instead.
        alt = ("current_f2p", "current_p2p", "prior_pass_rate", "prior_failures")
        have_spec = all(k in text for k in spec)
        have_alt = all(k in text for k in alt)
        if self.flavour == "services":
            if not (have_spec or have_alt):
                self.err("CSF018", graders[0], f"grader reward keys must include {list(spec)} or {list(alt)}")
        elif not have_spec:
            miss = [k for k in spec if k not in text]
            self.err("CSF018", graders[0], f"grader does not emit reward key(s) {miss}")
        if self.flavour == "multirepo" and not re.search(r"(f2p|p2p)_(total|passed)__|_total__", text):
            self.warn("CSF018", graders[0], "multi-repo grader should also emit per-repo keys f2p_total__<repo>, ...")

    def check_p2p_core(self, p: Path) -> None:
        for n, line in enumerate(self._text(p).splitlines(), start=1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if re.search(r"\s", s) or ".." in s.split("/") or s.startswith("/"):
                self.err("CSF024", p, f"line {n}: expected a comment (#) or one relative test path, got {s!r}")

    def check_suites(self) -> None:
        p = self.root / "tests" / "suites.json"
        if not p.is_file():
            self.err(
                "CSF029",
                p,
                "multi-repo tasks need tests/suites.json (repo, workdir, language, framework, run_cmd, parser)",
            )
            return
        try:
            data = json.loads(self._text(p))
        except json.JSONDecodeError as e:
            self.err("CSF029", p, f"suites.json does not parse: {e}")
            return
        suites = data.get("suites") if isinstance(data, dict) else data
        if not isinstance(suites, list) or not suites:
            self.err("CSF029", p, 'suites.json must be a non-empty list of suites (or {"suites": [...]})')
            return
        covered = set()
        for i, s in enumerate(suites):
            if not isinstance(s, dict):
                self.err("CSF029", p, f"suite #{i} is not an object")
                continue
            for k in SUITE_KEYS:
                if not s.get(k):
                    self.err("CSF029", p, f"suite #{i} is missing `{k}`")
            if not (s.get("parser") or s.get("report")):
                self.warn("CSF029", p, f"suite #{i} names no report parser (`parser`/`report`)")
            if s.get("repo") not in self.repos:
                self.err("CSF029", p, f"suite #{i} repo {s.get('repo')!r} is not in metadata.repos")
            covered.add(s.get("repo"))
        modes = data.get("contract_only", {}) if isinstance(data, dict) else {}
        for r in self.repos:
            if r in self.contract_only:
                if r in covered or (modes.get(r) or {}).get("mode") != "contract-only":
                    self.err("CSF029", p, f"contract-only repo {r!r} takes no suite: record it under `contract_only`")
            elif r not in covered:
                self.err("CSF029", p, f"no suite for repo {r!r}")

    # -- per-step ----------------------------------------------------------------------------------------------------
    def check_step_dirs(self) -> None:
        listed = set(self.step_names)
        on_disk = {d.name for d in self.steps_dir.iterdir() if d.is_dir()}
        for n in self.step_names:
            if n not in on_disk:
                self.err(
                    "CSF031", self.steps_dir / n, f"step {n!r} is listed in task.toml but has no steps/{n}/ folder"
                )
        for n in sorted(on_disk - listed):
            self.warn("CSF031", self.steps_dir / n, "step folder is not listed in [[steps]]")
        root_hidden = self.root / "tests" / "hidden"
        for n in self.step_names:
            d = self.steps_dir / n
            if d.is_dir():
                self.check_step(n, d, root_hidden)
        if self.flavour == "multirepo" and self.step_names and not self.cross_repo_steps:
            self.err("CSF020", self.steps_dir, "a multi-repo task needs at least one step whose gold changes 2+ repos")

    def parse_env(self, p: Path) -> dict[str, str]:
        out = {}
        for line in self._text(p).splitlines():
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_]\w*)=(.*)$", line)
            if m:
                out[m.group(1)] = m.group(2).strip().strip("\"'")
        return out

    def check_step(self, name: str, d: Path, root_hidden: Path) -> None:
        inst = d / "instruction.md"
        itext = ""
        if not inst.is_file() or not (itext := self._text(inst)).strip():
            self.err("CSF020", inst, "instruction.md is missing or empty")
        elif re.search(r"^diff --git |^@@ -\d+(,\d+)? \+\d+|^\+\+\+ b/", itext, re.M):
            self.err(
                "CSF030",
                inst,
                "instruction.md contains gold-diff markers (`diff --git` / hunk headers); "
                "the instruction must not leak the solution",
            )
        self.check_solution(name, d)
        tests = d / "tests"
        env: dict[str, str] = {}
        step_env = tests / "step.env"
        if step_env.is_file():
            env = self.parse_env(step_env)
            self.check_step_env(name, step_env, env)
        elif self.flavour != "services":
            self.err("CSF022", step_env, "tests/step.env is missing (STEP, F2P, P2P_EARLIER)")
        if self.flavour == "services":
            ts = tests / "test.sh"
            if not ts.is_file():
                self.err("CSF017", ts, "per-step verifier needs tests/test.sh")
            else:
                self.check_test_sh(ts)
                self.check_grader(tests)
        hidden = self.hidden_files(d, root_hidden)
        if not hidden:
            self.err(
                "CSF025", tests, "no hidden tests found: expected tests/hidden/ (shared or per step) or tests/checks/"
            )
        elif itext:
            self.check_instruction_paths(name, inst, itext, hidden, env)

    @staticmethod
    def curated_ids(path: Path):
        """tests/ids.json of a step (curated F2P/P2P test ids), or None when the step is graded by test files."""
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        except ValueError:
            return {}

    def check_step_env(self, name: str, p: Path, env: dict[str, str]) -> None:
        if env.get("STEP") != name:
            self.err("CSF022", p, f"STEP must be {name!r}, got {env.get('STEP')!r}")
        f2p_keys = [k for k in env if k == "F2P" or k.startswith("F2P_")]
        ids = self.curated_ids(p.parent / "ids.json")
        if ids is not None:
            if not ids.get("f2p"):
                self.err("CSF022", p.parent / "ids.json", "ids.json has no f2p test id")
            elif self.flavour == "multirepo" and not all("::" in i for i in ids["f2p"] + ids.get("p2p", [])):
                self.err("CSF022", p.parent / "ids.json", "multi-repo ids must be `<repo>::<id>`")
            f2p_keys = f2p_keys or ["F2P"]
        elif not f2p_keys or not any(env[k].strip() for k in f2p_keys):
            self.err("CSF022", p, "no F2P list defined (F2P or F2P_<REPO>) or it is empty")
        if "P2P_EARLIER" not in env and not any(k.startswith("P2P_EARLIER_") for k in env):
            self.warn("CSF022", p, "no P2P_EARLIER list defined (use an empty string for step 1)")
        if self.flavour == "multirepo":
            upper = {re.sub(r"\W", "_", r).upper() for r in self.repos}
            namespaced = any(k.startswith("F2P_") and k[4:] in upper for k in f2p_keys) or any(
                "::" in env[k] for k in f2p_keys
            )
            if not namespaced:
                self.err(
                    "CSF022", p, "multi-repo step.env must namespace F2P: F2P_<REPO> lists or `<repo>::<id>` entries"
                )

    def hidden_files(self, d: Path, root_hidden: Path) -> list[Path]:
        """Relative paths of hidden test files visible to this step (shared hidden/, step hidden/, step checks/)."""
        out: list[Path] = []
        for base in (root_hidden, d / "tests" / "hidden"):
            if base.is_dir():
                out += [f.relative_to(base) for f in base.rglob("*") if f.is_file()]
        checks = d / "tests" / "checks"
        if checks.is_dir():
            out += [Path(f.name) for f in checks.rglob("*") if f.is_file() and f.name != "conftest.py"]
        return out

    def check_instruction_paths(self, name: str, inst: Path, text: str, hidden: list[Path], env: dict) -> None:
        envdir = self.root / "environment"
        names: set[str] = set()
        for rel in hidden:
            parts = rel.parts
            names.add(str(rel))
            if self.flavour == "multirepo" and len(parts) > 1 and parts[0] in self.repos:
                names.add("/".join(parts[1:]))
        for k, v in env.items():
            if k == "F2P" or k.startswith("F2P_"):
                names.update(t.split("::", 1)[-1] for t in v.split() if "/" in t or "." in t)
        for rel in sorted(names):
            if len(rel) < 8 or (envdir / rel).exists():
                continue
            if rel in text:
                self.err(
                    "CSF026",
                    inst,
                    f"instruction mentions hidden-test-only path `{rel}`; hidden test files must not be named",
                )

    def check_solution(self, name: str, d: Path) -> None:
        sol = d / "solution"
        solve = sol / "solve.sh"
        stext = ""
        if not solve.is_file():
            self.err("CSF020", solve, "solution/solve.sh is missing")
        else:
            self._executable("CSF020", solve)
            stext = self._text(solve)
            if not re.search(r"git\s+(-C\s+\S+\s+)?apply|\bpatch\s+-p", stext):
                self.err("CSF020", solve, "solve.sh does not apply a gold patch (`git apply ...`)")
        if self.flavour == "multirepo":
            gold = sol / "gold"
            files = sorted(gold.glob("*.patch")) if gold.is_dir() else []
            if not files:
                self.err("CSF020", gold, "step needs solution/gold/<repo>.patch for at least one repo")
            elif len(files) < 2:
                self.warn("CSF020", gold, "single-repo step: only one solution/gold/<repo>.patch")
            else:
                self.cross_repo_steps += 1
            for g in files:
                repo = g.stem
                if repo not in self.repos:
                    self.err("CSF020", g, f"gold patch for unknown repo {repo!r} (metadata.repos = {self.repos})")
                if stext and f"/app/{repo}" not in stext and repo not in stext:
                    self.err(
                        "CSF020", solve, f"solve.sh never applies the {repo} gold patch (`git -C /app/{repo} apply`)"
                    )
                self.check_patch(g)
        else:
            g = sol / "gold.patch"
            if not g.is_file():
                self.err("CSF020", g, "solution/gold.patch is missing")
            else:
                self.check_patch(g)

    def check_patch(self, g: Path) -> None:
        text = self._text(g)
        if not text.strip():
            self.err("CSF021", g, "gold patch is empty")
            return
        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        i, nfiles = 0, 0
        while i < len(lines):
            line = lines[i]
            m = re.match(r"^diff --git a/(\S+) b/(\S+)$", line)
            if m:
                nfiles += 1
                for path in (m.group(1), m.group(2)):
                    if path.startswith("/") or ".." in path.split("/") or path.split("/")[0] == ".git":
                        self.err("CSF021", g, f"patch path `{path}` escapes the repo (absolute, `..`, or .git)")
                i += 1
                continue
            h = re.match(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@", line)
            if h:
                old, new = int(h.group(1) or 1), int(h.group(2) or 1)
                i += 1
                while (old > 0 or new > 0) and i < len(lines):
                    c = lines[i][:1]
                    if c == " " or lines[i] == "":
                        old, new = old - 1, new - 1
                    elif c == "-":
                        old -= 1
                    elif c == "+":
                        new -= 1
                    elif c == "\\":
                        pass
                    else:
                        break
                    i += 1
                if old != 0 or new != 0:
                    self.err("CSF021", g, f"malformed hunk near line {i}: line counts do not match the @@ header")
                    return
                continue
            i += 1
        if nfiles == 0:
            self.err("CSF021", g, "gold patch is not a unified git diff (no `diff --git` header)")

    # -- environment leak ---------------------------------------------------------------------------------------------
    def check_env_leak(self) -> None:
        env = self.root / "environment"
        if not env.is_dir():
            return
        bad = [d for d in env.rglob("*") if d.is_dir() and d.name in ("hidden", "steps", "solution")]
        for d in bad:
            self.err(
                "CSF023",
                d,
                f"environment/ contains a `{d.name}/` directory; verifier material must stay out of the image",
            )
        for f in env.rglob("step.env"):
            self.err("CSF023", f, "step.env must not live under environment/")
        if not self.has_steps:
            return
        env_rel = {f.relative_to(env) for f in env.rglob("*") if f.is_file() and f.name != "base.patch"}
        hidden_roots = [
            self.root / "tests" / "hidden",
            *(self.steps_dir / n / "tests" / "hidden" for n in self.step_names),
        ]
        for base in hidden_roots:
            if base.is_dir():
                for f in base.rglob("*"):
                    if f.is_file() and f.relative_to(base) in env_rel:
                        self.err(
                            "CSF023",
                            env / f.relative_to(base),
                            "a hidden test file also appears under environment/ (leak)",
                        )
        for n in self.step_names:
            checks = self.steps_dir / n / "tests" / "checks"
            if checks.is_dir():
                for f in checks.rglob("test_*.py"):
                    if any(e.name == f.name for e in env_rel):
                        self.err("CSF023", env, f"hidden check {f.name} also appears under environment/ (leak)")

    def check_readme(self) -> None:
        p = self.root / "README.md"
        if not p.is_file():
            self.warn("CSF027", p, "README.md is missing")
            return
        heads = " | ".join(re.findall(r"^#+\s*(.+)$", self._text(p), re.M)).lower()
        for label, pat in (("status", r"status"), ("how grading works", r"grading"), ("known limits", r"limit")):
            if not re.search(pat, heads):
                self.warn("CSF027", p, f"README.md has no `{label}` section")


def check_task(path: Path | str, allow_missing_steps: bool = False) -> CheckReport:
    return _Checker(Path(path), allow_missing_steps).run()
