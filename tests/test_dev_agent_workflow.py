"""Consent, sandbox, and verification tests for the development agent."""
import tempfile
import unittest
import uuid
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from actions import dev_agent


class DevAgentWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._workspace_temp = tempfile.TemporaryDirectory(
            prefix=".test-tmp-", dir=Path(__file__).resolve().parents[1]
        )
        cls._previous_tempdir = tempfile.tempdir
        tempfile.tempdir = cls._workspace_temp.name

    @classmethod
    def tearDownClass(cls):
        tempfile.tempdir = cls._previous_tempdir
        cls._workspace_temp.cleanup()

    def test_project_files_are_only_applied_after_the_approval_callback(self):
        project_name = "codex-flow-" + uuid.uuid4().hex
        projects_root = Path(tempfile.gettempdir()) / ("projects-" + uuid.uuid4().hex)
        target = projects_root / project_name
        plan = {
            "project_name": project_name,
            "entry_point": "main.py",
            "files": [{"path": "main.py", "description": "entry", "imports": []}],
            "verification_command": "python -m unittest discover -v",
            "dependencies": [],
        }
        with patch.object(dev_agent, "PROJECTS_DIR", projects_root), \
             patch("core.execution.docker_sandbox_status", return_value=(True, "sandbox ready")), \
             patch.object(dev_agent, "_plan_project", return_value=plan), \
             patch.object(dev_agent, "_write_file", return_value="print('hello')"), \
             patch("core.execution.request_execution", return_value="pending") as request:
            result = dev_agent._build_project(
                "Create a hello world app", "python", project_name, 20
            )

            self.assertTrue(result.startswith("pending\nDevelopment task ID: "))
            self.assertFalse(target.exists(), "planning must not create the project directory")
            approval = request.call_args.kwargs
            self.assertIn("Files to create/update (1)", approval["detail"])
            self.assertIn("python -m unittest discover -v", approval["detail"])
            self.assertIn("Nothing has been written or executed yet", approval["detail"])
            self.assertIn("Proposed diff preview", approval["detail"])
            self.assertIn("print('hello')", approval["detail"])

            with patch.object(dev_agent, "_apply_project_files", return_value=None) as apply, \
                 patch("core.dev_project_context.save_project_context", return_value={}), \
                 patch.object(dev_agent, "_execute_project_workflow",
                              return_value="Sandbox verification passed.\nverified") as verify:
                self.assertEqual(approval["operation"](), "Sandbox verification passed.\nverified")

            apply.assert_called_once()
            verify.assert_called_once()

    def test_incomplete_generation_is_never_presented_for_approval(self):
        project_name = "codex-incomplete-" + uuid.uuid4().hex
        projects_root = Path(tempfile.gettempdir()) / ("incomplete-projects-" + uuid.uuid4().hex)
        plan = {
            "project_name": project_name, "entry_point": "main.py",
            "files": [
                {"path": "helper.py", "description": "helper", "imports": []},
                {"path": "main.py", "description": "entry", "imports": ["helper"]},
            ], "verification_command": "python -m unittest", "dependencies": [],
        }
        with patch.object(dev_agent, "PROJECTS_DIR", projects_root), \
             patch("core.execution.docker_sandbox_status", return_value=(True, "ready")), \
             patch.object(dev_agent, "_plan_project", return_value=plan), \
             patch.object(dev_agent, "_write_file", side_effect=["def helper(): pass", RuntimeError("model unavailable")]), \
             patch("core.execution.request_execution") as request:
            result = dev_agent._build_project("Build it", "python", project_name, 20)
        self.assertIn("Generation was incomplete", result)
        self.assertFalse((projects_root / project_name).exists())
        request.assert_not_called()

    def test_truncated_diff_is_never_presented_for_approval(self):
        project_name = "codex-large-diff-" + uuid.uuid4().hex
        projects_root = Path(tempfile.gettempdir()) / ("large-projects-" + uuid.uuid4().hex)
        plan = {
            "project_name": project_name, "entry_point": "main.py",
            "files": [{"path": "main.py", "description": "entry", "imports": []}],
            "verification_command": "python -m unittest", "dependencies": [],
        }
        with patch.object(dev_agent, "PROJECTS_DIR", projects_root), \
             patch("core.execution.docker_sandbox_status", return_value=(True, "ready")), \
             patch.object(dev_agent, "_plan_project", return_value=plan), \
             patch.object(dev_agent, "_write_file", return_value="line\n" * 3000), \
             patch("core.execution.request_execution") as request:
            result = dev_agent._build_project("large change", "python", project_name, 20)
        self.assertIn("full change does not fit", result)
        request.assert_not_called()

    def test_failed_build_verification_restores_existing_files(self):
        project = Path(tempfile.gettempdir()) / "build-rollback"
        project.mkdir(exist_ok=True)
        source = project / "main.py"
        source.write_bytes(b"original\xff")
        with patch.object(dev_agent, "_execute_project_workflow",
                          return_value="Sandbox verification failed.\nassert failed"):
            with self.assertRaisesRegex(RuntimeError, "Generated source was rolled back"):
                dev_agent._execute_approved_project(
                    "goal", {"main.py": "generated"}, {"main.py": b"original\xff"},
                    "python -m unittest", [],
                    project, 20, "python",
                )
        self.assertEqual(source.read_bytes(), b"original\xff")

    def test_failed_new_project_is_removed_after_verification_failure(self):
        project = Path(tempfile.gettempdir()) / ("new-project-rollback-" + uuid.uuid4().hex)
        with patch.object(dev_agent, "_execute_project_workflow",
                          return_value="Sandbox verification failed.\nassert failed"):
            with self.assertRaisesRegex(RuntimeError, "Generated source was rolled back"):
                dev_agent._execute_approved_project(
                    "goal", {"nested/main.py": "generated"}, {"nested/main.py": None},
                    "python -m unittest", [],
                    project, 20, "python",
                )
        self.assertFalse(project.exists())

    def test_build_refuses_to_overwrite_files_changed_since_approval_preview(self):
        projects_root = Path(tempfile.gettempdir()) / "toctou-root"
        projects_root.mkdir(exist_ok=True)
        project_name = "toctou-" + uuid.uuid4().hex
        target = projects_root / project_name
        target.mkdir()
        source = target / "main.py"
        source.write_text("before preview", encoding="utf-8")
        plan = {
            "project_name": project_name, "entry_point": "main.py",
            "files": [{"path": "main.py", "description": "entry", "imports": []}],
            "verification_command": "python -m unittest", "dependencies": [],
        }
        with patch.object(dev_agent, "PROJECTS_DIR", projects_root), \
             patch("core.execution.docker_sandbox_status", return_value=(True, "ready")), \
             patch.object(dev_agent, "_plan_project", return_value=plan), \
             patch.object(dev_agent, "_write_file", return_value="generated"), \
             patch("core.execution.request_execution", side_effect=lambda **kwargs: kwargs) as request:
            dev_agent._build_project("update", "python", project_name, 20)
            approval = request.call_args.kwargs

        source.write_text("user edit after preview", encoding="utf-8")
        with patch.object(dev_agent, "_execute_project_workflow") as verify:
            with self.assertRaisesRegex(RuntimeError, "changed after the approval preview"):
                approval["operation"]()
        self.assertEqual(source.read_text(encoding="utf-8"), "user edit after preview")
        verify.assert_not_called()

    def test_project_file_apply_rolls_back_partial_write(self):
        with tempfile.TemporaryDirectory(prefix="marklv-dev-agent-") as temp:
            root = Path(temp) / "project"
            root.mkdir()
            original = root / "existing.py"
            original.write_text("before", encoding="utf-8")
            real_replace = dev_agent.os.replace
            calls = 0

            def replace_once_then_fail(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated disk failure")
                return real_replace(source, destination)

            with patch.object(dev_agent.os, "replace", side_effect=replace_once_then_fail):
                error = dev_agent._apply_project_files(
                    root, {"existing.py": "after", "new.py": "new"}
                )

            self.assertIn("simulated disk failure", error)
            self.assertEqual(original.read_text(encoding="utf-8"), "before")
            self.assertFalse((root / "new.py").exists())

    def test_project_execution_uses_networkless_container_and_limits(self):
        from core import execution

        with tempfile.TemporaryDirectory(prefix="marklv-sandbox-") as temp:
            project = Path(temp)
            with patch.object(execution.shutil, "which", return_value="docker.exe"), \
                 patch.object(execution, "_run_process", return_value="Process exited with code 0.\nok") as run:
                result = execution.run_sandboxed_project_command(
                    "python main.py", project, timeout=15, language="python"
                )

        argv = run.call_args.args[0]
        self.assertEqual(result, "Process exited with code 0.\nok")
        for item in ("--network=none", "--cap-drop=ALL", "--read-only",
                     "--pids-limit=128", "--memory=1g", "--cpus=2",
                     "python:3.12-slim"):
            self.assertIn(item, argv)
        self.assertIn("main.py", argv)
        self.assertNotIn("--network=bridge", argv)

    def test_dependency_install_uses_limited_network_container(self):
        from core import execution

        with tempfile.TemporaryDirectory(prefix="marklv-deps-") as temp:
            with patch.object(execution.shutil, "which", return_value="docker.exe"), \
                 patch.object(execution, "_run_process", return_value="Process exited with code 0. ok") as run:
                result = execution.install_dependencies(
                    ["requests"], temp, timeout=45, manager="pip", language="python"
                )

        argv = run.call_args.args[0]
        self.assertIn("--network=bridge", argv)
        self.assertIn("--cap-drop=ALL", argv)
        self.assertIn("--read-only", argv)
        self.assertIn("--target=/workspace/.marklv_site", argv)
        self.assertIn("requests", argv)
        self.assertEqual(result, "Process exited with code 0. ok")

    def test_no_docker_never_falls_back_to_host_execution(self):
        from core.execution import ExecutionPolicyError, run_sandboxed_project_command

        with patch("core.execution.shutil.which", return_value=None), \
             patch("core.execution._run_process") as host_process:
            with self.assertRaisesRegex(ExecutionPolicyError, "refusing to run this project on the host"):
                run_sandboxed_project_command("python main.py", Path.home())
        host_process.assert_not_called()

    def test_workflow_does_not_retry_or_apply_unapproved_repair(self):
        project_dir = Path(tempfile.gettempdir()) / "repair-project"
        project_dir.mkdir(exist_ok=True)
        cache = Path(tempfile.gettempdir()) / "runtime-cache-python"
        package_dir = cache / ".marklv_site"
        package_dir.mkdir(parents=True, exist_ok=True)
        (package_dir / "marker.txt").write_text("ready", encoding="utf-8")
        (cache / ".marklv-runtime.json").write_text(json.dumps({
            "tree_sha256": dev_agent._dependency_tree_digest(package_dir),
        }), encoding="utf-8")
        with patch.object(dev_agent, "_ensure_runtime_dependencies", return_value=cache) as install, \
             patch.object(dev_agent, "_run_project", return_value="Process exited with code 1.\nModuleNotFoundError: unapproved_extra") as run, \
             patch.object(dev_agent, "_apply_project_files") as apply:
            result = dev_agent._execute_project_workflow(
                ["approved-package"], "python main.py", project_dir, 20, "python",
            )

        self.assertIn("No unreviewed code repair or newly discovered dependency", result)
        install.assert_called_once()
        self.assertEqual(install.call_args.args[0], ["approved-package"])
        run.assert_called_once()
        apply.assert_not_called()

    def test_persistent_dependency_environment_is_reused_per_project_and_package_set(self):
        home = Path(tempfile.gettempdir()) / ("runtime-home-" + uuid.uuid4().hex)
        home.mkdir()
        project = home / "project"
        project.mkdir()
        app_root = home / "Desktop" / "Projects"
        installed = []

        def fake_install(packages, stage, manager="pip", language="python"):
            installed.append((list(packages), manager))
            package_dir = stage / ("node_modules" if manager == "npm" else ".marklv_site")
            package_dir.mkdir()
            (package_dir / "marker.txt").write_text("ready", encoding="utf-8")
            return "Process exited with code 0. installed"

        with patch.object(dev_agent.Path, "home", return_value=home), \
             patch.object(dev_agent, "PROJECTS_DIR", app_root), \
             patch.object(dev_agent, "_install_dependencies", side_effect=fake_install):
            first = dev_agent._ensure_runtime_dependencies(["requests"], project, "python")
            second = dev_agent._ensure_runtime_dependencies(["requests"], project, "python")
            workspace = Path(tempfile.gettempdir()) / ("runtime-copy-" + uuid.uuid4().hex)
            workspace.mkdir()
            dev_agent._copy_runtime_dependencies(first, workspace, "python")

        self.assertEqual(first, second)
        self.assertEqual(len(installed), 1)
        self.assertEqual((workspace / ".marklv_site" / "marker.txt").read_text(encoding="utf-8"), "ready")

    def test_test_artifacts_are_created_only_in_disposable_workspace(self):
        root = Path(tempfile.gettempdir()) / "artifact-isolation"
        root.mkdir(exist_ok=True)
        (root / "main.py").write_text("print('ok')", encoding="utf-8")
        created = []

        def run_and_leave_test_artifact(command, workspace, **kwargs):
            artifact = workspace / "test-output.tmp"
            artifact.write_text("temporary", encoding="utf-8")
            created.append(artifact)
            return "Process exited with code 0.\nverified"

        with patch.object(dev_agent.Path, "home", return_value=root.parent), \
             patch.object(dev_agent, "_run_project", side_effect=run_and_leave_test_artifact):
            result = dev_agent._execute_project_workflow(
                [], "python -m unittest", root, 20, "python"
            )

        self.assertIn("removed automatically", result)
        self.assertTrue(created)
        self.assertFalse((root / "test-output.tmp").exists())
        self.assertTrue(all(not item.parent.exists() for item in created))

    def test_existing_project_fix_is_reviewed_before_write_and_rolls_back_on_failure(self):
        root = Path(tempfile.gettempdir()) / "approved-existing-fix"
        root.mkdir(exist_ok=True)
        source = root / "main.py"
        source.write_text("def value():\n    return 1\n", encoding="utf-8")
        proposed = "def value():\n    return 2\n"
        model_response = SimpleNamespace(text=json.dumps({"changes": [{
            "path": "main.py", "evidence": "main.py:2",
            "reason": "The return value is incorrect", "content": proposed,
        }]}))

        approvals = []
        with patch("core.execution.docker_sandbox_status", return_value=(True, "ready")), \
             patch.object(dev_agent, "_get_model", return_value=SimpleNamespace(
                 generate_content=lambda _prompt: model_response)), \
             patch("core.execution.request_execution", side_effect=lambda **kwargs: approvals.append(kwargs) or kwargs):
            source_approval = dev_agent._fix_existing_project(str(root), "Fix value result", "python", 20)

        self.assertEqual(source.read_text(encoding="utf-8"), "def value():\n    return 1\n")
        self.assertIn("Approve sending project source", source_approval["title"])
        self.assertIn("Source text is sent to Gemini", source_approval["detail"])
        with patch("core.execution.docker_sandbox_status", return_value=(True, "ready")), \
             patch.object(dev_agent, "_get_model", return_value=SimpleNamespace(
                 generate_content=lambda _prompt: model_response)), \
             patch("core.execution.request_execution", side_effect=lambda **kwargs: approvals.append(kwargs) or kwargs):
            approval = source_approval["operation"]()

        self.assertIn("Proposed diff", approval["detail"])
        self.assertIn("main.py:2", approval["detail"])
        with patch.object(dev_agent, "_execute_project_workflow",
                          return_value="Sandbox verification did not pass.\nassert failed"), \
             patch("core.dev_project_context.save_project_context", return_value={}):
            result = approval["operation"]()

        self.assertIn("rolled back", result)
        self.assertEqual(source.read_text(encoding="utf-8"), "def value():\n    return 1\n")

    def test_audit_requires_source_sharing_consent_and_filters_unmatched_citations(self):
        root = Path(tempfile.gettempdir()) / ("audit-consent-" + uuid.uuid4().hex)
        root.mkdir()
        (root / "main.py").write_text("password = input()\n", encoding="utf-8")
        audit_response = SimpleNamespace(text=json.dumps({
            "findings": [
                {"severity": "medium", "title": "Unvalidated input", "path": "main.py",
                 "line": 1, "evidence": "password = input()", "impact": "input is not validated",
                 "recommendation": "validate input", "confidence": "high"},
                {"severity": "high", "title": "Invented finding", "path": "other.py",
                 "line": 999, "evidence": "unsafe", "impact": "bad", "recommendation": "fix"},
            ], "architecture_notes": [], "test_gaps": [],
        }))
        approvals = []
        with patch.object(dev_agent, "_get_model") as model, \
             patch("core.execution.docker_sandbox_status", return_value=(False, "stopped")), \
             patch("core.execution.request_execution", side_effect=lambda **kwargs: approvals.append(kwargs) or kwargs):
            source_approval = dev_agent._audit_project(str(root), "audit inputs", "python", 20)
        model.assert_not_called()
        self.assertIn("Approve sending project source", source_approval["title"])
        self.assertNotIn("password = input()", source_approval["detail"])

        with patch.object(dev_agent, "_get_model", return_value=SimpleNamespace(
                 generate_content=lambda _prompt: audit_response)):
            result = source_approval["operation"]()
        self.assertIn("main.py:1", result)
        self.assertNotIn("Invented finding", result)
        self.assertIn("Sandbox unavailable", result)

    def test_audit_blocks_content_secrets_before_consent_without_echoing_values(self):
        root = Path(tempfile.gettempdir()) / ("audit-secret-" + uuid.uuid4().hex)
        root.mkdir()
        secret = "ghp_" + "A" * 36
        (root / "settings.py").write_text(f'api_key = "{secret}"\n', encoding="utf-8")
        with patch.object(dev_agent, "_get_model") as model, \
             patch("core.execution.request_execution") as request:
            result = dev_agent._audit_project(str(root), "audit", "python", 20)

        self.assertIn("settings.py:1", result)
        self.assertIn("GitHub token", result)
        self.assertNotIn(secret, result)
        model.assert_not_called()
        request.assert_not_called()

    def test_media_import_is_validated_previewed_and_requires_approval(self):
        from PIL import Image

        base = Path(tempfile.gettempdir()) / ("media-import-" + uuid.uuid4().hex)
        project = base / "project"
        project.mkdir(parents=True)
        source = base / "generated-art.png"
        Image.new("RGB", (8, 8), color="red").save(source)
        with patch("core.execution.request_execution", side_effect=lambda **kwargs: kwargs):
            approval = dev_agent.dev_agent({
                "mode": "import_media", "project_path": str(project),
                "media_source_path": str(source), "media_destination_path": "public/assets/art.png",
            })
        destination = project / "public" / "assets" / "art.png"
        self.assertIn("Approve importing media", approval["title"])
        self.assertIn(str(destination), approval["detail"])
        self.assertFalse(destination.exists())
        result = approval["operation"]()
        self.assertIn("Media imported and verified", result)
        self.assertEqual(destination.read_bytes(), source.read_bytes())

    def test_media_import_rejects_path_traversal_before_confirmation(self):
        base = Path(tempfile.gettempdir()) / ("media-traversal-" + uuid.uuid4().hex)
        project = base / "project"
        project.mkdir(parents=True)
        source = base / "file.woff2"
        source.write_bytes(b"wOF2" + b"x" * 20)
        with patch("core.execution.request_execution") as request:
            result = dev_agent.dev_agent({
                "mode": "import_media", "project_path": str(project),
                "media_source_path": str(source), "media_destination_path": "../outside.woff2",
            })
        self.assertIn("rejected safely", result)
        request.assert_not_called()

    def test_audit_review_includes_frontend_findings_and_manifest_quality_scripts(self):
        root = Path(tempfile.gettempdir()) / ("audit-ui-" + uuid.uuid4().hex)
        root.mkdir()
        (root / "package.json").write_text(json.dumps({
            "scripts": {"typecheck": "tsc --noEmit", "test": "vitest run"},
            "devDependencies": {"react": "^19", "typescript": "^5"},
        }), encoding="utf-8")
        (root / "app.tsx").write_text('<main><img src="hero.png" /></main>', encoding="utf-8")
        (root / "style.css").write_text("body { color: black; }", encoding="utf-8")
        audit_response = SimpleNamespace(text=json.dumps({
            "findings": [], "architecture_notes": [], "test_gaps": [],
        }))
        approvals = []
        with patch.object(dev_agent, "PROJECTS_DIR", root / "meta"), \
             patch.object(dev_agent, "_get_model", return_value=SimpleNamespace(
                 generate_content=lambda _prompt: audit_response)), \
             patch("core.execution.docker_sandbox_status", return_value=(True, "ready")), \
             patch("core.execution.request_execution", side_effect=lambda **kwargs: approvals.append(kwargs) or kwargs):
            source = dev_agent._audit_project(str(root), "Review the web UI", "typescript", 30)
            report = source["operation"]()

        test_approval = approvals[-1]
        self.assertIn("Frontend source review", report["detail"])
        self.assertIn("package.json script: typecheck", test_approval["detail"])
        self.assertIn("tsc --noEmit", test_approval["detail"])
        self.assertIn("package.json script: test", test_approval["detail"])
        self.assertIn("alt attribute", test_approval["detail"])

    def test_secret_scanner_reports_locations_and_types_only(self):
        secret = "AIza" + "B" * 35
        result = dev_agent._source_secret_block({"config.py": f'KEY="{secret}"'})
        self.assertIn("Google API key", result)
        self.assertNotIn(secret, result)

    def test_planner_inputs_fail_closed_without_side_effects(self):
        with patch("core.execution.docker_sandbox_status", return_value=(True, "ready")), \
             patch.object(dev_agent, "_plan_project", return_value={
                 "entry_point": "../outside.py", "files": [{"path": "../outside.py"}],
                 "run_command": "python ../outside.py", "dependencies": [],
             }), \
             patch("core.execution.request_execution") as request:
            result = dev_agent._build_project("test", "python", "safe", 20)

        self.assertIn("unsafe command or entry point", result)
        request.assert_not_called()

    def test_unavailable_docker_stops_before_planning_or_writing(self):
        with patch("core.execution.docker_sandbox_status", return_value=(False, "daemon stopped")), \
             patch.object(dev_agent, "_plan_project") as planner, \
             patch("core.execution.request_execution") as request:
            result = dev_agent._build_project("test", "python", "no-docker", 20)

        self.assertIn("not generated or changed project files", result.lower())
        planner.assert_not_called()
        request.assert_not_called()

    def test_research_sends_only_bounded_public_technical_queries(self):
        with patch("actions.web_search.web_search", return_value="Official docs: https://example.test/docs") as search:
            result = dev_agent._research_plan_queries([
                "React Router official migration guide",
                r"C:\Users\1\private project",
                "contact name@example.com",
                "token=AIza" + "B" * 35,
                "another official query",
            ])
        self.assertEqual(search.call_count, 2)
        self.assertEqual(search.call_args_list[0].args[0]["mode"], "research")
        self.assertIn("https://example.test/docs", result)
        self.assertNotIn("private project", result)
        self.assertNotIn("name@example.com", result)

    def test_vscode_open_uses_only_the_resolved_project_folder_without_shell(self):
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="vscode-target-", dir=workspace) as temp:
            project = Path(temp)
            with patch.object(dev_agent.shutil, "which", return_value="/tools/code"), \
                 patch.object(dev_agent.subprocess, "Popen") as launch:
                self.assertTrue(dev_agent._open_project_in_vscode(project))
        argv = launch.call_args.args[0]
        self.assertEqual(argv[-1], str(project))
        self.assertIn("--reuse-window", argv)
        self.assertFalse(launch.call_args.kwargs["shell"])

    def test_playwright_uses_resolved_version_and_matching_isolated_image(self):
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="playwright-cache-", dir=workspace) as temp:
            project = Path(temp)
            package = project / "node_modules" / "@playwright" / "test"
            package.mkdir(parents=True)
            (package / "package.json").write_text(json.dumps({"version": "1.63.0"}), encoding="utf-8")
            with patch("core.execution.run_playwright_sandboxed_project_command", return_value="Process exited with code 0") as run:
                result = dev_agent._run_playwright_project("npm run e2e", project, 30)
        self.assertIn("code 0", result)
        self.assertEqual(run.call_args.args[2], "1.63.0")

    def test_playwright_docker_runner_is_pinned_and_networkless(self):
        from core import execution
        with patch.object(execution, "_run_in_docker", return_value="Process exited with code 0") as run:
            result = execution.run_playwright_sandboxed_project_command(
                "npm run e2e", Path.home(), "1.63.0", timeout=30,
            )
        self.assertIn("code 0", result)
        self.assertEqual(run.call_args.kwargs["image_override"], "mcr.microsoft.com/playwright:v1.63.0-noble")
        self.assertFalse(run.call_args.kwargs["network"])

    def test_python_playwright_runner_uses_matching_official_image(self):
        from core import execution
        with patch.object(execution, "_run_in_docker", return_value="Process exited with code 0") as run:
            result = execution.run_playwright_python_sandboxed_project_command(
                "python -m pytest tests/e2e -q", Path.home(), "1.63.0", timeout=30,
            )
        self.assertIn("code 0", result)
        self.assertEqual(run.call_args.kwargs["image_override"],
                         "mcr.microsoft.com/playwright/python:v1.63.0-noble")
        self.assertTrue(run.call_args.kwargs["pythonpath"])
        self.assertFalse(run.call_args.kwargs["network"])

    def test_python_playwright_resolves_version_from_approved_dependency_cache(self):
        workspace = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="python-playwright-cache-", dir=workspace) as temp:
            project = Path(temp)
            package = project / ".marklv_site" / "playwright-1.63.0.dist-info"
            package.mkdir(parents=True)
            (package / "METADATA").write_text("Name: playwright\nVersion: 1.63.0\n", encoding="utf-8")
            with patch("core.execution.run_playwright_python_sandboxed_project_command",
                       return_value="Process exited with code 0") as run:
                result = dev_agent._run_playwright_project(
                    "python -m pytest tests/e2e -q", project, 30, language="python",
                )
        self.assertIn("code 0", result)
        self.assertEqual(run.call_args.args[2], "1.63.0")


if __name__ == "__main__":
    unittest.main()
