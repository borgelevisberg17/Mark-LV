"""Unit tests for canonical path containment and traversal rejection."""
import tempfile
import unittest
from pathlib import Path

from core.path_policy import (
    PathPolicyError,
    is_within_roots,
    resolve_child_path,
    resolve_user_path,
)


class PathPolicyTests(unittest.TestCase):
    def test_child_path_stays_inside_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            root.mkdir()
            target = resolve_child_path(root, "src/main.py", roots=[root])
            self.assertEqual(target, root.resolve() / "src" / "main.py")
            self.assertTrue(target.is_relative_to(root.resolve()))

    def test_rejects_posix_and_windows_traversal(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            root.mkdir()
            for candidate in ("../escape.txt", r"..\escape.txt", "/etc/passwd", r"C:\Windows\system.ini"):
                with self.subTest(candidate=candidate):
                    with self.assertRaises(PathPolicyError):
                        resolve_child_path(root, candidate, roots=[root])

    def test_missing_path_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "allowed"
            root.mkdir()
            with self.assertRaisesRegex(PathPolicyError, "Path does not exist"):
                resolve_user_path("missing.docx", base=root, roots=[root], must_exist=True)

    def test_rejects_absolute_path_outside_allowed_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "allowed"
            outside = Path(temp) / "outside.txt"
            root.mkdir()
            outside.write_text("private", encoding="utf-8")
            with self.assertRaises(PathPolicyError):
                resolve_user_path(outside, roots=[root])

    def test_rejects_symlink_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "workspace"
            outside = Path(temp) / "outside"
            root.mkdir()
            outside.mkdir()
            (outside / "secret.txt").write_text("private", encoding="utf-8")
            link = root / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Symlink creation is unavailable on this platform")
            self.assertFalse(is_within_roots(link / "secret.txt", [root]))
            with self.assertRaises(PathPolicyError):
                resolve_child_path(root, "linked/secret.txt", roots=[root])


if __name__ == "__main__":
    unittest.main()
