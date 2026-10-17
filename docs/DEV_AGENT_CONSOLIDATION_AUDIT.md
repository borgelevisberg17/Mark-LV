# Development Agent Consolidation Audit

Date: 2026-10-17  
Scope: `actions/dev_agent.py`, `core/dev_project_context.py`, `core/dev_task_store.py`, `core/execution.py`, `core/confirm.py`, the development-agent prompt, and the focused test suite. This is a source-level review plus the test results recorded below; it is not a security certification or a live Docker/image supply-chain audit.

## Current controls

- Build, audit, and fix are distinct modes. Audit/fix require an exact project path; build targets a named folder below the configured projects root.
- Existing-source audit and fix show an explicit approval before sending a bounded source snapshot to the configured Gemini model. Audit findings are accepted only when path, line, and quoted source text match the captured snapshot.
- Audit and fix scan source contents locally for common credential formats before asking to share source. A match stops the upload and reports only the file, line, and credential class; matched values are never included in the result.
- Fix previews a bounded diff, checks the cited source against its captured baseline, asks before writing, verifies in Docker, and restores original bytes if verification fails.
- Build does not ask for approval if any planned file failed generation. It captures original bytes before review, rejects a stale preview at execution time, installs only the listed packages, and restores prior files if verification fails.
- Project verification uses a disposable copy that excludes known secret-named files, symlinks, VCS metadata, caches, and installed dependency directories. The copy is bounded and automatically removed.
- Build, audit and fix now share deterministic quality selection: Python syntax compilation plus the explicit test command, or recognized `package.json` scripts (`typecheck`, `lint`, `test`, `build`) in a fixed order plus the explicit test command. Each command and script body is shown in the execution approval; checks stop at the first failure. TypeScript without a declared typecheck/build script is rejected as unverifiable rather than counted as passing.
- Web plans now include application type, framework rationale, design-system guidance, responsive/accessibility acceptance, and declared external resources. A bounded source heuristic reports detected frameworks, missing CSS tokens/breakpoints, image alt omissions, clickable divs, and remote URLs in the approval/audit. This is source review only; it is not screenshot or browser-based visual QA.
- `import_media` supports user-selected local raster images and font files. It validates type/signature/size, binds exact project-relative destination and source/destination hashes to a separate approval, then copies atomically. `visual_studio` can generate/edit image assets; the agent's media import mode can place a saved image in a project. SVG is excluded as active content.
- Approved dependency sets are installed into an app-managed cache keyed by canonical project identity, language, and exact requested package names. The cache is published only after the install completes, content-hashed before reuse/copy, and tests still run against disposable source copies. The exact cache path is shown in the approval.
- Project memory is keyed by normalized canonical path, bounded to recent outcomes, and excludes source text and raw process output.
- Builds now have durable task IDs, per-file hash-verified source checkpoints, a to-do list, phase/message timestamps, and `task_list`, `task_status`, `task_control`, and `resume` modes. Pause/cancel requests are applied between generation calls. Recoverable preparation tasks resume only after explicit user action.
- Generated source checkpoint artifacts stay in the local task store only while a task is resumable; terminal completion/cancellation/failure deletes those files. The task record retains bounded metadata and checklist outcomes.
- Build task status is linked to the operation approval journal. If approval was cancelled/expired, checkpoints are cleaned. If restart happens after an approved side effect may have started, task state becomes `unknown_after_restart` and resume is blocked pending reconciliation.
- Runtime project commands use Docker with networking disabled, constrained CPU, memory and process count, a read-only container root, and no host fallback.

## Findings to consolidate

### P0 — task persistence is in place; long-running execution still needs a worker service

Build tasks now persist per-file generation checkpoints and a to-do list, support pause/cancel at safe boundaries, and can explicitly resume interrupted preparation. Approval cancellation/expiry is reconciled, and a restart in an approved side-effect phase is marked uncertain and cannot be replayed. However, generation remains synchronous inside the tool call, so users may not be able to issue control actions while a call is waiting. Pause cannot interrupt an active model call; cancel cannot interrupt a Docker command. The confirmation worker is still a daemon thread. Project commands have a hard 300-second cap, approval expires after 90 seconds, and build code generation can issue up to 100 serial model calls. This still falls short of dependable hour-long work.

**Needed:** a persistent worker/queue that returns control immediately while work runs; bounded heartbeats and configurable per-task budgets; cancellation propagated to active model/Docker processes; explicit worker reattachment; parent-child approval IDs; recovery UI for uncertain outcomes. Task state now includes a bounded 200-entry event timeline, but that does not make the synchronous worker durable. Do not resume test/install/write phases until reconciliation proves the effect.

### P0 — large change review artifact is not scalable

Build and fix now refuse approval when the complete diff exceeds the review panel limit, so a truncated diff is not approved. This closes the unsafe path but blocks larger application changes until the UI supports a full diff artifact.

**Needed:** a review artifact that can display every changed line and remain bound to the approved fingerprint, so large work can be safely reviewed without manual splitting. Keep the file manifest and rollback baseline linked to that artifact.

### P1 — dependency cache is persistent but not yet reproducible or production-ready

Approved packages now persist in a separate app-managed cache, versioned by the exact requested package list and project identity. Failed installs never publish a cache, and test artifacts still stay disposable. However, unpinned package names can resolve to different versions on the first install at different times; there is no lockfile/integrity verification, cleanup UI, or direct runtime launcher that configures applications to consume this cache. Audit/fix still do not provision dependencies, and their verification commands remain generic.

**Needed:** resolve and show exact versions/integrity before approval, detect/validate package manifests and lockfiles, provide cache list/cleanup and runtime launch controls, select project-defined lint/type/unit/integration/e2e commands, and distinguish “test unavailable due to dependencies” from a code defect.

### P1 — dependency supply-chain controls

Packages are name-validated, but versions and lockfiles are not required. Dependency installation uses network-enabled Docker and package lifecycle/build scripts run inside that container. Approval lists package names, but there is no registry allowlist, integrity pin, SBOM, vulnerability/license scan, or policy for script execution.

**Needed:** deterministic lockfiles and integrity hashes, registry restrictions, safe script policy, SBOM and vulnerability/license review, and a separate approval when a lockfile or dependency set changes.

### P1 — source secret screening needs broader coverage

Audit/fix now scan for common private-key, cloud/API key, GitHub token, bearer authorization, and secret-assignment patterns before source sharing. This is a conservative blocklist, not a complete secret detector: encoded, split, custom, or otherwise unfamiliar credentials may still pass. User consent remains required.

**Needed:** a maintained secret-scanning engine with configurable policies/exclusions, safe redaction with a reviewable preview, and an option to keep all source analysis local when an offline model is available.

### P1 — project continuity and workspace binding

The path identity prevents similar project names from sharing memory. However, build does not accept an exact existing `project_path`; it relies on `project_name` under one configured projects root. Project goals are overwritten as “current goal,” `decisions` is unused, and conversation `session_memory` is not integrated. There is no explicit active-project selector or durable task-to-project binding.

**Needed:** user-visible project selection and active-project identity; explicit change-project transition; structured goals, constraints, decisions, acceptance criteria and task links; bounded durable checkpoints; verify the selected project in every tool invocation before continuing.

### P1 — visual validation and full quality gates remain incomplete

The agent now records the planner's framework/design intent, maps acceptance criteria to evidence, and can run explicit Playwright E2E suites for JavaScript/TypeScript and Python web projects. Chromium runs in a Playwright-version-matched Microsoft container with networking disabled and only a disposable project copy mounted. The planner asks generated suites to save desktop and narrow viewport captures under `test-results` and add axe-core when compatible and explicitly approved. After the suite runs, the verifier accepts at most two bounded PNGs from that disposable directory, checks their PNG signature and size, and previews them in the Mark panel from memory before cleanup. It does not fabricate screenshots if the suite creates none. Automated axe reports and visual regression comparison are not yet collected or interpreted by Mark, so accessibility assertions remain project-suite output and visual comparisons require human review. Formatting, deeper static analysis, dependency/security review, and performance checks still depend on declared project scripts or a later quality profile. Generated or imported media is checked for file validity, not visual fit, content licensing, or brand suitability.

**Needed:** automated capture generation and enforcement at responsive viewports, axe report collection and interpretation, visual regression comparison, reproducible dependency/lockfile resolution, explicit visual/design review artifacts, and human review of generated media and license provenance. Unit tests exercise screenshot validation and preview routing contracts but do not pull and execute the Chromium images against a live application in this repository.

### P2 — progress and interaction detail is partly consolidated

Build tasks now expose live phase/check progress in the Mark content panel, persist up to 200 ordered task events, and show the latest events through `task_status`. VS Code can open the exact project after approved changes verify successfully. Audit/fix still use their separate approval chains, and the event history does not yet link source-sharing, edit, test, and operation approvals. Task status and controls still require another tool call.

**Needed:** one user-visible timeline with the next required decision, elapsed time, changed files, test state, and linked approvals for every development mode. Add first-class task controls in the UI rather than relying only on tool calls.

## Verification

- The current focused tests cover project isolation, source-sharing approval, grounded audit citations, stale-preview rejection, incomplete generation, truncated-diff refusal, rollback, disposable test cleanup, sandbox configuration, and confirmation boundaries.
- Focused task and development-agent suites cover checkpoint integrity and timeline retention, project binding, pause/resume/cancel, uncertain restart recovery, task-list/status/resume, research privacy, VS Code handoff, and approved build flow.
- Latest full repository suite: 204 tests passed. Modified Python modules compile successfully. Tests validate Chromium runner selection and isolation contracts; they do not perform live Gemini calls, download Docker browser images, execute an actual Playwright suite, or render screenshots in this repository.

