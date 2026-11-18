# Contributing

- **Never** add a code path that writes to a monitored database or executes suggested SQL. Read-only is the product's safety model.
- Do not fabricate data in live code paths. Synthetic data belongs in `app/demo.py` and must stay labelled and separate.
- New diagnostics need: version-gated SQL in `app/pg/versions.py` (and a test in `tests/test_core.py`), an analyzer rule with stated limitations (`app/pg/analysis.py` + tests), a diagnostic card (`app/recommend.py`), and a line in `docs/flight-recorder.md`.
- Any new privilege requirement must be added to `docs/permissions.md` and degrade to an "unavailable" result, not an exception.
- Redact anything that came from the monitored database before it is stored or returned.
- Run `pytest -m "not live and not e2e"` before every commit. Run the live and browser tests when you touch collectors, the recorder, or the UI (`scripts/lab_setup.sh`, `playwright install chromium`).
- Do not claim a test passed unless you ran it; state in the PR which PostgreSQL versions you executed against.
- UI changes must work from the keyboard and in both themes; do not add dependencies or remote resources (the CSP forbids them).
