"""Static regression tests for high-risk action execution paths."""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read(path):
    return (ROOT / path).read_text(encoding="utf-8")


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"Function not found: {name}")


class ActionExecutionSecurityTests(unittest.TestCase):
    def test_critical_actions_do_not_enable_shell_true(self):
        for relative in (
            "actions/open_app.py",
            "actions/computer_settings.py",
            "actions/dev_agent.py",
        ):
            with self.subTest(path=relative):
                tree = ast.parse(_read(relative), filename=relative)
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    for keyword in node.keywords:
                        self.assertFalse(
                            keyword.arg == "shell"
                            and isinstance(keyword.value, ast.Constant)
                            and keyword.value.value is True,
                            f"{relative} contains shell=True",
                        )

    def test_desktop_generated_code_is_not_executed(self):
        tree = ast.parse(_read("actions/desktop.py"), filename="actions/desktop.py")
        node = _function(tree, "_execute_generated_code")
        forbidden = {"exec", "eval", "compile"}
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = child.func.id if isinstance(child.func, ast.Name) else ""
                self.assertNotIn(name, forbidden)

    def test_file_processor_run_branch_does_not_launch_python(self):
        tree = ast.parse(_read("actions/file_processor.py"), filename="actions/file_processor.py")
        node = _function(tree, "_process_code")
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute):
                self.assertFalse(
                    child.func.attr in {"Popen", "run", "call", "check_call", "check_output"}
                    and isinstance(child.func.value, ast.Name)
                    and child.func.value.id == "subprocess",
                    "Code-processing function should not invoke subprocess directly",
                )



    def test_dev_agent_does_not_run_unisolated_project_commands(self):
        tree = ast.parse(_read("actions/dev_agent.py"), filename="actions/dev_agent.py")
        for function_name in ("_install_dependencies", "_run_project", "_try_auto_install"):
            with self.subTest(function=function_name):
                node = _function(tree, function_name)
                for child in ast.walk(node):
                    if not isinstance(child, ast.Call):
                        continue
                    if isinstance(child.func, ast.Attribute):
                        self.assertFalse(
                            isinstance(child.func.value, ast.Name)
                            and child.func.value.id == "subprocess"
                            and child.func.attr in {"run", "Popen", "call", "check_call", "check_output"},
                            f"{function_name} must not spawn unisolated processes",
                        )

    def test_file_actions_use_shared_path_policy(self):
        controller = _read("actions/file_controller.py")
        processor = _read("actions/file_processor.py")
        self.assertIn("from core.path_policy import", controller)
        self.assertIn("resolve_child_path", controller)
        self.assertIn("from core.path_policy import", processor)
        self.assertIn("_safe_extract_archive", processor)
        self.assertNotIn("shutil.unpack_archive(path, dest)", processor)

if __name__ == "__main__":
    unittest.main()
