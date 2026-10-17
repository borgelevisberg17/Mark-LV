"""Deterministic, reviewable quality checks for supported project stacks."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Mapping


_SCRIPT_ORDER = ("typecheck", "lint", "test", "build")
_REMOTE_URL = re.compile(r"https?://[^\s\"'<>)}]+", re.IGNORECASE)


def infer_project_language(files: Mapping[str, str], preferred: str = "") -> str:
    """Infer the runnable stack from source extensions without overriding mixed projects."""
    paths = [str(path).replace("\\", "/").casefold() for path in files]
    has_py = any(path.endswith(".py") for path in paths)
    has_ts = any(path.endswith((".ts", ".tsx")) for path in paths)
    has_js = any(path.endswith((".js", ".jsx", ".mjs", ".cjs")) for path in paths)
    if has_py and not (has_ts or has_js):
        return "python"
    if has_ts:
        return "typescript"
    if has_js:
        return "javascript"
    return preferred.strip().lower() or "python"


def inspect_frontend(files: Mapping[str, str]) -> dict[str, object]:
    """Perform bounded source-level UI/design/resource checks without claiming visual QA."""
    paths = {str(path).replace("\\", "/").casefold(): content
             for path, content in files.items()}
    manifest = None
    try:
        candidate = paths.get("package.json")
        manifest = json.loads(candidate) if candidate is not None else None
    except (TypeError, json.JSONDecodeError):
        manifest = None
    package_deps = {}
    if isinstance(manifest, dict):
        for key in ("dependencies", "devDependencies"):
            value = manifest.get(key)
            if isinstance(value, dict):
                package_deps.update(value)
    frameworks = ("next", "react", "vue", "svelte", "@angular/core", "astro", "vite")
    framework = next((name for name in frameworks if name in package_deps), "not identified")
    ui_suffixes = (".html", ".css", ".scss", ".sass", ".less", ".jsx", ".tsx", ".vue", ".svelte")
    ui_files = {path: content for path, content in paths.items() if path.endswith(ui_suffixes)}
    is_web = bool(ui_files or framework != "not identified")
    findings: list[str] = []
    resources: set[str] = set()
    if not is_web:
        return {"is_web": False, "framework": framework, "ui_files": [],
                "findings": [], "external_resources": []}
    if not ui_files:
        return {"is_web": True, "framework": framework, "ui_files": [],
                "findings": ["Web framework is present, but no frontend source file was captured for UI review."],
                "external_resources": []}

    css = "\n".join(content for path, content in ui_files.items()
                     if path.endswith((".css", ".scss", ".sass", ".less")))
    html = "\n".join(content for path, content in ui_files.items()
                      if path.endswith((".html", ".jsx", ".tsx", ".vue", ".svelte")))
    if not re.search(r"--[A-Za-z0-9_-]+\s*:", css):
        findings.append("No centralized CSS custom-property design tokens were detected; verify the project's design system.")
    if not re.search(r"@media\b|\b(sm|md|lg|xl):", css + "\n" + html):
        findings.append("No responsive breakpoint rule was detected in the captured UI files.")
    if re.search(r"<img\b(?![^>]*\balt\s*=)[^>]*>", html, re.IGNORECASE):
        findings.append("At least one image element has no explicit alt attribute in the captured markup.")
    if re.search(r"<div\b[^>]*\bonClick\s*=", html) and not re.search(r"<div\b[^>]*\b(role|tabIndex)\s*=", html):
        findings.append("A clickable div was detected without a keyboard/accessibility role in the captured markup.")
    for content in ui_files.values():
        resources.update(url.rstrip(".,;:") for url in _REMOTE_URL.findall(content))
    return {"is_web": True, "framework": framework, "ui_files": sorted(ui_files),
            "findings": findings[:20], "external_resources": sorted(resources)[:50]}


def quality_checks(language: str, files: Mapping[str, str],
                   verification_command: str) -> tuple[list[dict[str, str]], list[str]]:
    """Return exact commands and sources; never infer/install a missing tool."""
    normalized = language.strip().lower()
    paths = {str(path).replace("\\", "/").casefold() for path in files}
    checks: list[dict[str, str]] = []
    notes: list[str] = []
    if normalized in {"python", "py"}:
        checks.append({"label": "Python syntax compilation", "command": "python -m compileall -q .",
                       "source": "built-in Python compiler"})
        requirements = next((content for path, content in files.items()
                             if str(path).replace("\\", "/").casefold() in {"requirements.txt", "dev-requirements.txt"}), "")
        python_paths = {str(path).replace("\\", "/").casefold(): content
                        for path, content in files.items()}
        e2e_files = {path: content for path, content in python_paths.items()
                     if path.startswith(("tests/e2e/", "e2e/")) and path.endswith(".py")}
        if (e2e_files and re.search(r"(?im)^\s*(?:playwright|pytest)(?:\s|[<>=!~;\[]|$)", requirements)
                and any("playwright" in content.casefold() for content in e2e_files.values())):
            packages = {match.group(1).casefold() for match in re.finditer(
                r"(?im)^\s*([A-Za-z0-9_.-]+)", requirements)}
            if {"playwright", "pytest"}.issubset(packages):
                checks.append({"label": "Playwright UI/E2E (Chromium)",
                               "command": "python -m pytest tests/e2e -q",
                               "source": "requirements.txt declares playwright and pytest; tests/e2e contains browser checks; runs in a version-matched Chromium container with networking disabled"})
    elif normalized in {"javascript", "js", "node", "typescript", "ts"}:
        is_ts = normalized in {"typescript", "ts"} or any(p.endswith((".ts", ".tsx")) for p in paths)
        raw = next((content for path, content in files.items()
                    if str(path).replace("\\", "/").casefold() == "package.json"), None)
        manifest = None
        if raw is not None:
            try:
                manifest = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                notes.append("package.json exists but is invalid; package scripts were not selected.")
        scripts = manifest.get("scripts", {}) if isinstance(manifest, dict) else {}
        declared_packages = {}
        if isinstance(manifest, dict):
            for section in ("dependencies", "devDependencies"):
                values = manifest.get(section)
                if isinstance(values, dict):
                    declared_packages.update(values)
        if isinstance(scripts, dict):
            for name in _SCRIPT_ORDER:
                body = scripts.get(name)
                if isinstance(body, str) and body.strip() and len(body) <= 500:
                    checks.append({"label": f"package.json script: {name}",
                                   "command": f"npm run {name}", "source": f"package.json scripts.{name}: {body.strip()}"})
            playw_config = any(PurePosixPath(path).name.startswith("playwright.config.") for path in paths)
            has_playwright = any(name in declared_packages for name in ("playwright", "@playwright/test"))
            e2e_name = next((name for name in ("e2e", "test:e2e")
                             if isinstance(scripts.get(name), str) and scripts[name].strip()), None)
            if playw_config and has_playwright and e2e_name:
                checks.append({"label": "Playwright UI/E2E (Chromium)",
                               "command": f"npm run {e2e_name}",
                               "source": (f"package.json scripts.{e2e_name} and Playwright config; runs in a version-matched "
                                          "Chromium container with networking disabled")})
        has_tests = any(PurePosixPath(p).name.startswith(("test_", "spec_"))
                        or ".test." in p or ".spec." in p for p in paths)
        if not any(check["label"] == "package.json script: test" for check in checks) and has_tests and not is_ts:
            checks.append({"label": "Node built-in test runner", "command": "node --test",
                           "source": "recognized JavaScript test files; Node built-in runner"})
        if is_ts and not any(check["label"] in {"package.json script: typecheck", "package.json script: build"}
                             for check in checks):
            notes.append("TypeScript sources have no declared typecheck/build script; compilation is unverified.")
            # A generic test command cannot establish that TypeScript compiles.
            # Do not let a successful unrelated check be reported as verification.
            return [], notes
    else:
        notes.append(f"No quality profile is configured for language '{language}'.")

    if isinstance(verification_command, str) and verification_command.strip():
        checks.append({"label": "Project verification command", "command": verification_command.strip(),
                       "source": "planner-proposed command, included for explicit approval"})
    # Preserve order while preventing a duplicated command from running twice.
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for check in checks:
        key = check["command"].casefold()
        if key not in seen:
            unique.append(check)
            seen.add(key)
    if not unique:
        notes.append("No runnable verification check is available; the project cannot be reported as verified.")
    return unique, notes
