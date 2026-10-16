import subprocess
import shutil
import sys
import json
import re
import time
from pathlib import Path
from core.path_policy import PathPolicyError, resolve_child_path, resolve_user_path


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
API_CONFIG_PATH  = BASE_DIR / "config" / "api_keys.json"
PROJECTS_DIR     = Path.home() / "Desktop" / "JarvisProjects"
MAX_FIX_ATTEMPTS = 5
# Model choice, timeout and fallback ladder all live in core/gemini.py.
from core import gemini

MODEL_PLANNER    = gemini.SMART
MODEL_WRITER     = gemini.SMART

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _get_model(model_name: str = gemini.SMART):
    """Planning and writing whole files — the reasoning tier, and a long
    deadline because the answer is a source file rather than a sentence."""
    class _W:
        def generate_content(self, contents):
            resp = gemini.call(contents, tier=model_name, timeout_ms=60000)
            if resp is None:
                raise RuntimeError("every Gemini model on the ladder failed")
            return resp

    return _W()


def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\r?\n?", "", text)
    text = re.sub(r"\r?\n?```\s*$", "", text)
    return text.strip()


def _is_rate_limit(error: Exception) -> bool:
    msg = str(error).lower()
    return "429" in msg or "quota" in msg or "resource_exhausted" in msg


def _parse_traceback(output: str, project_files: list[str]) -> tuple[str | None, int | None]:

    pattern = re.compile(r'File ["\']([^"\']+\.py)["\'],\s+line\s+(\d+)', re.IGNORECASE)
    matches = pattern.findall(output)

    for raw_path, line_str in reversed(matches):
        raw_name = Path(raw_path).name
        for pf in project_files:
            if Path(pf).name == raw_name or pf == raw_path or raw_path.endswith(pf):
                return pf, int(line_str)

    return None, None


def _classify_error(output: str) -> str:

    low = output.lower()

    if any(x in low for x in (
        "no module named", "modulenotfounderror", "importerror",
        "cannot find module", "cannot find package", "err_module_not_found",
    )):
        return "dependency_error"

    if "syntaxerror" in low or "invalid syntax" in low:
        return "syntax_error"
    
    if "cannot import" in low or "importerror" in low:
        return "import_error"

    if any(x in low for x in (
        "traceback", "exception", "error:", "nameerror", "typeerror",
        "attributeerror", "valueerror", "keyerror", "indexerror",
        "zerodivisionerror", "filenotfounderror", "permissionerror",
    )):
        return "runtime_error"

    return "none"


def _has_error(output: str, run_command: str) -> bool:
    
    low = output.lower()

    if "timed out" in low:
        return False

    if not output.strip():
        return False

    error_type = _classify_error(output)
    return error_type != "none"

class RateLimitError(Exception):
    pass


def _plan_project(description: str, language: str) -> dict:
    model = _get_model(MODEL_PLANNER)

    prompt = f"""You are a senior software architect. Create a minimal, complete file plan for this project.

Language: {language}
Description: {description}

Return ONLY valid JSON — no markdown, no explanation:
{{
  "project_name": "snake_case_name",
  "entry_point": "main.py",
  "files": [
    {{
      "path": "main.py",
      "description": "Entry point — what it does and which modules it imports",
      "imports": ["utils.helpers", "core.engine"]
    }},
    {{
      "path": "utils/helpers.py",
      "description": "Helper utilities — what functions it exposes",
      "imports": []
    }}
  ],
  "run_command": "python main.py",
  "dependencies": ["requests"]
}}

Critical rules:
1. List files in DEPENDENCY ORDER — files with no imports come first, entry point comes last.
2. The "imports" field must list every other project module this file imports (dot-notation, e.g. "utils.helpers").
3. Keep it minimal — only files truly needed.
4. Entry point must be in the files list.
5. Use relative paths only (e.g. "utils/helpers.py", not absolute paths).
6. Standard library modules (os, sys, json, etc.) do NOT go in "dependencies".

JSON:"""

    try:
        response = model.generate_content(prompt)
        raw = _strip_fences(response.text)
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Planner returned invalid JSON: {e}\nRaw: {response.text[:300]}")
    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _write_file(
    file_info: dict,
    project_description: str,
    all_files: list[dict],
    language: str,
    project_dir: Path,
    already_written: dict[str, str],
) -> str:
    model = _get_model(MODEL_WRITER)

    file_path = file_info.get("path", "")
    if not isinstance(file_path, str) or not file_path.strip():
        raise PathPolicyError("Generated project file path must be a non-empty relative path.")
    file_desc = file_info.get("description", "")
    file_imports = file_info.get("imports", [])

    file_list = "\n".join(
        f"  [{i+1}] {f['path']}: {f.get('description', '')}"
        for i, f in enumerate(all_files)
    )

    dependency_context = ""
    for dep_dotted in file_imports:
        dep_path = dep_dotted.replace(".", "/") + ".py"
        if dep_path in already_written:
            code_snippet = already_written[dep_path][:2000]
            dependency_context += f"\n\n--- {dep_path} (you must import from this) ---\n{code_snippet}"

    lang_rules = ""
    if language.lower() == "python":
        lang_rules = """
Python-specific rules:
- Use type hints for all function signatures.
- Add docstrings for all public functions and classes.
- Use if __name__ == "__main__": guard in the entry point.
- For relative imports within the project, use: from utils.helpers import foo  (match the project structure exactly).
- Do NOT use implicit relative imports (from . import ...) unless it's a proper package with __init__.py.
- If this is a package subdirectory, create __init__.py files where needed."""
    elif language.lower() in ("javascript", "typescript", "js", "ts"):
        lang_rules = """
JS/TS-specific rules:
- Use ES modules (import/export), not CommonJS (require).
- Add JSDoc comments for all exported functions.
- Handle promise rejections with try/catch in async functions."""

    prompt = f"""You are a senior {language} developer writing production-quality code for a real project.

Project goal: {project_description}

Complete project file structure (in dependency order):
{file_list}

{f"Dependencies this file must import from other project files:{dependency_context}" if dependency_context else ""}

Your task: Write the complete, working code for: {file_path}
Purpose of this file: {file_desc}
{f"This file imports from: {', '.join(file_imports)}" if file_imports else "This file has no project-internal imports."}

{lang_rules}

General rules:
- Output ONLY raw code. Absolutely no explanation, no markdown, no triple backticks.
- Write COMPLETE, RUNNABLE code — no placeholders, no "# TODO", no "pass" stubs.
- Every import must either be from the standard library, listed dependencies, or the project files shown above.
- Match import paths EXACTLY to the file paths in the project structure (e.g. if file is "utils/helpers.py", import as "from utils.helpers import ...").
- Use proper error handling (try/except) where I/O or network calls are made.
- The code must work correctly when the project entry point is run from the project root directory.

Code for {file_path}:"""

    try:
        response = model.generate_content(prompt)
        code = _strip_fences(response.text)

        full_path = resolve_child_path(project_dir, file_path, roots=[Path.home()])
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(code, encoding="utf-8")

        print(f"[DevAgent] ✅ Written: {file_path} ({len(code)} chars)")
        return code

    except Exception as e:
        if _is_rate_limit(e):
            raise RateLimitError(str(e))
        raise

def _install_dependencies(
    dependencies: list[str],
    project_dir: Path,
    manager: str = "pip",
) -> str:
    """Install declared packages from an already approved execution workflow."""
    from core.execution import install_dependencies

    return install_dependencies(dependencies, project_dir, timeout=180, manager=manager)



def _open_vscode(project_dir: Path) -> bool:
    # Launch only a concrete executable; never pass a dynamic path through cmd.exe.
    candidates = [
        shutil.which("Code.exe"),
        str(Path.home() / "AppData/Local/Programs/Microsoft VS Code/Code.exe"),
        r"C:\Program Files\Microsoft VS Code\Code.exe",
        r"C:\Program Files (x86)\Microsoft VS Code\Code.exe",
    ]
    for candidate in candidates:
        if not candidate or not Path(candidate).is_file():
            continue
        try:
            subprocess.Popen(
                [candidate, str(project_dir)],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(1.5)
            print(f"[DevAgent] VS Code opened: {project_dir}")
            return True
        except Exception:
            continue
    return False

def _run_project(run_command: str, project_dir: Path, timeout: int = 30) -> str:
    """Run a project command without shell interpretation."""
    from core.execution import run_project_command

    return run_project_command(run_command, project_dir, timeout=timeout)


def _try_auto_install(error_output: str, project_dir: Path) -> bool:
    """Best-effort missing-package recovery, only inside the approved workflow."""
    from core.execution import install_dependencies

    python_match = re.search(r"No module named ['\"]([A-Za-z0-9_.-]+)['\"]", error_output, re.IGNORECASE)
    node_match = re.search(r"(?:Cannot find module|Cannot find package) ['\"]([^'\"]+)['\"]", error_output, re.IGNORECASE)
    manager = "pip"
    package = None
    if node_match:
        package = node_match.group(1).strip()
        manager = "npm"
        if package.startswith((".", "/", "\\\\")):
            return False
    elif python_match:
        package = python_match.group(1).split(".")[0]
    if not package or package.startswith("-") or len(package) > 100:
        return False
    try:
        result = install_dependencies([package], project_dir, timeout=180, manager=manager)
        return not result.startswith("Process exited with code ")
    except Exception as exc:
        print(f"[DevAgent] Automatic dependency recovery failed: {exc}")
        return False


def _execute_project_workflow(
    dependencies: list[str],
    run_command: str,
    project_dir: Path,
    timeout: int,
    language: str,
    player=None,
) -> str:
    """Install declared dependencies, run the project and recover missing packages."""
    manager = "npm" if language.lower() in {"javascript", "typescript", "js", "ts", "node"} else "pip"
    if dependencies:
        install_result = _install_dependencies(dependencies, project_dir, manager=manager)
        if install_result.startswith("Process exited with code "):
            return f"Dependency installation failed.\n{install_result}"
    result = _run_project(run_command, project_dir, timeout=timeout)
    for _ in range(2):
        if _classify_error(result) != "dependency_error":
            break
        if not _try_auto_install(result, project_dir):
            break
        result = _run_project(run_command, project_dir, timeout=timeout)
    if player:
        try:
            player.write_log(f"[DevAgent] Project execution result: {result[:12000]}")
        except Exception:
            pass
    return result



def _fix_files(
    error_output: str,
    project_description: str,
    all_files: list[dict],
    file_codes: dict[str, str],
    language: str,
    project_dir: Path,
    entry_point: str,
) -> dict[str, str]:

    model = _get_model(MODEL_PLANNER)

    error_file, error_line = _parse_traceback(error_output, list(file_codes.keys()))
    error_type = _classify_error(error_output)

    files_to_fix: list[str] = []

    if error_file:
        files_to_fix.append(error_file)
        if error_type == "import_error":
            for fi in all_files:
                if error_file.replace("/", ".").replace(".py", "") in fi.get("imports", []):
                    p = fi["path"]
                    if p not in files_to_fix:
                        files_to_fix.append(p)
    else:
        files_to_fix.append(entry_point)

    updated_codes: dict[str, str] = {}

    for fix_path in files_to_fix:
        current_code = file_codes.get(fix_path, "")

        other_ctx = ""
        for fp, code in file_codes.items():
            if fp != fix_path and code:
                snippet = code[:1500] + ("..." if len(code) > 1500 else "")
                other_ctx += f"\n--- {fp} ---\n{snippet}\n"

        line_hint = f"\nError appears to be near line {error_line} in this file." if (
            error_line and fix_path == error_file
        ) else ""

        prompt = f"""You are an expert {language} debugger. Fix the broken file below.

Project goal: {project_description}

All project files:
{chr(10).join(f"  - {f['path']}: {f.get('description', '')}" for f in all_files)}

Other files for context (read-only — fix only the target file):
{other_ctx[:3500]}

File to fix: {fix_path}{line_hint}
Error type: {error_type}

Error output:
{error_output[:2500]}

Current (broken) code:
{current_code}

Rules:
- Output ONLY the complete fixed code. No explanation, no markdown, no backticks.
- Fix ALL errors visible in the error output.
- Keep all existing correct logic — do not remove working features.
- Ensure import paths match the actual project file structure exactly.
- Do NOT introduce new bugs or remove error handling.

Fixed code for {fix_path}:"""

        try:
            response = model.generate_content(prompt)
            fixed = _strip_fences(response.text)

            full_path = resolve_child_path(project_dir, fix_path, roots=[Path.home()])
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(fixed, encoding="utf-8")

            updated_codes[fix_path] = fixed
            print(f"[DevAgent] 🔧 Fixed: {fix_path}")

        except Exception as e:
            if _is_rate_limit(e):
                raise RateLimitError(str(e))
            print(f"[DevAgent] ⚠️ Could not fix {fix_path}: {e}")

    return updated_codes

def _build_project(
    description: str,
    language: str,
    project_name: str,
    timeout: int,
    speak=None,
    player=None,
) -> str:

    def log(msg: str):
        print(f"[DevAgent] {msg}")
        if player:
            player.write_log(f"[DevAgent] {msg}")

    log("Planning project structure...")
    try:
        plan = _plan_project(description, language)
    except RateLimitError:
        msg = "Rate limit reached, sir. Please try again in a moment."
        if speak: speak(msg)
        return msg
    except ValueError as e:
        msg = f"Planning failed: {e}"
        if speak: speak(msg)
        return msg

    requested_name = project_name or plan.get("project_name", "jarvis_project")
    if not isinstance(requested_name, str):
        requested_name = "jarvis_project"
    proj_name = re.sub(r"[^\w\-]", "_", requested_name).strip("._-") or "jarvis_project"
    try:
        projects_root = resolve_user_path(PROJECTS_DIR, roots=[Path.home()])
        projects_root.mkdir(parents=True, exist_ok=True)
        projects_root = resolve_user_path(projects_root, roots=[Path.home()])
        project_dir = resolve_child_path(projects_root, proj_name, roots=[Path.home()])
        project_dir.mkdir(parents=True, exist_ok=True)
        project_dir = resolve_user_path(project_dir, roots=[Path.home()])
    except (OSError, PathPolicyError) as e:
        return f"Project directory rejected by path policy: {e}"

    files        = plan.get("files", [])
    entry_point  = plan.get("entry_point", "main.py")
    run_command  = plan.get("run_command", f"python {entry_point}")
    dependencies = plan.get("dependencies", [])

    if not isinstance(files, list) or not files:
        return "The project planner returned an invalid or empty file list."
    if not isinstance(run_command, str) or not run_command.strip():
        return "The project planner returned an invalid run command."
    if not isinstance(dependencies, list) or any(
        not isinstance(item, str) or not item.strip() for item in dependencies
    ):
        return "The project planner returned an invalid dependency list."

    log(f"Project: {proj_name} | Files: {len(files)} | Entry: {entry_point}")

    def _dep_sort_key(fi: dict) -> int:
        return len(fi.get("imports", []))

    sorted_files = sorted(files, key=_dep_sort_key)

    file_codes: dict[str, str] = {}

    for file_info in sorted_files:
        file_path = file_info.get("path", "")
        if not file_path:
            continue

        log(f"Writing {file_path}...")
        for attempt in range(2):
            try:
                code = _write_file(
                    file_info=file_info,
                    project_description=description,
                    all_files=files,
                    language=language,
                    project_dir=project_dir,
                    already_written=file_codes,
                )
                file_codes[file_path] = code
                time.sleep(0.4)
                break
            except RateLimitError:
                if attempt == 0:
                    log("Rate limit — waiting 20s...")
                    time.sleep(20)
                else:
                    log(f"Rate limit retry failed for {file_path}, skipping.")
            except Exception as e:
                log(f"Failed to write {file_path}: {e}")
                break

    if not file_codes:
        msg = "I could not write any project files, sir."
        if speak: speak(msg)
        return msg

    _open_vscode(project_dir)

    from core.execution import ExecutionPolicyError, parse_command, request_execution
    try:
        parse_command(run_command)
        bounded_timeout = max(1, min(300, int(timeout or 30)))
        manager = "npm" if language.lower() in {"javascript", "typescript", "js", "ts", "node"} else "pip"
        dependency_summary = ", ".join(dependencies[:20]) if dependencies else "none declared"
        if len(dependencies) > 20:
            dependency_summary += f" (+{len(dependencies) - 20} more)"
        return request_execution(
            key=f"dev-agent-{proj_name}",
            title=f"Install dependencies and run project: {proj_name}",
            detail=(
                f"Project: {project_dir}\n"
                f"Command: {run_command}\n"
                f"Package manager: {manager}\n"
                f"Declared dependencies: {dependency_summary}\n"
                f"Timeout per command: {bounded_timeout}s. If execution reports a missing "
                "package, the assistant may try up to two automatic dependency-recovery "
                "attempts. Commands run with your current user's permissions; this is not "
                "an OS sandbox. Approve only if you trust the generated project."
            ),
            operation=lambda: _execute_project_workflow(
                dependencies, run_command, project_dir, bounded_timeout, language, player
            ),
            player=player,
        )
    except (ExecutionPolicyError, OSError, ValueError) as exc:
        return f"Project generated at {project_dir}, but execution could not be prepared: {exc}"


def dev_agent(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
    speak=None,
) -> str:
    p            = parameters or {}
    description  = p.get("description", "").strip()
    language     = p.get("language", "python").strip()
    project_name = p.get("project_name", "").strip()
    try:
        timeout = max(1, min(300, int(p.get("timeout", 30))))
    except (TypeError, ValueError):
        return "Timeout must be an integer between 1 and 300 seconds."

    if not description:
        return "Please describe the project you want me to build, sir."

    return _build_project(
        description  = description,
        language     = language,
        project_name = project_name,
        timeout      = timeout,
        speak        = speak,
        player       = player,
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "dev_agent",
    "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "description": {
                "type": "STRING",
                "description": "What the project should do"
            },
            "language": {
                "type": "STRING",
                "description": "Programming language (default: python)"
            },
            "project_name": {
                "type": "STRING",
                "description": "Optional project folder name"
            },
            "timeout": {
                "type": "INTEGER",
                "description": "Run timeout in seconds (default: 30)"
            }
        },
        "required": [
            "description"
        ]
    },
    "handler": dev_agent,
}
