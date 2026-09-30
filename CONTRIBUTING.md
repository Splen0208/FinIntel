# Contributing

Use Python 3.11+ and a project virtual environment. Install `requirements-dev.txt`, then run:

```powershell
.venv\Scripts\python -m pytest backend\tests -q
```

Tests use synthetic fixtures and isolated databases. Do not add real cookies, social posts, resumes, model keys or a developer's absolute filesystem paths. Do not run platform collection in a unit test.

For an adapter change, preserve the source identity, retrieval mode, full/partial/missing state and strict quote offsets. A nonempty summary is not proof of a full document. Never weaken validation to make a test or model response pass.

Please describe the user-visible change and relevant checks. Include sanitized error codes and dependency versions in issues; avoid full process environments, database dumps and login screenshots.
