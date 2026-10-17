import unittest
from types import SimpleNamespace
from unittest.mock import patch

from actions import web_search


class GroundedSearchEvidenceTests(unittest.TestCase):
    def test_gemini_grounding_links_are_preserved_with_summary(self):
        response = SimpleNamespace(candidates=[SimpleNamespace(
            content=SimpleNamespace(parts=[SimpleNamespace(text="Use the documented API.")]),
            grounding_metadata=SimpleNamespace(grounding_chunks=[
                SimpleNamespace(web=SimpleNamespace(
                    uri="https://framework.example/docs/api", title="Official API docs")),
                SimpleNamespace(web=SimpleNamespace(
                    uri="http://insecure.example/page", title="Insecure")),
            ]),
        )])
        with patch.object(web_search, "_gemini_available", return_value=True), \
             patch("core.gemini.call", return_value=response):
            result = web_search._gemini_search("framework official docs")
        self.assertIn("Use the documented API.", result)
        self.assertIn("Official API docs: https://framework.example/docs/api", result)
        self.assertNotIn("http://insecure.example/page", result)


if __name__ == "__main__":
    unittest.main()
