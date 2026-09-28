# MailGrab

**Search Gmail. Download the attachments. Get on with your day.**

A small Python command-line tool for collecting PDFs or other attachments from batches of email. Built for the practical task of downloading files from 100–200 messages without opening each message manually.

No menus, server, or subscription. Your files stay in the local folder you choose. Google sign-in opens a browser on the first real run; everything else happens in the terminal.

```console
python mailgrab.py --query "from:billing@example.com has:attachment" --dry-run
python mailgrab.py --query "from:billing@example.com has:attachment" --output invoices
```

## Try it in 30 seconds — no account needed

Requires **Python 3.10 or newer**. Download this repository or clone it:

```console
git clone https://github.com/akifmaze/mailgrab.git
cd mailgrab
python mailgrab.py --demo --dry-run
python mailgrab.py --demo --output demo-downloads
python mailgrab.py --demo --output demo-downloads
```

The offline demo creates three synthetic invoice PDFs from three example messages. The last command skips all three already saved files. Add `--type all` to include a synthetic text attachment as well. Demo mode needs no packages, credentials, Gmail access, or network. It ignores `--query` and does not emulate Gmail search.

```text
First run:
[saved] 8d36865b63f0ef26f8b8_invoice.pdf
[saved] 8e829edc2f6530715c31_invoice.pdf
[saved] fa40b6515c0fa48dcb09_invoice.pdf
Summary: 3 messages, 3 matched, 3 downloaded, 0 skipped, 0 failed

Same command again:
Summary: 3 messages, 3 matched, 0 downloaded, 3 skipped, 0 failed
```

## What it does

- Accepts a Gmail search expression and follows all result pages.
- Downloads PDFs by default, or all attachment types with `--type all`.
- Finds attachments inside nested MIME parts and handles both embedded API data and separate attachment requests.
- Prefixes filenames with stable attachment IDs, so repeated names such as `invoice.pdf` do not overwrite one another.
- Saves a checkpoint after every completed file and verifies SHA-256 hashes before skipping it on the next run.
- Preserves edited or conflicting files and chooses a new filename for a replacement.
- Keeps a private JSON record connecting each saved file to its source message.
- Retries eligible transient Gmail API failures and continues past individual message/attachment errors.

## Connect your Gmail account

Each user supplies their own Google Cloud **Desktop app OAuth client**. No shared client secret or developer-owned mail service is bundled.

1. Follow Google's [Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python) to create/select a Cloud project and enable the Gmail API.
2. Configure the Google Auth platform's branding and audience. For a personal external app in testing, add your own Google account as a test user.
3. Create an OAuth client with application type **Desktop app**, download its JSON, and save it privately as `credentials.json` beside the script, or supply its location with `--credentials`.
4. Install the packages below, then run a small preview. Google will open a sign-in and consent page. Sign in to the intended account and review the requested permission.

Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe mailgrab.py --query "has:attachment filename:pdf" --max-messages 5 --dry-run
```

macOS / Linux:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python mailgrab.py --query "has:attachment filename:pdf" --max-messages 5 --dry-run
```

In the remaining examples, `python` means the interpreter from that environment. You can use its full path instead of activating it.

The requested scope is `gmail.readonly`. It allows reading mail, including message contents, not just attachments. MailGrab makes read requests only: it does not send mail, change labels, mark messages read, archive, or delete anything. The `gmail.metadata` scope is insufficient for this tool's search and attachment access. See Google's [scope reference](https://developers.google.com/workspace/gmail/api/auth/scopes).

The local browser callback uses a temporary loopback port. A browser on the same computer is needed for initial authorization; headless authorization is not supported in this version. Do not provide your Gmail password to the script.

## Everyday examples

Preview PDFs from a sender:

```console
python mailgrab.py --query "from:billing@example.com has:attachment" --dry-run
```

Download PDFs from a date range:

```console
python mailgrab.py --query "has:attachment after:2025/01/01 before:2026/01/01" --output invoices-2025
```

Download all attachment types from matching messages:

```console
python mailgrab.py --query "subject:coursework has:attachment" --type all --output coursework
```

Limit a first run and use credentials stored elsewhere:

```console
python mailgrab.py --query "has:attachment filename:pdf" --max-messages 10 --credentials /path/to/credentials.json
```

Run the exact command again to resume. Successful files are skipped only when the recorded file still exists and its hash matches. Missing files are downloaded again. Modified files remain untouched, with a numbered replacement saved alongside them. The same content attached to two different messages is kept twice: this is attachment-identity deduplication, not global content deduplication.

## Options

| Option | Default | Purpose |
| --- | --- | --- |
| `--query TEXT` | Required for Gmail | Gmail search expression; quote it in your shell |
| `--type pdf\|all` | `pdf` | Select PDFs or all recognized attachments |
| `--output PATH` | `downloads` | Local attachment folder |
| `--dry-run` | Off | Inspect and list matching parts without saving attachment files |
| `--max-messages N` | All matches | Cap inspected messages, not attachments |
| `--max-attachment-mb N` | `25` | Maximum individual file size in MiB |
| `--include-spam-trash` | Off | Include Spam and Trash in the API search |
| `--credentials PATH` | `credentials.json` | Your Desktop OAuth client JSON |
| `--token PATH` | See below | Private saved authorization file |
| `--demo` | Off | Use built-in synthetic messages, completely offline |
| `--version` | — | Print the version |

A real `--dry-run` still contacts Google, authorizes if needed, and retrieves full message structures. Those API responses may already contain inline attachment bytes and message text. It avoids separate attachment-body requests, attachment writes, and download-state creation; it is **not an offline or metadata-only mode**. Authorization may create or refresh the token file.

The search is passed to Gmail as written. The local attachment filter is applied afterward, so `--type all` does not override a restrictive `filename:pdf` in your query. Gmail API search can differ from the Gmail UI, particularly around thread-wide matching and aliases; see [Google's search guide](https://developers.google.com/workspace/gmail/api/guides/filtering).

## Output and recovery

```text
invoices/
  <stable-id>_invoice.pdf
  <another-id>_invoice.pdf
  .mailgrab-state.json
```

The state file is also the private download record: it contains source message IDs, sender, subject, original filename, local filename, byte count, hash, and save time. It is a record of successful saves across runs, not a complete Gmail backup or a full error audit. The terminal prints the current run's totals and errors. `downloaded` includes a complete file recovered after a missing checkpoint.

Do not delete the state file if you want normal skip/resume behavior. After an unexpected shutdown, complete files can be recognized again by their deterministic name and content hash. An incomplete file left by a power failure is preserved and a replacement receives a new name. A corrupted state file causes a stop rather than silent replacement.

Only one process may write to a given output folder. A normal exit or Ctrl+C releases `.mailgrab.lock`. After a forced termination or power loss, verify no MailGrab process is active before manually removing that lock. The lock contains the process ID for reference; a reused ID is not proof that the original process is still running.

Exit codes: **0** success (including no matches), **1** any download/API/filesystem failure, **2** invalid arguments, **130** Ctrl+C. Previously checkpointed downloads remain available after a later failure.

## Credentials and privacy

Default token locations:

- Windows: `%LOCALAPPDATA%\mailgrab\token.json` (with an AppData/Local fallback).
- macOS / Linux: `$XDG_CONFIG_HOME/mailgrab/token.json`, or `~/.config/mailgrab/token.json`.

Tokens are saved as local JSON, not in an encrypted credential vault. Use a private account/folder and appropriate operating-system access controls. `--token` lets you keep separate authorization files for separate accounts. Download identities include an account hash so two accounts do not accidentally share checkpoints.

Do not commit credentials, tokens, downloaded mail, reports, or terminal captures containing private filenames. The included `.gitignore` covers common default names, not every custom path. Review staged changes before pushing. MailGrab has no analytics or upload service; real mode communicates with Google for OAuth and Gmail access. See [SECURITY.md](SECURITY.md).

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| OAuth client file not found | Create a Desktop app OAuth client and pass its downloaded JSON via `--credentials`. |
| Access blocked / HTTP 403 | Check the Gmail API is enabled, your account is an allowed test user, and any Workspace administrator restrictions. |
| Saved authorization cannot refresh | Check connectivity first. If consent was revoked or expired, remove the local token file and authorize again. |
| Authentication expires after several days | External apps in testing can have seven-day refresh-token expiry for Gmail scopes; see [Google's token-expiration documentation](https://developers.google.com/identity/protocols/oauth2#expiration). |
| No PDFs found | Inspect the search, account, date range, `--max-messages`, and MIME/filename filter. Try a narrower known message. |
| HTTP 429 or transient server failure | Eligible failures are retried up to five times by the Google client. If retries fail, run the same command later. |
| Attachment too large | Increase `--max-attachment-mb` only if you have enough memory and trust the source. |
| Output folder locked | Check for another active run; remove a stale lock only after that check. |

## Limits and testing

- PDFs are selected by filename extension or MIME type, not validated as trustworthy PDF content. Files are never opened or executed automatically.
- `--type all` includes named inline images as well as ordinary attachments. Unnamed text/HTML message bodies are excluded; unnamed PDFs and explicit attachment parts are included.
- Remote download links inside emails and files stored in Drive are not followed.
- Attachments are loaded in memory, not streamed. The size limit is per file, not a total-run budget; full-message API responses can contain data before the filter is applied.
- Mailboxes changing during pagination can affect results. Re-run the query to collect newly matching messages; this is not a point-in-time archive.
- The initial release passes 28 offline tests on Windows with Python 3.11.9, covering API pagination, MIME traversal, file collisions, partial failures, checkpoint recovery, and account separation. Dependency installation and request construction were also checked with `google-api-python-client 2.200.0`, `google-auth 2.59.0`, and `google-auth-oauthlib 1.4.1`. It has not been validated against a live Gmail account or on macOS/Linux. Do not interpret mock tests as live OAuth verification.

Run all tests using the standard library:

```console
python -m unittest -v
```

## Contributing and license

Bug reports and small focused improvements are welcome. Include your Python version, command with personal values replaced, exit code, and a synthetic reproduction. Never post raw mail, tokens, OAuth client files, or download state. Add an offline regression test for a bug fix.

Original project code is under the [MIT License](LICENSE). Google client libraries remain under their own licenses. API behavior and setup were checked against the official documentation linked above; no previous private script or real email data is bundled. Gmail is a Google product; this project is independent and is not endorsed by Google.
