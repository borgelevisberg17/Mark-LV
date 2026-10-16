# Action Execution Security Inventory

Date: 2026-10-16  
Scope: Python action modules and adjacent process/network/file-operation surfaces reviewed in this pass.

## Policy

1. **No shell interpolation of untrusted values.** Use argument arrays with `shell=False`; validate inputs against explicit allowlists and bounds. Do not construct command strings from model/user text.
2. **No in-process execution of AI-generated Python.** `desktop_control` task mode is disabled until an OS-enforced isolated runner exists. A reduced `__builtins__` dictionary is not a sandbox.
3. **No direct execution of arbitrary files from `file_processor`.** The `run` action is disabled. A timeout alone is not isolation.
4. **Process execution must be explicit and narrow.** Prefer fixed executable paths/known commands, bounded arguments, timeouts, and checked return codes. Do not run package installation or generated project commands without explicit authorization and an isolated environment.
5. **Writes, deletion, network, system settings, scheduled tasks, and application launches are side effects.** They need validation, least privilege, logging, and confirmation for destructive/irreversible actions.
6. **Network input is untrusted.** Validate URL schemes/hosts where appropriate, apply timeouts and response-size limits, and never execute downloaded content automatically.
7. **Archive extraction and generated output paths must remain within an authorized destination.** Prevent path traversal, symlink escapes, accidental overwrites, and unbounded disk use.

## Changes made in this branch

- `actions/open_app.py`: removed `shell=True` and the constructed `start {app_name}` shell command. Only exact keys from `_APP_ALIASES` are accepted; unknown names are rejected. Windows URI activation uses the OS API; executable launches use argument arrays.
- `actions/computer_settings.py`: replaced shell-based `xrandr` command substitution with direct `xrandr` calls and parsed output values.
- `actions/dev_agent.py`: VS Code launch no longer invokes `shell=True`; it looks for a concrete executable and launches with an argument array.
- `actions/desktop.py`: AI-generated Python is no longer passed to `exec`; arbitrary desktop task mode fails closed until a separate, OS-enforced runner is provided.
- `actions/file_processor.py`: the `code / run` operation now refuses direct execution. Code can still be reviewed/explained; execution requires an isolated runner.

- `core/path_policy.py`: new shared canonical path policy; resolves symlinks before containment checks, rejects absolute child paths and POSIX/Windows traversal forms, and constrains user file processing to the home and temporary directories.
- `actions/file_controller.py`: routes path checks through the shared policy; nested names reject traversal; rename accepts only a single safe name; move/copy into a destination directory resolves the final child path again.
- `actions/file_processor.py`: input paths and generated sibling outputs are checked against allowed roots. Archive extraction no longer calls `shutil.unpack_archive`; ZIP/TAR extraction pre-validates entries, rejects traversal, links and special files, refuses overwrites, and caps extraction at 10,000 entries / 2 GiB.
- `actions/dev_agent.py`: project file paths are constrained beneath the project root. Automatic dependency installation, auto-install recovery and generated project execution now fail closed. The agent still generates source files and may open VS Code, but does not install packages or run generated code without an OS-enforced isolated runner.
- Added `tests/test_path_policy.py` for traversal and symlink escapes, plus regression checks for disabled unisolated project execution and shared file policy use.

## Inventory: file writes, moves, copies, and deletion

| Module | Operations found | Security/side-effect notes |
|---|---|---|
| `actions/file_controller.py` | Create/write files and directories; move/copy files and folders; delete files/directories; undo moves/writes | High impact. Validate resolved paths against approved roots, avoid symlink escapes, confirm destructive operations, and preserve undo data safely. |
| `actions/desktop.py` | Organize/clean desktop by moving files; create archive folders; download wallpaper to a temporary file and delete it afterward; set wallpaper | Moves affect user files. URL download needs scheme/host and size limits; temporary files should use `NamedTemporaryFile`/secure temp APIs rather than predictable names. |
| `actions/file_processor.py` | Writes transformed images, documents, CSV/Excel/JSON, transcripts and generated code; creates outputs; extracts archives to a chosen destination; removes temporary audio | Output may overwrite existing files depending on destination collision. Archive destination/path traversal and resource exhaustion require validation. Direct Python execution is disabled in this branch. |
| `actions/code_helper.py` | Creates/writes generated files; runs subprocesses; creates and deletes screenshots/temp artifacts | Generated code and project paths need containment and authorization. |
| `actions/dev_agent.py` | Writes generated/fixed source files; creates project directories; installs packages with pip; launches/runs project commands | Package installation and project execution are high-risk code execution. Shell-based VS Code launch was removed, but the agent's install/run capability still needs an explicit policy and isolated execution. |
| `actions/background_monitor.py` | Creates/writes persistent monitoring state | Ensure state location is fixed and file permissions are restrictive. |
| `actions/reminder.py` | Writes temporary scripts/XML/launch-agent files; creates scheduled tasks/agents; removes temporary files and generated launch-agent files | Can persist code and schedule future execution. Treat as high impact; validate schedule values and confirm removal/creation where appropriate. |
| `actions/screen_processor.py` | Writes configuration JSON | Validate config fields and avoid overwriting unrelated configuration. |
| `actions/flight_finder.py`, `actions/youtube_video.py` | Create/write generated text files; open resulting files in an editor | Restrict output locations and sanitize filenames/content-derived paths. |
| `actions/game_updater.py` | Creates/removes scheduled tasks, LaunchAgent plist and cron entries; launches update tools; may download/read remote metadata | Persistent execution and system changes require explicit user authorization. Validate schedule values and remote metadata before use. |
| `dashboard/server.py` | Writes temporary scripts/files, TLS certificate/key files and uploaded/downloaded content; removes temporary or rejected files | Adjacent to Actions, but still part of the overall execution boundary. Review upload path containment and permissions. |

## Inventory: process execution

- `actions/open_app.py`: application launchers. Shell invocation removed in this branch; exact application aliases only.
- `actions/computer_settings.py`: OS commands for brightness, audio, window management, Wi-Fi, reboot/shutdown and system utilities. The two shell-based `xrandr` fallbacks were replaced with argument-array calls. System-changing commands should remain allowlisted and irreversible operations should use the confirmation gate.
- `actions/dev_agent.py`: `pip install` and project run/test commands. The VS Code launcher no longer uses `shell=True`; arbitrary project commands and dependency installation remain a separate high-risk execution surface.
- `actions/file_processor.py`: FFmpeg/FFprobe process calls for media processing; arguments are passed as arrays. Direct execution of Python files is disabled. Validate timestamps, formats, quality bounds, file size, duration and subprocess return codes.
- `actions/browser_control.py`: browser/process launch and automation helpers.
- `actions/game_updater.py`: launchers, process detection, task scheduler/LaunchAgent/cron management and shutdown calls.
- `actions/reminder.py`: OS-specific commands and scheduled execution.
- `actions/computer_control.py`, `actions/code_helper.py`, `actions/send_message.py`, `actions/youtube_video.py`: process launches for computer interaction, code workflows, messaging and URL/file opening.
- `dashboard/server.py`: privileged/system utility calls. One Windows firewall path still executes a generated `.bat` via `subprocess.run(..., shell=True)`; its path is created with a temporary-file API rather than directly from user text, but the shell use should be replaced with a safer native/explicit execution path in a dedicated follow-up.

## Inventory: network access

- `actions/game_updater.py`: HTTP requests for remote update/metadata checks.
- `actions/youtube_video.py`: HTTP requests for search/video metadata.
- `actions/web_search.py`, `actions/flight_finder.py`, `actions/weather_report.py`: external search/API requests.
- `actions/browser_control.py`: browser navigation and remote content.
- `actions/desktop.py`: URL-based wallpaper download.
- `actions/file_processor.py`: sends user-provided file content to the configured Gemini service for supported analysis/transcription workflows.
- `dashboard/server.py`: downloads a browser-side crypto library and serves/uploads/downloads dashboard content.

Network operations need explicit timeouts, response-size limits, URL scheme validation, safe handling of redirects, and no automatic execution of downloaded files. User documents sent to external AI services should be treated as a data-sharing action.

## Remaining high-priority work

1. **Partially implemented:** `file_controller` and `file_processor` now share canonical path containment and archive extraction checks. Continue testing path races, output overwrite behavior in non-archive converters, and operations on network-mounted filesystems.
2. Add explicit capabilities to each action (read/write/delete/network/process/system settings) and enforce them in a central dispatcher.
3. Define confirmation requirements for destructive operations, scheduled-task changes, shutdown/reboot, network toggles, and external data sharing.
4. **Execution remains disabled:** implement a disposable `dev_agent` worker with OS-enforced filesystem/network/resource limits before restoring dependency installation or project execution.
5. Replace the remaining dashboard batch-file shell invocation and audit every `subprocess` call for timeouts, return-code handling, input bounds, and least privilege.
6. Add tests for path traversal, destructive-operation confirmation, URL validation, timeouts, and denied arbitrary code execution.

This is a source-level inventory of the listed modules, not a claim that every runtime path has been dynamically exercised.
