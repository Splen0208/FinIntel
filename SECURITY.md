# Security and data handling

FinIntel is a single-user local application. The launcher binds to `127.0.0.1`. The API has no multi-user authentication. Do not expose it to the public Internet or bind it to a shared network interface.

Login profiles, database settings (including model keys and X credentials), resumes, source snapshots and reports are private local state. They live under `.local/` or `outputs/` and must not be committed, uploaded with bug reports, or included in release archives. `.gitignore` helps prevent accidental commits; it does not scrub an existing Git history.

Model analysis sends selected evidence to the configured model endpoint. Running the application locally does not imply offline inference. Contact information is removed from normalized evidence; inspect the selected material and provider configuration before use.

Platform login and verification belong to the user. Collection is read-only, bounded by per-run budgets and account gates. A challenge or account/environment risk stops collection; do not add CAPTCHA bypasses or automatic account switching.

To report a vulnerability, use the repository's private vulnerability reporting channel if enabled. If it is unavailable, ask the maintainer for a private contact. Never put credentials, resumes or browser profiles in a public issue.
