"""GitHub CLI connection and repository picker for the local desktop UI.

Tokens are owned by GitHub CLI; Mark-LV never reads, prints, or stores them.
"""
from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from core.execution import _run_process, request_execution
from core.path_policy import resolve_user_path

_REPO_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_TOKEN_RE = re.compile(r"(?:gh[oprsu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})")


def _gh() -> str | None:
    return shutil.which("gh")


def _run_gh(args: list[str], *, timeout: int = 20) -> str:
    gh = _gh()
    if not gh:
        raise RuntimeError("GitHub CLI (gh) is not installed. Install GitHub CLI, then restart Mark-LV.")
    return _run_process([gh, *args], Path.home(), timeout, include_exit_status=True)


def _exit_ok(output: str) -> bool:
    return output.startswith("Process exited with code 0.")


def connection_status() -> tuple[bool, str]:
    """Return a display-safe auth status; no credential material is requested."""
    if not _gh():
        return False, "GitHub CLI is not installed. Install `gh` to connect."
    result = _run_gh(["auth", "status", "--active", "--hostname", "github.com"], timeout=15)
    if not _exit_ok(result):
        return False, "Not connected to github.com. Choose Connect to start browser authorization."
    account = "GitHub account connected."
    match = re.search(r"Logged in to github\.com account ([A-Za-z0-9-]+)", result, re.IGNORECASE)
    if match:
        account = f"Connected as {match.group(1)}."
    return True, account


def _stream_login(progress: Callable[[str], None] | None = None, timeout: int = 300) -> tuple[bool, str]:
    gh = _gh()
    if not gh:
        return False, "GitHub CLI (gh) is not installed. Install it, then restart Mark-LV."
    try:
        proc = subprocess.Popen(
            [gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web"],
            cwd=str(Path.home()), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, shell=False,
        )
    except OSError as exc:
        return False, f"Could not start GitHub CLI: {exc}"

    lines: queue.Queue[str | None] = queue.Queue()

    def read_output():
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                lines.put(_ANSI_RE.sub("", line).strip())
        finally:
            lines.put(None)

    reader = threading.Thread(target=read_output, name="github-auth-output", daemon=True)
    reader.start()
    collected: list[str] = []
    deadline = time.monotonic() + max(30, min(timeout, 600))
    stream_closed = False
    while proc.poll() is None or not stream_closed:
        if time.monotonic() >= deadline:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            return False, "GitHub authorization timed out. Start Connect again when ready."
        try:
            line = lines.get(timeout=0.2)
        except queue.Empty:
            continue
        if line is None:
            stream_closed = True
            continue
        if not line:
            continue
        line = _TOKEN_RE.sub("[credential redacted]", line)
        collected.append(line[:500])
        if len(collected) > 80:
            collected.pop(0)
        if progress:
            try:
                progress(line[:240])
            except Exception:
                pass

    if proc.returncode != 0:
        summary = "\n".join(collected[-6:])
        return False, (summary or "GitHub authorization did not complete.")[:2000]
    return True, "GitHub browser authorization completed."


def list_repositories(limit: int = 100) -> tuple[bool, str, list[dict]]:
    """List the authenticated user's visible repositories without reading contents."""
    connected, status = connection_status()
    if not connected:
        return False, status, []
    output = _run_gh([
        "repo", "list", "--limit", str(max(1, min(int(limit), 100))),
        "--json", "nameWithOwner,isPrivate,updatedAt",
    ], timeout=45)
    if not _exit_ok(output):
        return False, f"Could not list GitHub repositories: {output[:1800]}", []
    payload = output.partition("\n")[2]
    try:
        repositories = __import__("json").loads(payload)
    except (ValueError, TypeError):
        return False, "GitHub returned an unreadable repository list.", []
    if not isinstance(repositories, list):
        return False, "GitHub returned an invalid repository list.", []
    safe = [item for item in repositories[:100]
            if isinstance(item, dict) and isinstance(item.get("nameWithOwner"), str)]
    return True, status, safe


def _clone(repo_name: str, destination: Path) -> str:
    gh = _gh()
    if not gh:
        raise RuntimeError("GitHub CLI was removed before the clone began.")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".marklv-github-clone-", dir=parent) as temp:
        checkout = Path(temp) / "checkout"
        result = _run_process(
            [gh, "repo", "clone", repo_name, str(checkout)], parent, 300,
            include_exit_status=True,
        )
        if not _exit_ok(result):
            raise RuntimeError(f"GitHub clone failed:\n{result[:2500]}")
        if not checkout.is_dir() or not (checkout / ".git").exists():
            raise RuntimeError("GitHub reported success but the cloned repository is incomplete.")
        if destination.exists():
            raise RuntimeError("Clone destination appeared while the operation was running; no files were replaced.")
        os.replace(checkout, destination)
    return f"Repository cloned successfully: {destination}"


def connect_and_list(_values: dict | None = None, progress: Callable[[str], None] | None = None):
    """Connect through gh's browser/device flow, then list accessible repos."""
    try:
        connected, status = connection_status()
        if not connected:
            if not _gh():
                return False, status
            if progress:
                progress("Opening GitHub's browser authorization. Follow the one-time device-code instructions…")
            ok, message = _stream_login(progress=progress)
            if not ok:
                return False, message
            connected, status = connection_status()
            if not connected:
                return False, "GitHub CLI finished authorization but the account could not be verified. " + status
        ok, status, repositories = list_repositories()
        if not ok:
            return False, status
        visible = [f"{'Private' if item.get('isPrivate') else 'Public'} · {item['nameWithOwner']}"
                   for item in repositories[:25]]
        more = f"\n…and {len(repositories) - 25} more" if len(repositories) > 25 else ""
        listing = "\n".join(visible) if visible else "No repositories were returned for this account."
        return True, f"{status}\nAccessible repositories (up to 25 of {len(repositories)}):\n{listing}{more}\n\nMark-LV does not read or store your token; GitHub CLI manages its credential. Review the requested access in GitHub's authorization page."
    except Exception as exc:
        return False, f"GitHub connection failed: {exc}"


def clone_from_settings(values: dict):
    repo_name = (values.get("repository") or "").strip()
    raw_parent = (values.get("clone_folder") or "").strip()
    if not _REPO_RE.fullmatch(repo_name):
        return False, "Enter a repository as owner/name."
    try:
        parent = resolve_user_path(raw_parent or str(Path.home() / "Documents" / "GitHub"), roots=[Path.home()])
        destination = resolve_user_path(parent / repo_name.rsplit("/", 1)[-1], roots=[Path.home()])
        if destination.parent != parent:
            return False, "Clone destination must be a direct child of the selected folder."
        if destination.exists():
            return False, f"The clone destination already exists: {destination}"
        connected, status = connection_status()
        if not connected:
            return False, status
        detail = (
            f"GitHub repository: {repo_name}\nLocal destination: {destination}\n"
            "This downloads the selected repository to this computer. It will not overwrite an existing path."
        )
        result = request_execution(
            key=f"github-clone:{destination}",
            fingerprint=f"{repo_name}\n{destination}",
            category="project", permissions=("network.github", "filesystem.write"),
            title=f"Approve GitHub clone: {repo_name}", detail=detail,
            run=lambda: _clone(repo_name, destination),
        )
        return True, result
    except Exception as exc:
        return False, f"Clone was not started: {exc}"


def disconnect():
    try:
        connected, status = connection_status()
        if not connected:
            return True, status
        return True, request_execution(
            key="github-disconnect:github.com", fingerprint="logout:github.com",
            category="account", permissions=("account.write",),
            title="Approve GitHub disconnection",
            detail=("Disconnect github.com from GitHub CLI on this computer. "
                    "This removes the CLI's saved authentication for GitHub and affects other local projects that use it."),
            run=lambda: _logout(),
        )
    except Exception as exc:
        return False, f"Could not disconnect GitHub: {exc}"


def _logout() -> str:
    result = _run_gh(["auth", "logout", "--hostname", "github.com"], timeout=30)
    if not _exit_ok(result):
        raise RuntimeError(result[:1500])
    return "GitHub authentication removed from GitHub CLI on this computer."


def settings_schema() -> dict:
    try:
        from memory.config_manager import get_plugin_config
        values = get_plugin_config("github_connection")
    except Exception:
        values = {}
    return {
        "plugin": "github_connection",
        "namespace": "github_connection",
        "title": "GITHUB · REPOSITORIES",
        "description": (
            "Connect using GitHub CLI browser authorization. Tokens stay with GitHub CLI. "
            "The authorization page shows the scopes requested; GitHub CLI's standard scopes include repository and organization access."
        ),
        "fields": [
            {"key": "repository", "label": "Repository to clone (owner/name)", "type": "text",
             "placeholder": "owner/repository"},
            {"key": "clone_folder", "label": "Local parent folder for clones", "type": "text",
             "default": str(Path.home() / "Documents" / "GitHub")},
        ],
        "values": values,
        "actions": [
            {"label": "CONNECT / REFRESH REPOSITORIES", "run": connect_and_list, "progress": True},
            {"label": "CLONE SELECTED REPOSITORY", "run": clone_from_settings},
            {"label": "DISCONNECT GITHUB", "run": lambda _values: disconnect()},
        ],
    }

