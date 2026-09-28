"""Offline tests with synthetic messages. No credentials or network required."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import mailgrab as mg


class MailGrabTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "downloads"

    def tearDown(self):
        self.temporary.cleanup()

    def run_demo(self, *extra):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = mg.main(["--demo", "--output", str(self.output), *extra])
        return status, stdout.getvalue(), stderr.getvalue()

    def execute(self, client, *extra):
        args = mg.parser().parse_args(["--demo", *extra])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with mg.output_lock(self.output):
                return mg.process(client, args, self.output, mg.load_state(self.output))

    def test_demo_saves_three_distinct_pdfs_with_same_original_name(self):
        status, text, errors = self.run_demo()
        self.assertEqual(status, 0, errors)
        self.assertIn("3 downloaded", text)
        files = list(self.output.glob("*.pdf"))
        self.assertEqual(len(files), 3)
        self.assertEqual(len({p.read_bytes() for p in files}), 3)
        self.assertTrue(all(p.read_bytes().startswith(b"%PDF-") for p in files))

    def test_repeat_run_skips_verified_files(self):
        self.run_demo()
        before = {p.name: p.stat().st_mtime_ns for p in self.output.glob("*.pdf")}
        status, text, _ = self.run_demo()
        self.assertEqual(status, 0)
        self.assertIn("0 downloaded, 3 skipped", text)
        self.assertEqual(before, {p.name: p.stat().st_mtime_ns for p in self.output.glob("*.pdf")})

    def test_all_includes_named_text_attachment(self):
        status, text, _ = self.run_demo("--type", "all")
        self.assertEqual(status, 0)
        self.assertIn("4 matched", text)
        self.assertEqual(len(list(self.output.glob("*.txt"))), 1)

    def test_dry_run_does_not_create_output_or_fetch_attachment_body(self):
        status, text, _ = self.run_demo("--dry-run")
        self.assertEqual(status, 0)
        self.assertIn("3 matched", text)
        self.assertFalse(self.output.exists())
        client = mg.DemoClient()
        client.message = Mock(return_value={"payload": {"filename": "a.pdf", "body": {"attachmentId": "remote"}}})
        client.attachment = Mock(side_effect=AssertionError("Must not fetch"))
        args = mg.parser().parse_args(["--demo", "--dry-run"])
        with redirect_stdout(io.StringIO()):
            mg.process(client, args, self.output, None)
        client.attachment.assert_not_called()

    def test_deleted_file_downloads_again(self):
        self.run_demo()
        next(self.output.glob("*.pdf")).unlink()
        _, text, _ = self.run_demo()
        self.assertIn("1 downloaded, 2 skipped", text)

    def test_edited_file_is_preserved_and_replacement_gets_new_name(self):
        self.run_demo()
        edited = next(self.output.glob("*.pdf"))
        edited.write_bytes(b"user's annotations")
        status, text, _ = self.run_demo()
        self.assertEqual(status, 0)
        self.assertEqual(edited.read_bytes(), b"user's annotations")
        self.assertIn("1 downloaded, 2 skipped", text)
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 4)
        _, text, _ = self.run_demo()
        self.assertIn("0 downloaded, 3 skipped", text)

    def test_complete_file_without_checkpoint_is_recovered(self):
        self.run_demo()
        (self.output / mg.STATE_NAME).unlink()
        status, _, _ = self.run_demo()
        self.assertEqual(status, 0)
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 3)

    def test_partial_or_conflicting_existing_file_is_not_overwritten(self):
        self.output.mkdir()
        (self.output / "invoice.pdf").write_bytes(b"partial")
        name, _ = mg.save_without_overwrite(self.output, "invoice.pdf", b"complete")
        self.assertEqual(name, "invoice-2.pdf")
        self.assertEqual((self.output / "invoice.pdf").read_bytes(), b"partial")

    def test_two_accounts_have_separate_attachment_identities(self):
        self.execute(mg.DemoClient())
        second = mg.DemoClient()
        second.account = lambda: "another@example.invalid"
        result = self.execute(second)
        self.assertEqual(result["downloaded"], 3)
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 6)

    def test_lock_does_not_allow_simultaneous_writers(self):
        with mg.output_lock(self.output):
            with self.assertRaises(mg.MailGrabError):
                with mg.output_lock(self.output):
                    self.fail("Second writer acquired lock")
            self.assertTrue((self.output / mg.LOCK_NAME).exists())
        self.assertFalse((self.output / mg.LOCK_NAME).exists())

    def test_corrupt_manifest_is_not_overwritten(self):
        self.output.mkdir()
        path = self.output / mg.STATE_NAME
        path.write_text("not json", encoding="utf-8")
        status, _, _ = self.run_demo()
        self.assertEqual(status, 1)
        self.assertEqual(path.read_text(), "not json")

    def test_manifest_cannot_reference_paths_outside_output(self):
        self.output.mkdir()
        mg.atomic_json(self.output / mg.STATE_NAME, {"version": 1, "files": {
            "test": {"saved_as": "../secret.pdf", "sha256": "0" * 64}}})
        with self.assertRaises(mg.MailGrabError):
            mg.load_state(self.output)

    def test_filenames_remove_path_traversal_windows_reserved_names_and_controls(self):
        self.assertEqual(mg.safe_name("../../invoice.pdf"), "invoice.pdf")
        self.assertEqual(mg.safe_name(r"C:\private\invoice.pdf"), "invoice.pdf")
        self.assertEqual(mg.safe_name("CON.pdf"), "_CON.pdf")
        self.assertEqual(mg.safe_name("nul"), "_nul")
        self.assertNotIn("\x1b", mg.safe_name("\x1b[31mred.pdf"))
        self.assertNotIn("\u202e", mg.safe_name("report\u202eexe.pdf"))
        self.assertNotIn(":", mg.safe_name("invoice.pdf:stream"))
        self.assertEqual(mg.safe_name(".."), "attachment")
        self.assertLess(len(mg.safe_name("\U0001f4c4" * 100 + ".pdf").encode("utf-8")), 200)

    def test_nested_mime_top_level_pdf_and_unnamed_attachment(self):
        part = {"mimeType": "multipart/mixed", "parts": [
            {"mimeType": "text/plain", "body": {"data": "Ym9keQ=="}},
            {"mimeType": "multipart/mixed", "parts": [
                {"mimeType": "application/pdf", "body": {"data": ""}},
                {"mimeType": "application/octet-stream", "headers": [
                    {"name": "Content-Disposition", "value": "attachment"}], "body": {"data": ""}}
            ]}]}
        found = list(mg.attachments(part))
        self.assertEqual([x.path for x in found], ["0.1.0", "0.1.1"])
        self.assertEqual([x.name for x in found], ["attachment.pdf", "attachment.bin"])
        self.assertEqual(len(list(mg.attachments({"mimeType": "application/pdf"}))), 1)

    def test_base64url_padding_and_validation(self):
        raw = b"\xfb\xff\x00"
        self.assertEqual(mg.decode_data(base64.urlsafe_b64encode(raw).decode().rstrip("=")), raw)
        self.assertEqual(mg.decode_data("YQ"), b"a")
        with self.assertRaises(mg.MailGrabError):
            mg.decode_data("not@base64")

    def test_remote_attachment_fetch_and_size_validation(self):
        client = mg.DemoClient()
        client.messages = lambda *args: iter(["external"])
        client.message = lambda _: {"payload": {"filename": "invoice.pdf", "body": {
            "attachmentId": "attachment-1", "size": 3}}}
        client.attachment = Mock(return_value={"data": "YWJj"})
        result = self.execute(client)
        self.assertEqual(result["downloaded"], 1)
        client.attachment.assert_called_once_with("external", "attachment-1")
        self.assertEqual(next(self.output.glob("*.pdf")).read_bytes(), b"abc")

    def test_one_failure_does_not_discard_other_downloads(self):
        client = mg.DemoClient()
        original = client.message
        def broken(message_id):
            if message_id == "demo-002":
                raise RuntimeError("secret server details")
            return original(message_id)
        client.message = broken
        result = self.execute(client)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["downloaded"], 2)
        self.assertEqual(mg.error_label(RuntimeError("SECRET")), "RuntimeError")

    def test_oversized_attachment_is_not_fetched(self):
        client = mg.DemoClient()
        client.messages = lambda *args: iter(["large"])
        client.message = lambda _: {"payload": {"filename": "large.pdf", "body": {
            "size": 30 * 1024 * 1024, "attachmentId": "large"}}}
        client.attachment = Mock()
        result = self.execute(client)
        self.assertEqual(result["failed"], 1)
        client.attachment.assert_not_called()
        self.assertEqual(list(self.output.glob("*.pdf")), [])

    def test_size_mismatch_is_not_saved(self):
        client = mg.DemoClient()
        client.messages = lambda *args: iter(["bad"])
        client.message = lambda _: {"payload": {"filename": "bad.pdf", "body": {"size": 20, "data": "YQ"}}}
        self.assertEqual(self.execute(client)["failed"], 1)
        self.assertEqual(list(self.output.glob("*.pdf")), [])

    def test_checkpoint_failure_can_resume_without_duplicate_files(self):
        with patch.object(mg, "atomic_json", side_effect=OSError("disk full")):
            status, _, _ = self.run_demo()
        self.assertEqual(status, 1)
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 1)
        self.assertFalse((self.output / mg.LOCK_NAME).exists())
        self.assertEqual(self.run_demo()[0], 0)
        self.assertEqual(len(list(self.output.glob("*.pdf"))), 3)

    def test_state_records_source_and_hash(self):
        self.run_demo()
        state = mg.load_state(self.output)
        row = next(iter(state["files"].values()))
        self.assertEqual(row["original_name"], "invoice.pdf")
        self.assertIn("message_id", row)
        self.assertIn("sender", row)
        self.assertEqual(row["sha256"], mg.file_hash(self.output / row["saved_as"]))

    def test_max_messages_limits_demo(self):
        self.assertIn("2 messages", self.run_demo("--max-messages", "2")[1])

    def test_missing_query_and_invalid_limit_exit_two(self):
        for args in [[], ["--demo", "--max-messages", "0"]]:
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                mg.main(args)
            self.assertEqual(caught.exception.code, 2)


class GmailPaginationTests(unittest.TestCase):
    def service_with_pages(self, pages):
        service = Mock()
        resource = service.users.return_value.messages.return_value
        resource.list.side_effect = [Mock(execute=Mock(return_value=page)) for page in pages]
        return mg.GmailClient(service), resource

    def test_all_pages_are_followed_beyond_two_hundred_messages(self):
        client, resource = self.service_with_pages([
            {"messages": [{"id": str(i)} for i in range(500)], "nextPageToken": "page-two"},
            {"messages": [{"id": str(i)} for i in range(500, 620)]}])
        result = list(client.messages("has:attachment", None, False))
        self.assertEqual(len(result), 620)
        self.assertEqual(resource.list.call_args.kwargs["pageToken"], "page-two")

    def test_limit_is_messages_not_attachments(self):
        client, resource = self.service_with_pages([
            {"messages": [{"id": str(i)} for i in range(500)], "nextPageToken": "more"}])
        self.assertEqual(len(list(client.messages("x", 200, True))), 200)
        self.assertEqual(resource.list.call_args.kwargs["maxResults"], 200)
        self.assertTrue(resource.list.call_args.kwargs["includeSpamTrash"])
        self.assertEqual(resource.list.call_count, 1)

    def test_duplicate_ids_across_pages_are_not_reprocessed(self):
        client, _ = self.service_with_pages([
            {"messages": [{"id": "a"}], "nextPageToken": "next"},
            {"messages": [{"id": "a"}, {"id": "b"}]}])
        self.assertEqual(list(client.messages("x", None, False)), ["a", "b"])

    def test_empty_result(self):
        client, _ = self.service_with_pages([{}])
        self.assertEqual(list(client.messages("x", None, False)), [])

    def test_repeated_page_token_stops_instead_of_looping(self):
        client, _ = self.service_with_pages([
            {"messages": [{"id": "a"}], "nextPageToken": "same"},
            {"messages": [{"id": "a"}], "nextPageToken": "same"}])
        with self.assertRaises(mg.MailGrabError):
            list(client.messages("x", None, False))


if __name__ == "__main__":
    unittest.main()
