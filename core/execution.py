"""Central policy and execution engine for side-effecting actions.

Human approval is explicit and happens through core.confirm; ordinary assistant
operations remain available. This is consent + process management, not an OS
sandbox: approved code still runs with the current user's permissions.
"""
from __future__ import annotations

import os
import hashlib
import shlex
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


def _run_process(argv: Sequence[str], cwd: Path, timeout: int) -> str:
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
                raise
        output = stdout_path.read_bytes()[:MAX_OUTPUT_CHARS].decode("utf-8", "replace")
        error = stderr_path.read_bytes()[:MAX_OUTPUT_CHARS].decode("utf-8", "replace")
    combined = (output + ("\n" + error if error else "")).strip()
    if return_code:
        return f"Process exited with code {return_code}.\n{combined}"[:MAX_OUTPUT_CHARS]
    return combined or "Process completed successfully with no output."


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


def run_project_command(command: str, project_dir: str | Path, timeout: int | float | None = None) -> str:
    """Run a project command without shell interpretation, after caller approval."""
    cwd = resolve_user_path(project_dir, roots=[Path.home()], must_exist=True)
    if not cwd.is_dir():
        raise ExecutionPolicyError("Project working directory must be a directory.")
    return _run_process(parse_command(command), cwd, _validate_timeout(timeout, 'project'))


def install_dependencies(dependencies: Sequence[str], project_dir: str | Path,
                         timeout: int | float | None = None,
                         manager: str = "pip") -> str:
    """Install declared packages; call only inside an approved operation."""
    import re
    import shutil as _shutil

    cwd = resolve_user_path(project_dir, roots=[Path.home()], must_exist=True)
    if not cwd.is_dir():
        raise ExecutionPolicyError("Project working directory must be a directory.")
    if not dependencies:
        return "No external dependencies to install."
    if len(dependencies) > 50:
        raise ExecutionPolicyError("A maximum of 50 dependencies can be installed per request.")
    if manager not in {"pip", "npm"}:
        raise ExecutionPolicyError("Supported package managers are pip and npm.")
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
    if manager == "pip":
        argv = [sys.executable, "-m", "pip", "install", *packages]
    else:
        npm = _shutil.which("npm")
        if not npm:
            raise ExecutionPolicyError("npm is not installed or is not available on PATH.")
        argv = [npm, "install", *packages]
    return _run_process(argv, cwd, _validate_timeout(timeout or 180))
