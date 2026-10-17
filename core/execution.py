"""Central policy and execution engine for side-effecting actions.

Human approval is explicit and happens through core.confirm. Generated project
commands and dependency installs use the constrained Docker runner below; the
other child-process helpers may still run with the current user's permissions.
"""
from __future__ import annotations

import os
import hashlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Sequence

from core.path_policy import PathPolicyError, resolve_user_path

MAX_CODE_BYTES = 256 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TIMEOUT_SECONDS = 300
DEFAULT_TIMEOUT_SECONDS = 60
MAX_OUTPUT_CHARS = 12_000
MAX_OUTPUT_BYTES = 1024 * 1024


class ExecutionPolicyError(ValueError):
    """Raised when an execution request exceeds the declared policy."""


def _log(player, message: str) -> None:
    if player is not None:
        try:
            player.write_log(f"[Execution] {message}")
        except Exception:
            pass


def request_execution(
    *,
    key: str,
    title: str,
    detail: str,
    operation: Callable[[], str],
    player=None,
    category: str = "general",
    permissions: Sequence[str] = (),
    fingerprint: str = "",
) -> str:
    """Queue an operation for approval with a stable deduplication key."""
    from core import confirm
    from core.operation_registry import stable_key

    # Callers should supply a payload fingerprint where the visible detail does
    # not uniquely describe the effect (for example generated desktop code).
    payload = fingerprint or f"{title}\n{detail}"
    idempotency_key = f"{key}:{stable_key(payload)}"

    def approved_operation() -> str:
        started = time.monotonic()
        _log(player, f"Started: {title} [category={category}; permissions={','.join(permissions) or 'none'}]")
        try:
            result = operation() or "Execution completed."
            elapsed = time.monotonic() - started
            _log(player, f"Finished: {title} ({elapsed:.1f}s; status=completed)")
            if result:
                _log(player, result[:MAX_OUTPUT_CHARS])
            return result
        except Exception as exc:
            elapsed = time.monotonic() - started
            _log(player, f"Failed: {title} ({elapsed:.1f}s; status=failed): {exc}")
            raise

    return confirm.request(
        key=idempotency_key, title=title, detail=detail, run=approved_operation,
        category=category, permissions=tuple(permissions),
    )


CATEGORY_TIMEOUTS = {
    "desktop": 60,
    "python_file": 60,
    "general": DEFAULT_TIMEOUT_SECONDS,
    "project": MAX_TIMEOUT_SECONDS,
    "dependency_install": MAX_TIMEOUT_SECONDS,
}


def _validate_timeout(timeout: int | float | None, category: str = "general") -> int:
    try:
        value = DEFAULT_TIMEOUT_SECONDS if timeout is None else int(timeout)
    except (TypeError, ValueError) as exc:
        raise ExecutionPolicyError("Timeout must be an integer within the category limit.") from exc
    category_limit = CATEGORY_TIMEOUTS.get(category, DEFAULT_TIMEOUT_SECONDS)
    if not 1 <= value <= min(MAX_TIMEOUT_SECONDS, category_limit):
        raise ExecutionPolicyError(
            f"Timeout for category '{category}' must be between 1 and {category_limit} seconds."
        )
    return value


def _run_process(argv: Sequence[str], cwd: Path, timeout: int,
                 on_terminate: Callable[[], None] | None = None,
                 include_exit_status: bool = False) -> str:
    if not argv or not argv[0]:
        raise ExecutionPolicyError("An executable command is required.")
    if len(argv) > 64 or any(not isinstance(arg, str) or len(arg) > 4096 for arg in argv):
        raise ExecutionPolicyError("Command arguments exceed the execution policy.")
    if sum(len(arg) for arg in argv) > 32_768:
        raise ExecutionPolicyError("Command is too long.")
    stdout_path = stderr_path = None
    with tempfile.TemporaryDirectory(prefix="marklv-exec-") as temp_dir:
        stdout_path = Path(temp_dir) / "stdout.log"
        stderr_path = Path(temp_dir) / "stderr.log"
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                list(argv),
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                shell=False,
                close_fds=True,
            )
            deadline = time.monotonic() + timeout
            try:
                while process.poll() is None:
                    if stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_OUTPUT_BYTES:
                        raise ExecutionPolicyError("Process output exceeded the 1 MiB limit.")
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"Command timed out after {timeout}s.")
                    time.sleep(0.05)
                return_code = process.returncode
            except (TimeoutError, ExecutionPolicyError):
                process.kill()
                process.wait()
                if on_terminate:
                    try:
                        on_terminate()
                    except Exception:
                        pass
                raise
        output = stdout_path.read_bytes()[:MAX_OUTPUT_CHARS].decode("utf-8", "replace")
        error = stderr_path.read_bytes()[:MAX_OUTPUT_CHARS].decode("utf-8", "replace")
    combined = (output + ("\n" + error if error else "")).strip()
    if return_code:
        return f"Process exited with code {return_code}.\n{combined}"[:MAX_OUTPUT_CHARS]
    success = combined or "Process completed successfully with no output."
    return f"Process exited with code 0.\n{success}" if include_exit_status else success


def run_python_file(path: str | Path, timeout: int | float | None = None) -> str:
    """Run a Python file after consent has been granted by the caller."""
    target = resolve_user_path(path, must_exist=True)
    if not target.is_file() or target.suffix.lower() != ".py":
        raise ExecutionPolicyError("The run action supports existing .py files only.")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ExecutionPolicyError("Python file exceeds the 2 MiB execution limit.")
    return _run_process([sys.executable, str(target)], target.parent, _validate_timeout(timeout, 'python_file'))


def run_python_code(code: str, timeout: int | float | None = None) -> str:
    """Run generated desktop Python in a separate process with a hard timeout.

    The child process is not a security sandbox and has the user's OS permissions.
    The caller must obtain explicit human approval before calling this function.
    """
    if not isinstance(code, str) or not code.strip():
        raise ExecutionPolicyError("Generated Python code is empty.")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ExecutionPolicyError("Generated Python code exceeds the 256 KiB limit.")
    timeout_value = _validate_timeout(timeout, 'desktop')
    bootstrap = r'''
import os, sys, time, shutil
from pathlib import Path
os_path = os.path
try:
    import pyautogui
except ImportError:
    pyautogui = None
namespace = {
    "__name__": "__main__",
    "Path": Path,
    "time": time,
    "shutil": type("shutil", (), {
        "copy2": shutil.copy2, "copytree": shutil.copytree,
        "disk_usage": shutil.disk_usage,
    })(),
    "os_path": os_path,
}
if pyautogui is not None:
    namespace["pyautogui"] = pyautogui
if sys.platform == "win32":
    try:
        import ctypes, winreg
        namespace["ctypes"] = ctypes
        namespace["winreg"] = type("winreg", (), {
            "OpenKey": winreg.OpenKey, "QueryValueEx": winreg.QueryValueEx,
            "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        })()
    except ImportError:
        pass
source = Path(sys.argv[1]).read_text(encoding="utf-8")
exec(compile(source, "<AI desktop task>", "exec"), namespace, namespace)
'''
    with tempfile.TemporaryDirectory(prefix="marklv-desktop-") as temp_dir:
        code_path = Path(temp_dir) / "generated_task.py"
        code_path.write_text(code, encoding="utf-8")
        return _run_process(
            [sys.executable, "-c", bootstrap, str(code_path)],
            Path.home(),
            timeout_value,
        )


def parse_command(command: str) -> list[str]:
    """Parse a model-generated command without invoking a shell."""
    if not isinstance(command, str) or not command.strip() or len(command) > 4096:
        raise ExecutionPolicyError("A valid project command is required.")
    try:
        argv = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        raise ExecutionPolicyError(f"Invalid command syntax: {exc}") from exc
    if not argv:
        raise ExecutionPolicyError("A valid project command is required.")
    return argv


_SANDBOX_IMAGES = {
    "python": "python:3.12-slim",
    "py": "python:3.12-slim",
    "javascript": "node:22-slim",
    "typescript": "node:22-slim",
    "js": "node:22-slim",
    "ts": "node:22-slim",
    "node": "node:22-slim",
}


def docker_sandbox_status(timeout: int = 4) -> tuple[bool, str]:
    """Check for an available Docker daemon without changing machine state."""
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker CLI was not found. Install Docker Desktop to run projects in a sandbox."
    try:
        result = subprocess.run(
            [docker, "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=max(1, min(int(timeout), 10)),
            shell=False, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"Docker sandbox is unavailable: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "Docker daemon is not running.").strip()
        return False, f"Docker sandbox is unavailable: {detail[:400]}"
    return True, f"Docker sandbox ready (engine {result.stdout.strip() or 'available'})."


def _docker_stop(docker: str, cid_file: Path) -> None:
    try:
        container_id = cid_file.read_text(encoding="ascii").strip()
        if container_id:
            subprocess.run(
                [docker, "stop", "--time", "1", container_id],
                capture_output=True, timeout=5, shell=False, check=False,
            )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        pass


def _run_in_docker(command: Sequence[str], project_dir: str | Path, *,
                   language: str, timeout: int | float | None,
                   network: bool = False, pythonpath: bool = False,
                   image_override: str | None = None) -> str:
    """Run only the requested project directory in a constrained container.

    Runtime containers have no network, no Linux capabilities, a read-only root
    filesystem, bounded memory/CPU/PIDs, and a single writable project mount.
    Dependency installation opts into network access but keeps the same limits.
    This requires a running Docker daemon; there is deliberately no host fallback.
    """
    docker = shutil.which("docker")
    if not docker:
        raise ExecutionPolicyError("Docker is required; refusing to run this project on the host.")
    image = image_override or _SANDBOX_IMAGES.get(str(language or "").strip().lower())
    if not image:
        raise ExecutionPolicyError(f"No sandbox image is configured for language '{language}'.")
    cwd = resolve_user_path(project_dir, roots=[Path.home()], must_exist=True)
    if not cwd.is_dir():
        raise ExecutionPolicyError("Project working directory must be a directory.")
    if not command or any(not isinstance(arg, str) or not arg for arg in command):
        raise ExecutionPolicyError("Sandbox command must be a non-empty argument list.")

    timeout_value = _validate_timeout(timeout, "project")
    with tempfile.TemporaryDirectory(prefix="marklv-docker-") as temp_dir:
        cid_file = Path(temp_dir) / "container.id"
        argv = [
            docker, "run", "--rm", "--pull=missing", "--init",
            "--network=bridge" if network else "--network=none",
            "--cap-drop=ALL", "--security-opt=no-new-privileges:true",
            "--pids-limit=128", "--memory=1g", "--cpus=2",
            "--read-only", "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
            "--cidfile", str(cid_file),
            "--volume", f"{cwd}:/workspace:rw", "--workdir", "/workspace",
            "--env", "PYTHONDONTWRITEBYTECODE=1",
        ]
        if image_override:
            argv.extend(["--shm-size=256m"])
        if pythonpath:
            argv.extend(["--env", "PYTHONPATH=/workspace/.marklv_site"])
        argv.extend([image, *command])
        return _run_process(
            argv, cwd, timeout_value,
            on_terminate=lambda: _docker_stop(docker, cid_file),
            include_exit_status=True,
        )


def run_project_command(command: str, project_dir: str | Path, timeout: int | float | None = None) -> str:
    """Run a project command on the host after caller approval.

    Kept for explicitly trusted host workflows. New development-agent code must
    use run_sandboxed_project_command instead.
    """
    cwd = resolve_user_path(project_dir, roots=[Path.home()], must_exist=True)
    if not cwd.is_dir():
        raise ExecutionPolicyError("Project working directory must be a directory.")
    return _run_process(parse_command(command), cwd, _validate_timeout(timeout, 'project'))


def run_sandboxed_project_command(command: str, project_dir: str | Path,
                                 timeout: int | float | None = None,
                                 language: str = "python") -> str:
    """Run a generated project in Docker with runtime networking disabled."""
    return _run_in_docker(
        parse_command(command), project_dir, language=language,
        timeout=timeout, network=False,
        pythonpath=str(language).lower() in {"python", "py"},
    )


def run_playwright_sandboxed_project_command(command: str, project_dir: str | Path,
                                              playwright_version: str,
                                              timeout: int | float | None = None) -> str:
    """Run approved Playwright E2E checks in a version-matched Chromium container."""
    import re
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(playwright_version or "")):
        raise ExecutionPolicyError("Playwright browser image requires an exact resolved x.y.z package version.")
    return _run_in_docker(
        parse_command(command), project_dir, language="node", timeout=timeout,
        network=False, image_override=f"mcr.microsoft.com/playwright:v{playwright_version}-noble",
    )


def run_playwright_python_sandboxed_project_command(command: str, project_dir: str | Path,
                                                     playwright_version: str,
                                                     timeout: int | float | None = None) -> str:
    """Run Python Playwright/pytest checks in the matching browser image."""
    import re
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(playwright_version or "")):
        raise ExecutionPolicyError("Playwright browser image requires an exact resolved x.y.z package version.")
    return _run_in_docker(
        parse_command(command), project_dir, language="python", timeout=timeout,
        network=False, pythonpath=True,
        image_override=f"mcr.microsoft.com/playwright/python:v{playwright_version}-noble",
    )


def install_dependencies(dependencies: Sequence[str], project_dir: str | Path,
                         timeout: int | float | None = None,
                         manager: str = "pip", language: str | None = None) -> str:
    """Install packages in an isolated project container after approval."""
    cwd = resolve_user_path(project_dir, roots=[Path.home()], must_exist=True)
    if not cwd.is_dir():
        raise ExecutionPolicyError("Project working directory must be a directory.")
    if not dependencies:
        return "No external dependencies to install."
    if manager not in {"pip", "npm"}:
        raise ExecutionPolicyError("Supported package managers are pip and npm.")
    packages = validate_dependencies(dependencies)
    language = language or ("node" if manager == "npm" else "python")
    if manager == "pip":
        argv = ["python", "-m", "pip", "install", "--disable-pip-version-check",
                "--no-cache-dir", "--target=/workspace/.marklv_site", *packages]
    else:
        argv = ["npm", "install", "--no-audit", "--no-fund", *packages]
    return _run_in_docker(
        argv, cwd, language=language,
        timeout=_validate_timeout(180 if timeout is None else timeout, "dependency_install"),
        network=True,
    )


def validate_dependencies(dependencies: Sequence[str]) -> list[str]:
    """Validate package names before they enter an approval preview."""
    import re

    if not isinstance(dependencies, (list, tuple)):
        raise ExecutionPolicyError("Dependencies must be a list of package names.")
    if len(dependencies) > 50:
        raise ExecutionPolicyError("A maximum of 50 dependencies can be installed per request.")
    package_pattern = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._@/-]{0,99}$")
    packages = []
    for dependency in dependencies:
        if (not isinstance(dependency, str)
                or not package_pattern.fullmatch(dependency)
                or dependency.startswith("-")
                or ".." in dependency
                or dependency.startswith(("/", "\\\\"))):
            raise ExecutionPolicyError(f"Invalid package name: {dependency!r}")
        packages.append(dependency)
    return packages
