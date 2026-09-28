# Security and privacy

MailGrab is a local command-line downloader. It requests Gmail read-only access and has no service that receives your mail or attachments.

## Local files are private data

OAuth tokens grant account access within their scopes. The client JSON, saved token, attachments, download state, and terminal output may contain sensitive information. Keep them in private directories, use appropriate OS permissions, and never publish them. Tokens are stored in JSON, not an encrypted vault. The download record contains email sender and subject metadata.

The `.gitignore` is a guardrail, not a privacy guarantee. Custom output directories and differently named credential files may not be ignored. Review all staged files. If a token is disclosed, revoke the app's access in your Google account; deleting a commit or file alone is insufficient.

## Untrusted attachments

MailGrab sanitizes filenames, uses exclusive file creation, avoids overwriting existing files, and checks saved-file hashes for resume. It does not scan for malware, certify a file's real type, or make attachments safe to open. Do not use an output directory that an untrusted local user can modify concurrently. Symbolic-link checks are defensive guardrails, not a sandbox against a hostile local filesystem.

## Reporting a problem

For ordinary bugs, open an issue with synthetic inputs and no private data. For a vulnerability, use GitHub private vulnerability reporting if available. Otherwise, request a private contact channel without posting exploit details, credentials, or personal information in a public issue.
