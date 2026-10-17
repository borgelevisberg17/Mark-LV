import json
import unittest

from core.dev_quality import inspect_frontend, quality_checks


class QualityProfileTests(unittest.TestCase):
    def test_python_adds_static_compilation_and_declared_command(self):
        checks, notes = quality_checks("python", {"main.py": "print('ok')"}, "python -m unittest")
        self.assertEqual([c["command"] for c in checks],
                         ["python -m compileall -q .", "python -m unittest"])
        self.assertEqual(notes, [])

    def test_node_scripts_are_ordered_and_show_exact_script_bodies(self):
        manifest = json.dumps({"scripts": {"build": "vite build", "test": "vitest run",
                                            "lint": "eslint src"}})
        checks, _ = quality_checks("javascript", {"package.json": manifest}, "npm test")
        self.assertEqual([c["command"] for c in checks],
                         ["npm run lint", "npm run test", "npm run build", "npm test"])
        self.assertEqual(checks[0]["source"], "package.json scripts.lint: eslint src")

    def test_playwright_e2e_requires_config_dependency_and_named_script(self):
        manifest = json.dumps({
            "devDependencies": {"@playwright/test": "^1.63.0"},
            "scripts": {"e2e": "playwright test", "build": "vite build"},
        })
        checks, _ = quality_checks("typescript", {
            "package.json": manifest, "playwright.config.ts": "export default {};",
            "src/app.ts": "export {};",
        }, "npm run build")
        browser = [item for item in checks if item["label"] == "Playwright UI/E2E (Chromium)"]
        self.assertEqual(len(browser), 1)
        self.assertEqual(browser[0]["command"], "npm run e2e")
        self.assertIn("networking disabled", browser[0]["source"])

    def test_playwright_is_not_claimed_without_explicit_project_setup(self):
        checks, _ = quality_checks("javascript", {
            "package.json": json.dumps({"devDependencies": {"playwright": "^1.63.0"},
                                         "scripts": {"test": "node --test"}}),
        }, "")
        self.assertFalse(any("Playwright" in item["label"] for item in checks))

    def test_python_playwright_requires_declared_dependencies_and_browser_test(self):
        checks, _ = quality_checks("python", {
            "requirements.txt": "playwright==1.63.0\npytest>=8\n",
            "tests/e2e/test_home.py": "from playwright.sync_api import sync_playwright\ndef test_home(): pass\n",
        }, "python -m unittest discover")
        browser = [item for item in checks if item["label"] == "Playwright UI/E2E (Chromium)"]
        self.assertEqual(len(browser), 1)
        self.assertEqual(browser[0]["command"], "python -m pytest tests/e2e -q")

    def test_typescript_without_declared_typecheck_or_build_is_not_claimed_compiled(self):
        checks, notes = quality_checks("typescript", {"src/app.ts": "export {}"}, "node --test")
        self.assertEqual(checks, [])
        self.assertTrue(any("TypeScript" in note and "unverified" in note for note in notes))

    def test_invalid_manifest_is_not_used_as_a_script_source(self):
        checks, notes = quality_checks("javascript", {"package.json": "{"}, "node --test")
        self.assertEqual([c["command"] for c in checks], ["node --test"])
        self.assertTrue(any("invalid" in note for note in notes))

    def test_no_invented_command_when_planner_provides_none(self):
        checks, notes = quality_checks("rust", {"main.rs": "fn main() {}"}, "")
        self.assertEqual(checks, [])
        self.assertTrue(any("No runnable" in note for note in notes))

    def test_frontend_review_detects_framework_tokens_responsiveness_and_remote_fonts(self):
        review = inspect_frontend({
            "package.json": json.dumps({"dependencies": {"react": "^19", "vite": "^6"}}),
            "src/app.jsx": '<main><img src="hero.png" /></main>',
            "src/style.css": ':root { --brand: #123; } @media (max-width: 600px) {} @import url(https://fonts.example/font.css);',
        })
        self.assertTrue(review["is_web"])
        self.assertEqual(review["framework"], "react")
        self.assertFalse(any("design tokens" in item for item in review["findings"]))
        self.assertFalse(any("breakpoint" in item for item in review["findings"]))
        self.assertTrue(any("alt" in item for item in review["findings"]))
        self.assertEqual(review["external_resources"], ["https://fonts.example/font.css"])

    def test_frontend_without_tokens_or_responsive_rules_gets_caveats_not_fake_pass(self):
        review = inspect_frontend({"index.html": '<img src="banner.png"><div onClick={go}>Go</div>'})
        self.assertTrue(review["is_web"])
        self.assertTrue(any("design tokens" in item for item in review["findings"]))
        self.assertTrue(any("responsive" in item for item in review["findings"]))
        self.assertTrue(any("alt" in item for item in review["findings"]))
        self.assertTrue(any("clickable div" in item for item in review["findings"]))


if __name__ == "__main__":
    unittest.main()
