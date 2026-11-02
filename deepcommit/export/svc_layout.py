"""The CSF service layout of an exported task: what the workspace manifest says about services and the environment, and
the files that turn it into a Harbor task run through harbor-svc.

Task layout (``tasks/<family>/<task-id>/``)::

  task.toml                   settings, milestone cards, service control (manifest mode), [[steps]]
  README.md, instruction.md   instruction.md: the episode context
  ledger.json                 GENERATED after curation: every check -> family, version, first/last step, bucket, kind
  environment/
    Dockerfile                main: the agent's container, plus the test runner and its packages
    docker-compose.yaml       main + infrastructure, app-src volume, internal appnet, localhost forwards
    app/<repo>/               starting code, one folder per repository
    app/<svc>/csf-service.toml  manifest of an app service (runtime, build, command, port, health, depends_on, env)
    runtimes/<runtime>/       task-owned images: Dockerfile, csf-run, csf-health
    vendor/                   files the Dockerfile bind-mounts (csf_environment.vendor)
  steps/mN-<slug>/
    instruction.md, solution/{gold.patch, solve.sh}
    tests/{test.sh, grade.py, suite.json, checks/<repo>/...}

Workspace manifest keys (all optional)::

  csf_services:
    - name: qdrant-server            # folder under /app and compose service name
      kind: app                      # app (default): csf-service.toml, deployed by harbor-svc; infra: a compose service
      runtime: {name: qdrant-1.20, dir: csf/runtimes/qdrant-1.20}   # dir relative to the manifest; needs a Dockerfile
      command: qdrant --disable-telemetry
      build: ""                      # optional build step, run in /build
      port: 6333
      health: /healthz               # an HTTP path or "tcp"
      depends_on: []
      env: {QDRANT__STORAGE__STORAGE_PATH: /tmp/qdrant/storage}
      localhost_ports: [6333, 6334]  # main forwards localhost:<port> to <name>:<port> (clients that default to localhost)
      # infra only: compose: {image: postgres:16, healthcheck: {...}}  (networks/appnet added)
  csf_environment:
    apt: [python3, python3-venv]     # extra apt packages for main
    toolchains: |                    # Dockerfile lines before the code is copied in
    after_app: |                     # Dockerfile lines after COPY app/ (dependency prefetch, editable installs)
    vendor: {name-in-vendor: path}   # copied into environment/vendor/
    resources: {cpus: 4, memory_mb: 8192, storage_mb: 20480, build_timeout_sec: 3600, agent_timeout_sec: 7200,
                verifier_timeout_sec: 1800}
    service_control: {app_cpus: 1.0, app_memory_mb: 2048, deploy_timeout_sec: 180, guide: "..."}
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

TEMPLATES = Path(__file__).parent / "templates"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
MANIFEST = "csf-service.toml"
APP_ALLOW = ["status", "logs", "deploy", "restart", "stop", "start"]
INFRA_ALLOW = ["status", "logs", "restart"]


class LayoutError(ValueError):
    pass


@dataclass
class ServiceSpec:
    name: str
    kind: str = "app"
    runtime: str = ""
    runtime_dir: Optional[Path] = None
    command: str = ""
    build: str = ""
    port: Optional[int] = None
    health: str = ""
    depends_on: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    localhost_ports: List[int] = field(default_factory=list)
    compose: Dict[str, Any] = field(default_factory=dict)

    @property
    def health_cmd(self) -> str:
        """What test.sh (from main) runs to see the service is up."""
        host = "localhost" if self.port in self.localhost_ports else self.name
        if self.port and self.health.startswith("/"):
            return f"curl -fs {host}:{self.port}{self.health}"
        if self.port:
            return f"bash -c 'exec 3<>/dev/tcp/{host}/{self.port}'"
        return "true"


@dataclass
class EnvironmentSpec:
    apt: List[str] = field(default_factory=list)
    toolchains: str = ""
    after_app: str = ""
    vendor: Dict[str, Path] = field(default_factory=dict)
    resources: Dict[str, Any] = field(default_factory=dict)
    service_control: Dict[str, Any] = field(default_factory=dict)


def parse_services(raw: dict, manifest_dir: Path, repo_names: List[str]) -> List[ServiceSpec]:
    out: List[ServiceSpec] = []
    for i, s in enumerate(raw.get("csf_services") or []):
        if not isinstance(s, dict) or not s.get("name"):
            raise LayoutError(f"csf_services[{i}] needs a name")
        name = s["name"]
        if not NAME_RE.match(name) or name == "main" or name.startswith("harbor-"):
            raise LayoutError(f"csf_services[{i}].name {name!r} must match {NAME_RE.pattern}, not main or harbor-*")
        if name in repo_names:
            raise LayoutError(f"csf_services[{i}].name {name!r} collides with a repository folder under /app")
        kind = s.get("kind", "app")
        if kind not in ("app", "infra"):
            raise LayoutError(f"csf_services[{i}].kind must be app or infra, got {kind!r}")
        spec = ServiceSpec(
            name=name,
            kind=kind,
            command=s.get("command", ""),
            build=s.get("build", ""),
            port=s.get("port"),
            health=s.get("health", ""),
            depends_on=list(s.get("depends_on") or []),
            env={str(k): str(v) for k, v in (s.get("env") or {}).items()},
            localhost_ports=[int(p) for p in s.get("localhost_ports") or []],
            compose=dict(s.get("compose") or {}),
        )
        if kind == "app":
            rt = s.get("runtime") or {}
            if not rt.get("name") or not rt.get("dir"):
                raise LayoutError(f"csf_services[{i}] ({name}): an app service needs runtime.name and runtime.dir")
            spec.runtime, spec.runtime_dir = rt["name"], (manifest_dir / rt["dir"]).resolve()
            if not (spec.runtime_dir / "Dockerfile").is_file():
                raise LayoutError(f"csf_services[{i}] ({name}): {spec.runtime_dir}/Dockerfile is missing")
            if not spec.command:
                raise LayoutError(f"csf_services[{i}] ({name}): an app service needs a command")
        elif not spec.compose.get("image") and not spec.compose.get("build"):
            raise LayoutError(f"csf_services[{i}] ({name}): an infra service needs compose.image or compose.build")
        if spec.health and not spec.port:
            raise LayoutError(f"csf_services[{i}] ({name}): health needs a port")
        out.append(spec)
    return out


def parse_environment(raw: dict, manifest_dir: Path) -> EnvironmentSpec:
    e = raw.get("csf_environment") or {}
    vendor = {str(k): (manifest_dir / v).resolve() for k, v in (e.get("vendor") or {}).items()}
    for k, p in vendor.items():
        if not p.exists():
            raise LayoutError(f"csf_environment.vendor.{k}: {p} does not exist")
    return EnvironmentSpec(
        apt=list(e.get("apt") or []),
        toolchains=e.get("toolchains", "") or "",
        after_app=e.get("after_app", "") or "",
        vendor=vendor,
        resources=dict(e.get("resources") or {}),
        service_control=dict(e.get("service_control") or {}),
    )


# ------------------------------------------------------------------ environment/


def _toml_str(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def render_manifest(svc: ServiceSpec) -> str:
    lines = [
        f"# App service `{svc.name}`, deployed by Harbor when the stack starts and by `harbor-svc deploy {svc.name}`;",
        "# each grading stack deploys it from the agent's /app (test.sh). Keys: runtime, build, command, port, health,",
        "# depends_on, env.",
        f"runtime = {_toml_str(svc.runtime)}",
    ]
    if svc.build:
        lines.append(f"build = {_toml_str(svc.build)}")
    lines.append(f"command = {_toml_str(svc.command)}")
    if svc.port:
        lines.append(f"port = {svc.port}")
    if svc.health:
        lines.append(f"health = {_toml_str(svc.health)}")
    if svc.depends_on:
        lines.append(f"depends_on = {_toml_str(svc.depends_on)}")
    if svc.env:
        lines.append("env = { " + ", ".join(f"{k} = {_toml_str(v)}" for k, v in svc.env.items()) + " }")
    return "\n".join(lines) + "\n"


def render_compose(services: List[ServiceSpec]) -> str:
    import yaml

    forwards = [(s.name, p) for s in services for p in s.localhost_ports]
    app = [s.name for s in services if s.kind == "app"]
    infra = [s for s in services if s.kind == "infra"]
    head = [
        "# The agent's stack, and the base every step's grading stack is built from. GENERATED by DeepCommit.",
        "#   main    Dockerfile: the agent's container (repos at /app on the app-src volume, toolchains) and the test runner",
    ]
    for s in infra:
        head.append(f"#   {s.name:<7} infrastructure (listed here)")
    for n in app:
        head.append(f"#   {n}  app service, NOT listed here: Harbor deploys it from /app/{n}/{MANIFEST} into its runtime")
        head.append("#           image (task.toml [environment.service_control.runtimes])")
    if forwards:
        head += [
            "#",
            "# main forwards localhost ports to services with socat (clients that default to localhost); socat resolves",
            "# the name per connection, so a forward survives `harbor-svc restart` and `harbor-svc deploy`.",
        ]
    main: Dict[str, Any] = {"build": {"context": ".", "dockerfile": "Dockerfile"}}
    if forwards:
        loop = []
        for name, port in forwards:
            loop.append(f"socat TCP4-LISTEN:{port},bind=127.0.0.1,fork,reuseaddr TCP:{name}:{port} &")
            loop.append(
                f"socat TCP6-LISTEN:{port},bind=[::1],ipv6only=1,fork,reuseaddr TCP:{name}:{port} 2>/dev/null &"
            )
        main["command"] = ["sh", "-c", "\n".join(loop + ["exec sleep infinity"]) + "\n"]
    main["volumes"] = ["app-src:/app"]
    main["networks"] = ["appnet", "default"]
    doc: Dict[str, Any] = {"services": {"main": main}}
    for s in infra:
        body = dict(s.compose)
        body.setdefault("networks", ["appnet"])
        doc["services"][s.name] = body
    doc["volumes"] = {"app-src": None}
    doc["networks"] = {"appnet": {"internal": True}}
    class _Dumper(yaml.SafeDumper):
        pass

    def _str(dumper, value):
        style = "|" if "\n" in value else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)

    _Dumper.add_representer(str, _str)
    text = yaml.dump(doc, Dumper=_Dumper, sort_keys=False, default_flow_style=False, width=120)
    text = text.replace("app-src: null", "app-src:")
    return "\n".join(head) + "\n" + text


def write_runtimes(env_dir: Path, services: List[ServiceSpec]) -> Dict[str, str]:
    """Copy each app service's runtime folder to environment/runtimes/<name>/ (csf-run / csf-health added when absent)."""
    catalog: Dict[str, str] = {}
    for s in services:
        if s.kind != "app" or s.runtime in catalog:
            continue
        dst = env_dir / "runtimes" / s.runtime
        shutil.copytree(s.runtime_dir, dst, dirs_exist_ok=True)
        for helper in ("csf-run", "csf-health"):
            if not (dst / helper).exists():
                shutil.copy2(TEMPLATES / "runtime" / helper, dst / helper)
            (dst / helper).chmod(0o755)
        catalog[s.runtime] = f"runtimes/{s.runtime}"
    return catalog


def write_vendor(env_dir: Path, env: EnvironmentSpec) -> None:
    for name, src in env.vendor.items():
        dst = env_dir / "vendor" / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)


def suites_config(
    suites: List[Dict[str, Any]], contract_only: Dict[str, Any], services: List[ServiceSpec]
) -> Dict[str, Any]:
    """_src/suites.json: what render.py puts in every step's suite.json."""
    return {
        "suites": suites,
        "contract_only": contract_only,
        "services": [{"name": s.name, "kind": s.kind, "health": s.health_cmd} for s in services],
    }


# ------------------------------------------------------------------ task.toml


def render_service_control(services: List[ServiceSpec], env: EnvironmentSpec, catalog: Dict[str, str]) -> List[str]:
    if not services:
        return []
    sc = env.service_control
    app = [s for s in services if s.kind == "app"]
    guide = sc.get("guide") or "\n".join(
        f"{s.name}: {'app service from /app/' + s.name + '/' + MANIFEST if s.kind == 'app' else 'infrastructure'}"
        + (f"; main forwards localhost:{', localhost:'.join(map(str, s.localhost_ports))} to it" if s.localhost_ports else "")
        + "."
        for s in services
    )
    t = [
        "",
        "# harbor-svc (Harbor's docs/service-control.md)"
        + (": manifest mode, app services come from /app/<svc>/csf-service.toml" if app else "")
        + ". Deployed when the agent's stack starts; each grading stack's test.sh deploys them from the agent's /app.",
        "[environment.service_control]",
        "enabled = true",
        "mcp = true",
        f"max_calls_per_minute = {sc.get('max_calls_per_minute', 60)}",
        f"operation_timeout_sec = {sc.get('operation_timeout_sec', 120)}",
    ]
    if app:
        t += [
            f"deploy_timeout_sec = {sc.get('deploy_timeout_sec', 180)}",
            f"app_cpus = {float(sc.get('app_cpus', 1.0))}",
            f"app_memory_mb = {int(sc.get('app_memory_mb', 2048))}",
        ]
    t += ['guide = """', guide.strip(), '"""']
    if app:
        t += ["", "[environment.service_control.runtimes]"]
        t += [f"{_toml_str(n)} = {_toml_str(p)}" for n, p in catalog.items()]
        t += ["", "[environment.service_control.app_services]", f"allow = {_toml_str(APP_ALLOW)}"]
    for s in services:
        if s.kind == "infra":
            t += ["", f"[environment.service_control.services.{s.name}]", f"allow = {_toml_str(INFRA_ALLOW)}"]
    return t


def render_task_toml(meta: Dict[str, Any], services: List[ServiceSpec], env: EnvironmentSpec,
                     catalog: Dict[str, str]) -> str:
    """task.toml of the service layout. ``meta`` as csf_task._render_toml; ``cards`` optional milestone cards."""
    res = {
        "build_timeout_sec": 3600, "cpus": 4, "memory_mb": 8192, "storage_mb": 20480,
        "agent_timeout_sec": 7200, "verifier_timeout_sec": 1800, **env.resources,
    }  # fmt: skip
    ws_repos = meta["source_repos"]
    contract = next((r for r in ws_repos if r["name"] == meta["contract_repo"]), ws_repos[0])
    excludes = sorted({ex for ex in meta.get("artifact_excludes", [])} | {"__pycache__", ".pytest_cache"})
    t = [
        'schema_version = "1.4"',
        'multi_step_reward_strategy = "mean"',
        "",
        "# The agent's whole code tree is the submission: after each step Harbor copies /app out of the agent's stack into",
        "# a fresh grading stack, where test.sh resets the test dirs and runs the step's checks. Build outputs stay behind.",
        f'artifacts = [{{ source = "/app", required = true, exclude = {_toml_str(excludes)} }}]',
        "",
        "[task]",
        f"name = {_toml_str('csf/' + meta['slug'])}",
        'version = "0.1.0"',
        f"description = {_toml_str(meta['description'])}",
        "keywords = "
        + _toml_str(["csf", meta["family"], *meta["languages"], "multi-repo", "multi-step"]
                    + (["service-control"] if services else [])),
        "",
        "[[task.authors]]",
        'name = "Deccan AI"',
        'email = "csf@deccan.ai"',
        "",
        "[metadata]",
        f"family = {_toml_str(meta['family'])}",
        f"source_repo = {_toml_str(contract['url'])}",
        f"source_commit = {_toml_str(contract['commit'])}",
        f"milestones_planned = {meta['planned']}",
        f"milestones_built = {meta['built']}",
        f"languages = {_toml_str(meta['languages'])}",
        f"repos = {_toml_str(meta['repos'])}",
        "repo_roles = { " + ", ".join(f"{_toml_str(k)} = {_toml_str(v)}" for k, v in meta["repo_roles"].items()) + " }",
        f"contract_only_repos = {_toml_str(meta['contract_only_repos'])}",
        f"services = {_toml_str([s.name for s in services])}",
        "source_repos = [",
        *[f"  {{ name = {_toml_str(r['name'])}, url = {_toml_str(r['url'])}, commit = {_toml_str(r['commit'])} }},"
          for r in ws_repos],
        "]",
        'csf_task_source = "derived"',
        f"csf_milestone_dag = {_toml_str(meta['dag'])}",
    ]
    if meta.get("license"):
        t.append(f"source_license = {_toml_str(meta['license'])}")
    for card in meta.get("cards", []):
        t += ["", "[[metadata.milestone_cards]]"] + [f"{k} = {_card_value(v)}" for k, v in card.items()]
    t += [
        "",
        "[environment]",
        f"build_timeout_sec = {res['build_timeout_sec']}",
        f"cpus = {res['cpus']}",
        f"memory_mb = {res['memory_mb']}",
        f"storage_mb = {res['storage_mb']}",
        'workdir = "/app"',
    ]
    hc = [s for s in services if s.health]
    if hc:
        t += [
            "",
            "# Runs once the stack is up (after Harbor deployed the app services).",
            "[environment.healthcheck]",
            f"command = {_toml_str(' && '.join(s.health_cmd for s in hc))}",
            "interval_sec = 2.0",
            "timeout_sec = 10.0",
            "retries = 30",
        ]
    t += render_service_control(services, env, catalog)
    t += [
        "",
        "[agent]",
        f"timeout_sec = {res['agent_timeout_sec']}",
        "",
        "# Every step is graded in a fresh stack built from environment/, with the agent's /app uploaded.",
        "[verifier]",
        'environment_mode = "separate"',
        f"timeout_sec = {res['verifier_timeout_sec']}",
    ]
    for name in meta["step_names"]:
        t += ["", "[[steps]]", f"name = {_toml_str(name)}", "[steps.verifier]", 'environment_mode = "separate"',
              f"env = {{ CSF_STEP = {_toml_str(name)} }}"]
    return "\n".join(t) + "\n"


def _card_value(v: Any) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_card_value(x)}" for k, x in v.items()) + " }"
    return _toml_str(v)


def rewrite_cards(task: Path, cards: List[Dict[str, Any]]) -> None:
    """Replace the [[metadata.milestone_cards]] tables of task.toml."""
    p = task / "task.toml"
    text = p.read_text(encoding="utf-8")
    text = re.sub(r"\n\[\[metadata\.milestone_cards\]\]\n(?:(?!\n\[)[^\n]*\n?)*", "\n", text)
    block = "".join(
        "\n[[metadata.milestone_cards]]\n" + "".join(f"{k} = {_card_value(v)}\n" for k, v in c.items()) for c in cards
    )
    anchor = text.index("\n[environment]\n")
    p.write_text(text[:anchor].rstrip("\n") + "\n" + block + text[anchor:], encoding="utf-8")


def step_cards(task: Path, milestones: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """One card per step: its milestones, the repos its gold patch touches, its checks by bucket."""
    import tomllib

    cards = []
    for st in tomllib.loads((task / "task.toml").read_text())["steps"]:
        sd = task / "steps" / st["name"]
        suite = read_suite(sd)
        gold = (sd / "solution" / "gold.patch")
        repos = sorted(set(re.findall(r"^diff --git a/([^/\s]+)/", gold.read_text(errors="replace"), re.M))) \
            if gold.exists() else []
        card: Dict[str, Any] = {"step": st["name"], "milestones": milestones.get(st["name"], []), "gold_repos": repos}
        if "checks" in suite:
            card["checks"] = {b: sum(c["bucket"] == b for c in suite["checks"]) for b in ("deploy", "f2p", "p2p")}
        cards.append(card)
    return cards


# ------------------------------------------------------------------ steps


def read_suite(step_dir: Path) -> Dict[str, Any]:
    return json.loads((step_dir / "tests" / "suite.json").read_text(encoding="utf-8"))


def write_suite(step_dir: Path, suite: Dict[str, Any]) -> None:
    (step_dir / "tests" / "suite.json").write_text(json.dumps(suite, indent=1) + "\n", encoding="utf-8")


def file_of(tid: str) -> Optional[str]:
    parts = tid.split("::", 2)
    return parts[1] if len(parts) >= 3 and parts[1].endswith(".py") else None


def set_checks(step_dir: Path, f2p: List[str], p2p: List[str], extra: Optional[Dict[str, Any]] = None) -> None:
    """Write a step's curated checks (deploy check per service, f2p, p2p) and the files test.sh runs for them."""
    suite = read_suite(step_dir)
    checks = [{"id": f"deploy::{s['name']}", "repo": s["name"], "bucket": "deploy"} for s in suite.get("services", [])]
    checks += [{"id": t, "repo": t.split("::", 1)[0], "bucket": "f2p"} for t in f2p]
    checks += [{"id": t, "repo": t.split("::", 1)[0], "bucket": "p2p"} for t in p2p]
    files: Dict[str, Dict[str, List[str]]] = {s["repo"]: {"f2p": [], "p2p": []} for s in suite["suites"]}
    for group, ids in (("f2p", f2p), ("p2p", p2p)):
        for tid in ids:
            f = file_of(tid)
            repo = tid.split("::", 1)[0]
            if f and repo in files and f not in files[repo][group]:
                files[repo][group].append(f)
    suite["checks"] = checks
    suite["files"] = files
    if extra:
        suite.update(extra)
    write_suite(step_dir, suite)


# ------------------------------------------------------------------ ledger


def write_ledger(task: Path) -> Dict[str, Any]:
    """ledger.json: every check by unique name -> family (the step whose f2p it is; 'base' when it passed before any
    step; 'infra' for deploy checks), version (distinct contents of its test file over the steps it is live in), first
    and last step, bucket per step, kind (own / versioned / carried / deploy). Never read at grading."""
    import tomllib

    steps = [s["name"] for s in tomllib.loads((task / "task.toml").read_text())["steps"]]
    tests: Dict[str, Dict[str, Any]] = {}
    hashes: Dict[str, Dict[str, str]] = {}
    for step in steps:
        sd = task / "steps" / step / "tests"
        suite = read_suite(task / "steps" / step)
        for c in suite.get("checks", []):
            e = tests.setdefault(c["id"], {"repo": c["repo"], "buckets": {}})
            e["buckets"][step] = c["bucket"]
        checks = sd / "checks"
        if checks.is_dir():
            for f in checks.rglob("*"):
                if f.is_file():
                    rel = f.relative_to(checks).parts
                    hashes.setdefault(f"{rel[0]}::{'/'.join(rel[1:])}", {})[step] = hashlib.sha256(
                        f.read_bytes()
                    ).hexdigest()[:12]
    out: Dict[str, Any] = {}
    for tid, e in sorted(tests.items()):
        live = [s for s in steps if s in e["buckets"]]
        if e["buckets"][live[0]] == "deploy":
            kind, family = "deploy", "infra"
        else:
            own = next((s for s in live if e["buckets"][s] == "f2p"), None)
            kind, family = ("own", own) if own else ("carried", "base")
        repo, rest = tid.split("::", 1)
        f = rest.split("::", 1)[0]
        if not f.endswith(".py"):
            f = f"tests/{f}.rs"  # cargo ids are <test binary>::<name>; integration binaries are tests/<binary>.rs
        h = hashes.get(f"{repo}::{f}", {})
        versions: List[Dict[str, str]] = []
        for s in live:
            if s in h and (not versions or versions[-1]["sha256_12"] != h[s]):
                versions.append({"from_step": s, "sha256_12": h[s]})
        if len(versions) > 1 and kind != "deploy":
            kind = "versioned"
        out[tid] = {
            "repo": e["repo"], "family": family, "version": max(len(versions), 1), "versions": versions,
            "first_step": live[0], "last_step": live[-1], "buckets": e["buckets"], "kind": kind,
        }  # fmt: skip
    ledger = {
        "_doc": "GENERATED by DeepCommit. Every check by unique name; never read at grading, used after trials.",
        "steps": steps,
        "tests": out,
    }
    (task / "ledger.json").write_text(json.dumps(ledger, indent=1) + "\n", encoding="utf-8")
    return ledger
