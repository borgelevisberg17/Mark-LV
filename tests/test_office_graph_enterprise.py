"""Offline regression tests for expanded Microsoft Graph operations."""
import unittest
from unittest.mock import patch

from actions import office_graph


class Microsoft365OperationTests(unittest.TestCase):
    def setUp(self):
        self.token_patch = patch.object(office_graph, "_access_token", return_value=("test-token", ""))
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.graph_patch = patch.object(office_graph, "_graph")
        self.graph = self.graph_patch.start()
        self.addCleanup(self.graph_patch.stop)
        self.confirm_patch = patch.object(
            office_graph, "request_confirmation",
            side_effect=lambda key, title, message, callback, **kwargs: callback(),
        )
        self.confirm = self.confirm_patch.start()
        self.addCleanup(self.confirm_patch.stop)

    def test_calendar_update_requires_id_and_patches_selected_fields(self):
        missing = office_graph.office_graph({"operation": "calendar_update", "subject": "New"})
        self.assertIn("requires event_id", missing)
        self.graph.return_value = {"id": "event-1", "subject": "New"}
        result = office_graph.office_graph({
            "operation": "calendar_update", "event_id": "event-1",
            "subject": "New", "start": "2026-10-20T10:00:00",
            "end": "2026-10-20T11:00:00", "time_zone": "Africa/Luanda",
        })
        self.assertIn("Updated Outlook event", result)
        args = self.graph.call_args
        self.assertEqual("PATCH", args.args[0])
        self.assertEqual("/me/events/event-1", args.args[1])
        self.assertEqual("New", args.kwargs["json_body"]["subject"])

    def test_calendar_delete_is_confirmed_before_graph_delete(self):
        self.graph.return_value = {}
        result = office_graph.office_graph({"operation": "calendar_delete", "event_id": "evt"})
        self.assertIn("deleted", result.lower())
        self.confirm.assert_called_once()
        self.assertEqual("DELETE", self.graph.call_args.args[0])

    def test_mail_search_uses_consistency_level_and_escapes_query(self):
        self.graph.return_value = {"value": [{"id": "m1", "subject": "Invoice", "from": {"emailAddress": {"address": "a@example.com"}}, "receivedDateTime": "2026-10-01"}]}
        result = office_graph.office_graph({"operation": "mail_search", "query": 'subject:"Invoice"', "limit": 5})
        self.assertIn("Invoice", result)
        self.assertEqual("eventual", self.graph.call_args.kwargs["headers"]["ConsistencyLevel"])
        self.assertEqual("GET", self.graph.call_args.args[0])

    def test_drive_rename_requires_confirmation_and_patches_name(self):
        self.graph.return_value = {"id": "file1", "name": "new.docx"}
        result = office_graph.office_graph({"operation": "drive_rename", "item_id": "file1", "name": "new.docx"})
        self.assertIn("Updated OneDrive item", result)
        self.confirm.assert_called_once()
        self.assertEqual({"name": "new.docx"}, self.graph.call_args.kwargs["json_body"])

    def test_onenote_page_creation_escapes_html_and_requires_confirmation(self):
        self.graph.return_value = {"id": "page1", "title": "A & B"}
        result = office_graph.office_graph({
            "operation": "onenote_create_page", "section_id": "section1",
            "title": "A & B", "body": "<script>alert(1)</script>",
        })
        self.assertIn("Created OneNote page", result)
        self.assertIn("A &amp; B", self.graph.call_args.kwargs["data"])
        self.assertNotIn("<script>", self.graph.call_args.kwargs["data"])
        self.assertEqual("text/html; charset=utf-8", self.graph.call_args.kwargs["headers"]["Content-Type"])

    def test_forms_open_uses_official_web_interface_without_graph_token(self):
        with patch("webbrowser.open", return_value=True) as browser, patch.object(office_graph, "_access_token") as token:
            result = office_graph.office_graph({"operation": "forms_open"})
        self.assertIn("Forms", result)
        browser.assert_called_once_with("https://forms.office.com/")
        token.assert_not_called()


if __name__ == "__main__":
    unittest.main()
