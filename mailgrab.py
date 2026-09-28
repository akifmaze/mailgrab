#!/usr/bin/env python3
"""Search Gmail and download attachments. No menus, mailbox writes, or telemetry."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

VERSION = "0.1.0"
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
STATE_NAME = ".mailgrab-state.json"
LOCK_NAME = ".mailgrab.lock"


class MailGrabError(Exception):
    """An actionable error safe to show without exposing server responses."""


def fingerprint(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_name(name: str) -> str:
    """Create one portable filename, never a path or terminal control sequence."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join("_" if unicodedata.category(c).startswith("C") else c for c in name)
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    name = name or "attachment"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])", name.split(".")[0]):
        name = "_" + name
    # Leave room for the stable ID prefix and collision suffix on Windows.
    suffix = Path(name).suffix[:16]
    stem = name[:-len(suffix)] if suffix else name
    # Also respect byte-based filename limits on Unix filesystems.
    stem = stem[:80].encode("utf-8")[:160].decode("utf-8", errors="ignore")
    return stem + suffix


def decode_data(value: str) -> bytes:
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error, TypeError) as exc:
        raise MailGrabError("Invalid attachment encoding; nothing was saved.") from exc


@dataclass(frozen=True)
class Attachment:
    path: str
    name: str
    mime: str
    body: dict


def attachments(part: dict, path: str = "0"):
    """Visit nested MIME parts, including a top-level attachment."""
    mime = part.get("mimeType", "application/octet-stream").lower()
    name = part.get("filename", "")
    disposition = next((h.get("value", "") for h in part.get("headers", [])
                        if h.get("name", "").lower() == "content-disposition"), "")
    is_attachment = bool(name) or disposition.lower().startswith("attachment")
    # Unnamed PDFs are useful attachments too; ordinary HTML/text bodies are not.
    if is_attachment or mime == "application/pdf":
        fallback = "attachment.pdf" if mime == "application/pdf" else "attachment.bin"
        yield Attachment(path, name or fallback, mime, part.get("body", {}))
    for index, child in enumerate(part.get("parts", [])):
        yield from attachments(child, f"{path}.{index}")


def selected(attachment: Attachment, kind: str) -> bool:
    return (kind == "all" or attachment.mime == "application/pdf"
            or attachment.name.lower().endswith(".pdf"))


def atomic_json(path: Path, value: dict) -> None:
    if path.is_symlink():
        raise MailGrabError("Refusing to replace a symbolic-link state or token file.")
    fd, temporary = tempfile.mkstemp(prefix=".mailgrab-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def output_lock(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    lock = output / LOCK_NAME
    try:
        handle = lock.open("x", encoding="ascii")
    except FileExistsError as exc:
        raise MailGrabError(
            "Output folder is locked. Stop the other MailGrab process first. "
            "After a crash, remove .mailgrab.lock only when no run is active."
        ) from exc
    try:
        with handle:
            handle.write(str(os.getpid()))
        yield
    finally:
        lock.unlink(missing_ok=True)


def load_state(output: Path) -> dict:
    path = output / STATE_NAME
    if path.is_symlink():
        raise MailGrabError("Refusing a symbolic-link state file.")
    if not path.exists():
        return {"version": 1, "files": {}}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("version") != 1 or not isinstance(value.get("files"), dict):
            raise ValueError("Unsupported state")
        for record in value["files"].values():
            name = record["saved_as"]
            if (not isinstance(name, str) or not name or name in {".", ".."}
                    or any(c in name for c in '/\\:') or Path(name).name != name
                    or not re.fullmatch(r"[a-f0-9]{64}", record["sha256"])):
                raise ValueError("Invalid state record")
        return value
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise MailGrabError("Invalid state file. Preserve it and use a new output folder.") from exc


def current_file(output: Path, record: dict | None) -> bool:
    if not record:
        return False
    path = output / record["saved_as"]
    return not path.is_symlink() and path.is_file() and file_hash(path) == record["sha256"]


def save_without_overwrite(output: Path, name: str, data: bytes) -> tuple[str, str]:
    """Recover complete unrecorded files, or create a new exclusive filename."""
    digest = hashlib.sha256(data).hexdigest()
    base = Path(name)
    for number in range(1, 10001):
        candidate = name if number == 1 else f"{base.stem}-{number}{base.suffix}"
        path = output / candidate
        if path.is_symlink():
            continue
        if path.is_file():
            if file_hash(path) == digest:
                return candidate, digest
            continue
        try:
            handle = path.open("xb")
        except (FileExistsError, IsADirectoryError):
            continue
        try:
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            # Only this newly created file is removed on a handled interruption.
            path.unlink(missing_ok=True)
            raise
        return candidate, digest
    raise MailGrabError("Too many filename collisions; use a new output folder.")


def error_label(exc: Exception) -> str:
    """Do not dump API URLs, tokens, queries, or mail content to the console."""
    if isinstance(exc, MailGrabError):
        return str(exc)
    status = getattr(getattr(exc, "resp", None), "status", None)
    return f"Gmail HTTP {status}" if status else type(exc).__name__


class GmailClient:
    def __init__(self, service):
        self.service = service

    @staticmethod
    def execute(request):
        return request.execute(num_retries=5)

    def account(self):
        return self.execute(self.service.users().getProfile(userId="me"))["emailAddress"].lower()

    def messages(self, query, limit, include_spam_trash):
        token = None
        seen = set()
        page_tokens = set()
        while True:
            args = dict(userId="me", q=query, maxResults=min(500, limit - len(seen))
                        if limit else 500, includeSpamTrash=include_spam_trash)
            if token:
                args["pageToken"] = token
            page = self.execute(self.service.users().messages().list(**args))
            for message in page.get("messages", []):
                message_id = message["id"]
                if message_id not in seen:
                    seen.add(message_id)
                    yield message_id
                    if limit and len(seen) >= limit:
                        return
            token = page.get("nextPageToken")
            if not token:
                return
            if token in page_tokens:
                raise MailGrabError("Gmail repeated a pagination token. Re-run the query later.")
            page_tokens.add(token)

    def message(self, message_id):
        return self.execute(self.service.users().messages().get(
            userId="me", id=message_id, format="full"))

    def attachment(self, message_id, attachment_id):
        return self.execute(self.service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id))


def default_token() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "mailgrab" / "token.json"


def connect(credentials_path: Path, token_path: Path):
    # Lazy imports keep --help, --demo, and unit tests usable without dependencies.
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise MailGrabError("Install dependencies: python -m pip install -r requirements.txt") from exc

    credentials = None
    if token_path.is_symlink():
        raise MailGrabError("Refusing a symbolic-link token file.")
    if token_path.exists():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
            if not credentials.valid and credentials.refresh_token:
                credentials.refresh(Request())
        except Exception as exc:
            raise MailGrabError(
                "Saved authorization could not be loaded or refreshed. Check your network. "
                "If consent expired or was revoked, remove the local token file and sign in again."
            ) from exc
    if not credentials or not credentials.valid:
        if not credentials_path.is_file():
            raise MailGrabError("OAuth client file not found. Follow README setup, then use --credentials PATH.")
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent",
                                                timeout_seconds=180)
        except Exception as exc:
            raise MailGrabError("Google sign-in failed or timed out. Check OAuth setup and try again.") from exc
    token_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(token_path, json.loads(credentials.to_json()))
    return GmailClient(build("gmail", "v1", credentials=credentials, cache_discovery=False))


def demo_pdf(label: str) -> bytes:
    """Generate a tiny valid one-page PDF containing only synthetic text."""
    stream = f"BT /F1 18 Tf 50 750 Td ({label}) Tj ET".encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
               b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    data = b"%PDF-1.4\n"
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(data)
    data += b"xref\n0 6\n0000000000 65535 f \n"
    data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return data + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()


class DemoClient:
    """Offline examples; intentionally no Gmail search-language emulation."""
    def account(self):
        return "demo@example.invalid"

    def messages(self, query, limit, include_spam_trash):
        yield from ["demo-001", "demo-002", "demo-003"][:limit or None]

    def message(self, message_id):
        number = int(message_id[-1])
        pdf = demo_pdf(f"Sample invoice {number} - no real customer data")
        part = {"filename": "invoice.pdf", "mimeType": "application/pdf",
                "body": {"size": len(pdf), "data": base64.urlsafe_b64encode(pdf).decode()}}
        parts = [part]
        if number == 3:
            text = b"Synthetic attachment generated by MailGrab demo.\n"
            parts.append({"filename": "notes.txt", "mimeType": "text/plain",
                          "body": {"size": len(text), "data": base64.urlsafe_b64encode(text).decode()}})
        return {"id": message_id, "internalDate": "1735689600000", "payload": {
            "mimeType": "multipart/mixed", "headers": [
                {"name": "From", "value": "Sample Sender <sender@example.invalid>"},
                {"name": "Subject", "value": f"Sample invoice {number}"}],
            "parts": [{"mimeType": "multipart/mixed", "parts": parts}]}}

    def attachment(self, message_id, attachment_id):
        raise AssertionError("Demo attachments are inline API data.")


def process(client, args, output: Path, state: dict | None) -> dict:
    account_key = fingerprint(client.account())
    totals = dict(messages=0, matched=0, downloaded=0, skipped=0, failed=0)
    maximum = int(args.max_attachment_mb * 1024 * 1024)
    for message_id in client.messages(args.query or "", args.max_messages, args.include_spam_trash):
        totals["messages"] += 1
        try:
            message = client.message(message_id)
        except Exception as exc:
            totals["failed"] += 1
            print(f"[error] Message {fingerprint(message_id)[:10]}: {error_label(exc)}", file=sys.stderr)
            continue
        payload = message.get("payload", {})
        headers = {h.get("name", "").lower(): h.get("value", "") for h in payload.get("headers", [])}
        for item in attachments(payload):
            if not selected(item, args.type):
                continue
            totals["matched"] += 1
            key = fingerprint(f"{account_key}\0{message_id}\0{item.path}")
            name = f"{key[:20]}_{safe_name(item.name)}"
            if args.dry_run:
                size = item.body.get("size", "unknown")
                print(f"[preview] {name} ({size} bytes)")
                continue
            try:
                if current_file(output, state["files"].get(key)):
                    totals["skipped"] += 1
                    print(f"[skip] {name}")
                    continue
                if int(item.body.get("size", 0)) > maximum:
                    raise MailGrabError("Attachment exceeds --max-attachment-mb.")
                body = item.body
                if body.get("attachmentId"):
                    body = client.attachment(message_id, body["attachmentId"])
                if "data" not in body:
                    raise MailGrabError("Attachment data is missing.")
                # Check encoded length too, before allocating decoded bytes.
                if len(body["data"]) > ((maximum + 2) // 3) * 4:
                    raise MailGrabError("Attachment exceeds --max-attachment-mb.")
                data = decode_data(body["data"])
                if len(data) > maximum:
                    raise MailGrabError("Attachment exceeds --max-attachment-mb.")
                if "size" in item.body and len(data) != int(item.body["size"]):
                    raise MailGrabError("Attachment size did not match Gmail metadata.")
                saved_as, digest = save_without_overwrite(output, name, data)
            except Exception as exc:
                totals["failed"] += 1
                print(f"[error] {name}: {error_label(exc)}", file=sys.stderr)
                continue
            state["files"][key] = dict(
                account_key=account_key, message_id=message_id, mime_part=item.path,
                original_name=item.name, saved_as=saved_as, sha256=digest, size=len(data),
                mime_type=item.mime, sender=headers.get("from", ""), subject=headers.get("subject", ""),
                message_timestamp_ms=message.get("internalDate"),
                saved_at=datetime.now(timezone.utc).isoformat())
            # A failed checkpoint stops the run; existing files remain recoverable.
            atomic_json(output / STATE_NAME, state)
            totals["downloaded"] += 1
            print(f"[saved] {saved_as}")
    print("Summary: " + ", ".join(f"{count} {label}" for label, count in totals.items()))
    if args.dry_run:
        print("Preview only: no attachment files or download state were written.")
    else:
        print(f"Private download record: {output / STATE_NAME}")
    return totals


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--version", action="version", version=f"MailGrab {VERSION}")
    result.add_argument("--query", help="Gmail search expression; required unless --demo is used")
    result.add_argument("--type", choices=["pdf", "all"], default="pdf", help="attachment filter (default: pdf)")
    result.add_argument("--output", type=Path, default=Path("downloads"), help="output folder (default: downloads)")
    result.add_argument("--dry-run", action="store_true", help="preview matches without saving attachments")
    result.add_argument("--max-messages", type=positive_int, help="maximum messages to inspect (default: all matches)")
    result.add_argument("--max-attachment-mb", type=positive_int, default=25, help="per-file limit in MiB (default: 25)")
    result.add_argument("--include-spam-trash", action="store_true", help="include Spam and Trash in search")
    result.add_argument("--credentials", type=Path, default=Path("credentials.json"), help="Google Desktop OAuth client JSON")
    result.add_argument("--token", type=Path, default=default_token(), help="private OAuth token location (see README)")
    result.add_argument("--demo", action="store_true", help="use three synthetic emails offline; ignores --query")
    return result


def main(argv=None):
    command = parser()
    args = command.parse_args(argv)
    if not args.demo and not (args.query and args.query.strip()):
        command.error("--query is required; use --demo for an offline example")
    try:
        client = DemoClient() if args.demo else connect(args.credentials, args.token)
        if args.demo:
            print("MailGrab demo: synthetic data only; no network or Google sign-in.")
        output = args.output.resolve()
        if args.dry_run:
            result = process(client, args, output, None)
        else:
            with output_lock(output):
                result = process(client, args, output, load_state(output))
        return 1 if result["failed"] else 0
    except KeyboardInterrupt:
        print("\nInterrupted. Run the same command again to resume.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {error_label(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
