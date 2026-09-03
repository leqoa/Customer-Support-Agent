# Contributing to AI-ITSS

Thanks for contributing! This is a short guide to get a dev environment
running and to run the checks CI will run on your PR.

## Dev environment setup

```bash
python3 -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate

# Runtime deps live in requirements.txt; dev/test/lint tooling lives in
# requirements-dev.txt so a production deploy doesn't pull in test tooling.
pip install -r requirements.txt -r requirements-dev.txt
```

Copy `.env.example` to `.env` and fill in any values you need for the
integrations you're working on (see `.env.example` for what each variable
does). `config/settings.yaml` holds non-secret configuration only.

## Running tests

```bash
pytest
```

## Running the linter

```bash
ruff check .
```

Lint config lives in `pyproject.toml` (`[tool.ruff]`). It's intentionally
permissive for now — see the comments there — and is currently
non-blocking in CI while the codebase is brought up to a clean baseline.

## Phase 2 tracking

Phase 2 work is tracked in [`docs/PHASE1_SUMMARY.md`](docs/PHASE1_SUMMARY.md).
