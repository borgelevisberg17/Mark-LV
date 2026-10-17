"""On-demand, approval-gated Git, CI, and GitHub workflows for Mark-LV."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

from core.execution import (
    _run_process,
    docker_sandbox_status,
    request_execution,
    run_sandboxed_project_command,
)
from core.path_policy import PathPolicyError, resolve_user_path


_BRANCH_RE = re.compile(r"^(feature|fix|chore|docs|refactor|test|release)/[a-z0-9][a-z0-9._/-]{0,70}$")
_SEMVER_RE = re.compile(r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$")
_MAX_FILE_BYTES = 2 * 1024 * 1024


def _repo_path(raw: str | None) -> Path:
    root = Path(__file__).resolve().parent.parent
    requested = raw.strip() if isinstance(raw, str) and raw.strip() else str(root)
    path = resolve_user_path(requested, roots=[Path.home()], must_exist=True)
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        raise ValueError("Repository path must be a directory.")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git CLI is not installed or not available on PATH.")
    output = _run_process([git, "rev-parse", "--show-toplevel"], path, 10)
    if output.startswith("Process exited with code"):
        raise ValueError(f"This path is not inside a Git repository: {path}")
    top = Path(output.strip()).resolve()
    return resolve_user_path(top, roots=[Path.home()], must_exist=True)


def _git(repo: Path, *args: str, timeout: int = 20, with_status: bool = False) -> str:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git CLI is not installed or not available on PATH.")
    output = _run_process([git, *args], repo, timeout, include_exit_status=with_status)
    if not with_status and output == "Process completed successfully with no output.":
        return ""
    return output


def _require_success(output: str, context: str) -> str:
    if output.lower().startswith("process exited with code") and not output.lower().startswith("process exited with code 0."):
        raise RuntimeError(f"{context} failed:\n{output[:3000]}")
    return output


def _clean_status(repo: Path) -> tuple[str, str]:
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    staged = _git(repo, "diff", "--cached", "--name-only")
    return status.strip(), staged.strip()


def _ci_workflow(language: str) -> str:
    common = """name: CI

on:
  push:
  pull_request:
  workflow_dispatch:

permissions:
  contents: read

concurrency:
  group: ci-${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}
  cancel-in-progress: true

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
"""
    if language == "python":
        return common + """      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: '3.12'
      - name: Install dependencies
        run: |
          if [ -f requirements.txt ]; then python -m pip install -r requirements.txt; fi
          if [ -f pyproject.toml ]; then python -m pip install -e .; fi
      - name: Run tests
        run: python -m unittest discover -v
"""
    return common + """      - uses: actions/setup-node@949feb2413d6458794dcd2491c4babbbce0c15c1 # v7.1.0
        with:
          node-version: '22'
          cache: npm
      - name: Install dependencies
        run: npm ci
      - name: Run tests
        run: npm test
      - name: Build
        run: npm run build --if-present
"""


def _atomic_write(path: Path, content: str) -> None:
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_FILE_BYTES:
        raise ValueError("Generated CI workflow exceeds the file size limit.")
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", prefix=".marklv-devops-", dir=path.parent, delete=False) as tmp:
            temp_name = tmp.name
            tmp.write(encoded)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(temp_name, path)
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _audit(repo: Path) -> str:
    branch = _git(repo, "branch", "--show-current").strip() or "(detached HEAD)"
    status = _git(repo, "status", "--short", "--branch").strip()
    recent = _git(repo, "log", "-5", "--oneline", "--decorate", timeout=15)
    diff = _git(repo, "diff", "--stat", "HEAD", timeout=15)
    remote = _git(repo, "remote", "-v").strip()
    # Avoid displaying embedded credentials in malformed HTTPS remote URLs.
    remote = re.sub(r"(https?://)[^/@\s]+:[^/@\s]+@", r"\1[credentials-redacted]@", remote)
    workflow_paths = sorted((repo / ".github" / "workflows").glob("*.yml")) + sorted(
        (repo / ".github" / "workflows").glob("*.yaml")
    )
    workflow_findings = []
    for path in workflow_paths[:30]:
        try:
            path = resolve_user_path(path, roots=[repo], must_exist=True)
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not re.search(r"(?m)^permissions:\s*$", content):
            workflow_findings.append(f"{path.relative_to(repo)}: no explicit top-level permissions block")
        for line_no, line in enumerate(content.splitlines(), 1):
            match = re.search(r"\buses:\s*[^\s]+@([^\s#]+)", line)
            if match and not re.fullmatch(r"[0-9a-fA-F]{40}", match.group(1)):
                workflow_findings.append(f"{path.relative_to(repo)}:{line_no}: action reference is not pinned to a full commit SHA")
    lines = [f"Repository: {repo}", f"Branch: {branch}", "", "Git status:", status or "(clean)", "", "Recent commits:", recent or "(no commits)", "", "Diff summary:", diff or "(no unstaged diff)", "", "Remotes:", remote or "(none configured)", "", "GitHub Actions review:"]
    if workflow_findings:
        lines.extend(f"- {item}" for item in workflow_findings)
    elif workflow_paths:
        lines.append("No missing permission blocks or mutable action refs found by the basic workflow scan.")
    else:
        lines.append("No GitHub Actions workflows found.")
    lines.append("Audit is static and local; it does not fetch remotes or execute repository code.")
    return "\n".join(lines)[:10_000]


def _setup_ci(repo: Path, language: str, player=None) -> str:
    if language not in {"python", "node"}:
        raise ValueError("CI bootstrap supports language='python' or language='node'.")
    if language == "python" and not (
        list((repo / "tests").rglob("test_*.py")) if (repo / "tests").is_dir() else []
    ) and not list(repo.glob("test_*.py")):
        raise ValueError("No Python tests were detected. Add tests/ or root test_*.py files before bootstrapping CI; this avoids a false-green workflow.")
    if language == "node":
        if not (repo / "package.json").is_file():
            raise ValueError("Node CI needs package.json at the repository root.")
        if not (repo / "package-lock.json").is_file():
            raise ValueError("Node CI needs a committed package-lock.json so CI can use deterministic npm ci installs.")
        try:
            package = json.loads((repo / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not parse package.json: {exc}") from exc
        if not isinstance(package, dict):
            raise ValueError("package.json must contain a JSON object.")
        if not isinstance(package.get("scripts"), dict) or not package["scripts"].get("test"):
            raise ValueError("Node CI needs a package.json test script; otherwise CI could pass without running tests.")
    workflow = repo / ".github" / "workflows" / "ci.yml"
    workflow = resolve_user_path(workflow, roots=[repo])
    if workflow.exists() and (not workflow.is_file() or workflow.stat().st_size > _MAX_FILE_BYTES):
        raise ValueError("Existing CI target is not a reviewable regular file.")
    proposed = _ci_workflow(language)
    old = workflow.read_text(encoding="utf-8", errors="replace") if workflow.exists() else ""
    if old == proposed:
        return f"The {language} CI workflow is already up to date: {workflow}"
    old_lines, new_lines = old.splitlines(), proposed.splitlines()
    import difflib
    preview = "\n".join(difflib.unified_diff(old_lines, new_lines, fromfile="existing/ci.yml" if old else "/dev/null", tofile="proposed/ci.yml", lineterm=""))[:5000]
    detail = (
        f"Repository: {repo}\nLanguage: {language}\nFile to create/update: .github/workflows/ci.yml\n"
        f"Overwrite existing file: {'yes' if old else 'no'}\n"
        "Workflow: push, pull request and manual triggers; read-only contents token; cancel superseded runs; time limit; actions pinned to full commit SHAs; lockfile-based Node install.\n"
        "This only writes the workflow locally. It does not commit, push, or trigger GitHub.\n\nProposed diff:\n" + preview
    )
    return request_execution(
        key=f"devops-ci-{hashlib.sha256(str(repo).encode()).hexdigest()[:16]}",
        fingerprint=hashlib.sha256((str(workflow) + proposed).encode()).hexdigest(),
        category="project", permissions=("filesystem.write",),
        title=f"Approve CI workflow for {repo.name}", detail=detail,
        operation=lambda: (_atomic_write(workflow, proposed) or f"CI workflow written: {workflow}"),
        player=player,
    )


def _validate_relative_files(files: object) -> list[str]:
    if not isinstance(files, list) or not files or len(files) > 50:
        raise ValueError("Select between 1 and 50 explicit repository-relative files.")
    result = []
    for item in files:
        if not isinstance(item, str) or not item.strip() or "\x00" in item:
            raise ValueError("File selections must be non-empty relative paths.")
        path = Path(item.replace("\\", "/"))
        if path.is_absolute() or any(part in {"..", "."} for part in path.parts):
            raise ValueError(f"Unsafe repository path: {item}")
        if item.startswith("-"):
            raise ValueError(f"Option-like paths are not accepted: {item}")
        normalized = path.as_posix()
        if normalized not in result:
            result.append(normalized)
    return result


def _create_branch(repo: Path, branch: str) -> str:
    _require_success(_git(repo, "switch", "-c", branch, with_status=True), "Creating branch")
    return f"Created local branch '{branch}' in {repo}. No remote was contacted."


def _commit(repo: Path, files: list[str], message: str) -> str:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git CLI is not installed.")
    _require_success(_run_process([git, "add", "--", *files], repo, 30, include_exit_status=True), "Staging selected files")
    staged = _git(repo, "diff", "--cached", "--name-only").splitlines()
    if sorted(staged) != sorted(files):
        raise RuntimeError("Staged paths do not match the approved file list; commit stopped.")
    result = _run_process([git, "commit", "-m", message], repo, 60, include_exit_status=True)
    return _require_success(result, "Commit")


def _create_pr(repo: Path, title: str, body: str, base: str) -> str:
    git, gh = shutil.which("git"), shutil.which("gh")
    if not git or not gh:
        raise RuntimeError("Git and GitHub CLI (gh) are required to create a pull request.")
    branch = _git(repo, "branch", "--show-current").strip()
    _require_success(_run_process([git, "push", "--set-upstream", "origin", branch], repo, 120, include_exit_status=True), "Pushing branch")
    result = _run_process([gh, "pr", "create", "--title", title, "--body", body, "--base", base], repo, 120, include_exit_status=True)
    return _require_success(result, "Creating pull request")


def _release(repo: Path, tag: str, message: str, publish: bool) -> str:
    git = shutil.which("git")
    if not git:
        raise RuntimeError("Git CLI is not installed.")
    _require_success(_run_process([git, "tag", "-a", tag, "-m", message], repo, 30, include_exit_status=True), "Creating annotated release tag")
    if publish:
        _require_success(_run_process([git, "push", "origin", tag], repo, 120, include_exit_status=True), "Publishing release tag")
        return f"Created and pushed release tag {tag}."
    return f"Created local annotated tag {tag}. It has not been pushed."


def devops(parameters: dict, player=None, speak=None) -> str:
    """Run one explicitly requested DevOps action; never starts background work."""
    del speak
    p = parameters if isinstance(parameters, dict) else {}
    raw_repo = p.get("repo_path")
    if raw_repo is not None and not isinstance(raw_repo, str):
        return "Repository path must be text."
    action = p.get("action", "audit")
    if not isinstance(action, str):
        return "Action must be text."
    action = action.strip().lower()
    try:
        if action == "repositories":
            from core.github_connection import list_repositories
            connected, status, repositories = list_repositories(limit=100)
            if not connected:
                return status
            lines = [status, f"Accessible GitHub repositories ({len(repositories)}):"]
            lines.extend(
                f"- {'Private' if item.get('isPrivate') else 'Public'} · {item['nameWithOwner']}"
                for item in repositories[:50]
            )
            if len(repositories) > 50:
                lines.append(f"…and {len(repositories) - 50} more")
            lines.append("Repository metadata only; no source files were read.")
            return "\n".join(lines)
        repo = _repo_path(p.get("repo_path"))
        if action == "audit":
            return _audit(repo)
        if action == "setup_ci":
            language = p.get("language", "")
            if not isinstance(language, str):
                return "Language must be text."
            return _setup_ci(repo, language.strip().lower(), player)
        if action == "verify":
            command = p.get("command")
            language = p.get("language", "python")
            if not isinstance(command, str) or not command.strip():
                return "Provide the exact test or build command to run."
            if not isinstance(language, str) or language.strip().lower() not in {"python", "py", "node", "javascript", "typescript", "js", "ts"}:
                return "Sandbox verification supports Python and Node projects."
            language = language.strip().lower()
            ready, status = docker_sandbox_status()
            if not ready:
                return f"Sandbox unavailable; nothing was executed. {status}"
            detail = f"Repository: {repo}\nLanguage: {language}\nCommand: {command}\nExecution: Docker, network disabled, bounded CPU/memory/processes and timeout. The repository is mounted as the sole writable project directory."
            return request_execution(
                key=f"devops-verify-{hashlib.sha256(str(repo).encode()).hexdigest()[:16]}",
                fingerprint=hashlib.sha256((str(repo) + language + command).encode()).hexdigest(),
                category="project", permissions=("process.sandbox",),
                title=f"Approve project verification: {repo.name}", detail=detail,
                operation=lambda: run_sandboxed_project_command(command, repo, timeout=180, language=language), player=player,
            )
        if action == "branch":
            branch = p.get("branch")
            if not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch) or ".." in branch or "//" in branch:
                return "Branch must follow a name such as feature/add-login, fix/cache-race, or chore/update-deps."
            status, _staged = _clean_status(repo)
            if status:
                return "Create a branch only after the working tree is clean; current changes were left untouched."
            if _git(repo, "show-ref", "--verify", f"refs/heads/{branch}").startswith("Process exited with code 0."):
                return f"Branch already exists: {branch}"
            detail = f"Repository: {repo}\nCreate local branch: {branch}\nWorking tree: clean\nNo remote will be contacted."
            return request_execution(key=f"devops-branch-{hashlib.sha256(str(repo).encode()).hexdigest()[:16]}", fingerprint=branch, category="project", permissions=("git.write",), title=f"Approve branch creation: {branch}", detail=detail, operation=lambda: _create_branch(repo, branch), player=player)
        if action == "commit":
            message, raw_files = p.get("message"), p.get("files")
            if not isinstance(message, str) or not message.strip() or len(message) > 200:
                return "Provide a concise commit message (1–200 characters)."
            if "\n" in message or "\r" in message:
                return "Commit message must be a single line."
            files = _validate_relative_files(raw_files)
            status, staged = _clean_status(repo)
            if not status:
                return "There are no repository changes to commit."
            branch = _git(repo, "branch", "--show-current").strip()
            if branch in {"", "main", "master", "develop", "production"}:
                return "Create and switch to a feature branch before committing; direct commits to the primary branch are blocked."
            if staged:
                return "Already staged changes were found. Unstage or review them first; the DevOps action will not mix them into this commit."
            changed = set(_git(repo, "diff", "--name-only", "HEAD").splitlines())
            changed.update(_git(repo, "ls-files", "--others", "--exclude-standard").splitlines())
            for filename in files:
                resolve_user_path(repo / filename, roots=[repo])
                if filename not in changed:
                    return f"Selected file is not changed: {filename}"
            detail = f"Repository: {repo}\nFiles to stage and commit:\n" + "\n".join(f"- {f}" for f in files) + f"\n\nCommit message: {message}\nOther changed files remain unstaged. Existing Git commit hooks may run on this computer. No push will occur."
            return request_execution(key=f"devops-commit-{hashlib.sha256(str(repo).encode()).hexdigest()[:16]}", fingerprint=hashlib.sha256((message + json.dumps(files)).encode()).hexdigest(), category="project", permissions=("git.write", "process.local"), title=f"Approve commit: {message[:70]}", detail=detail, operation=lambda: _commit(repo, files, message), player=player)
        if action == "pull_request":
            title, body, base = p.get("title"), p.get("body", ""), p.get("base", "main")
            if not isinstance(title, str) or not title.strip() or len(title) > 200 or not isinstance(body, str) or len(body) > 10_000:
                return "Pull request needs a title (up to 200 chars) and body (up to 10,000 chars)."
            if not isinstance(base, str) or not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", base) or ".." in base:
                return "Base branch name is invalid."
            status, _staged = _clean_status(repo)
            branch = _git(repo, "branch", "--show-current").strip()
            if status:
                return "Commit or discard working tree changes before opening a pull request; nothing was pushed."
            if branch in {"", "main", "master", "develop", "production"}:
                return "Switch to a feature branch before opening a pull request; nothing was pushed."
            gh = shutil.which("gh")
            if not gh:
                return "GitHub CLI (gh) is required. Install it and authenticate with 'gh auth login'; nothing was pushed."
            auth = _run_process([gh, "auth", "status"], repo, 10, include_exit_status=True)
            if not auth.startswith("Process exited with code 0."):
                return "GitHub CLI is not authenticated. Run 'gh auth login'; nothing was pushed."
            detail = f"Repository: {repo}\nPush branch to origin: {branch}\nCreate pull request into: {base}\nTitle: {title}\nBody:\n{body}\n\nThis contacts GitHub and pushes commits from the selected branch. It does not merge the pull request."
            return request_execution(key=f"devops-pr-{hashlib.sha256(str(repo).encode()).hexdigest()[:16]}", fingerprint=hashlib.sha256((branch + title + body + base).encode()).hexdigest(), category="project", permissions=("network.github", "git.push"), title=f"Approve GitHub pull request: {title[:70]}", detail=detail, operation=lambda: _create_pr(repo, title.strip(), body, base), player=player)
        if action == "release":
            raw_version, publish = p.get("version"), p.get("publish", False)
            message = p.get("message", "")
            if not isinstance(raw_version, str) or not _SEMVER_RE.fullmatch(raw_version.strip()):
                return "Release version must follow SemVer, for example 1.4.0 or 2.0.0-rc.1."
            version_match = _SEMVER_RE.fullmatch(raw_version.strip())
            prerelease = version_match.group(4) if version_match else None
            if prerelease and any(part.isdigit() and len(part) > 1 and part.startswith("0")
                                  for part in prerelease.split(".")):
                return "SemVer numeric prerelease identifiers cannot contain leading zeroes."
            if not isinstance(publish, bool) or not isinstance(message, str) or len(message) > 200:
                return "publish must be true or false and release message must be at most 200 characters."
            tag = "v" + raw_version.strip().removeprefix("v")
            status, staged = _clean_status(repo)
            if status or staged:
                return "A release requires a clean working tree; no tag was created."
            branch = _git(repo, "branch", "--show-current").strip()
            if branch not in {"main", "master"}:
                return "A release tag can only be created from main/master; switch branches and rerun the release audit."
            existing = _git(repo, "tag", "--list", tag).strip()
            if existing:
                return f"Release tag already exists: {tag}"
            detail = f"Repository: {repo}\nBranch: {branch} (clean)\nCreate annotated tag: {tag}\nRelease notes: {message or '(uses version as annotation)'}\nPublish to origin: {'yes' if publish else 'no'}\n"
            if publish:
                detail += "Publishing contacts GitHub and makes this release tag available remotely."
            else:
                detail += "The tag will remain local; nothing will be pushed."
            return request_execution(key=f"devops-release-{hashlib.sha256(str(repo).encode()).hexdigest()[:16]}", fingerprint=hashlib.sha256((tag + message + str(publish)).encode()).hexdigest(), category="project", permissions=("git.write", "network.github") if publish else ("git.write",), title=f"Approve release tag {tag}", detail=detail, operation=lambda: _release(repo, tag, message.strip() or tag, publish), player=player)
        return "Unknown DevOps action. Choose audit, setup_ci, verify, branch, commit, pull_request, or release."
    except (OSError, RuntimeError, ValueError, PathPolicyError) as exc:
        return f"DevOps action failed safely: {exc}"


TOOL = {
    "name": "devops",
    "description": (
        "On-demand DevOps for a local Git repository. Use ONLY when the user explicitly asks for Git, CI/CD, GitHub, versioning, pull requests, or release work; never run automatically during ordinary development. Supports read-only audit and authenticated repository listing; approval-gated GitHub Actions CI setup; sandboxed, approval-gated verification; feature branch creation; explicit-file commits; GitHub pull requests through authenticated gh CLI; and SemVer annotated release tags. Shows exact paths/commands/effects before asking for approval. Never merges, deploys, pushes, tags, writes, or installs dependencies without the corresponding explicit approval."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {"type": "STRING", "description": "audit | repositories | setup_ci | verify | branch | commit | pull_request | release. Default audit."},
            "repo_path": {"type": "STRING", "description": "Optional path to a local Git repository; defaults to this Mark-LV repository."},
            "language": {"type": "STRING", "description": "python or node for CI setup / sandbox verification."},
            "command": {"type": "STRING", "description": "Exact test/build command for verify; it runs in Docker with networking disabled."},
            "branch": {"type": "STRING", "description": "Conventional local branch name, e.g. feature/login, for branch action."},
            "files": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Explicit repository-relative files for commit; no implicit git add -A."},
            "message": {"type": "STRING", "description": "Single-line commit message or release annotation."},
            "title": {"type": "STRING", "description": "Pull request title."},
            "body": {"type": "STRING", "description": "Pull request description."},
            "base": {"type": "STRING", "description": "Pull request base branch (default main)."},
            "version": {"type": "STRING", "description": "Semantic version to tag, e.g. 1.4.0 or 2.0.0-rc.1."},
            "publish": {"type": "BOOLEAN", "description": "For release only: push the tag to origin after confirmation (default false)."},
        },
        "required": ["action"],
    },
    "handler": devops,
}
