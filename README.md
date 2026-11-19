# Large Data Analysis Agent

V5 is a local application for ingesting datasets and codebooks, reviewing prepared data,
publishing project assets to PostgreSQL, and working with analysis agents in a browser.
It includes resumable ingestion, conservative codebook matching, a Docker execution
sandbox, and a local web interface.

## Run locally

Use Python 3.11 or newer, PostgreSQL, and Docker Desktop (or Docker Engine).

```sh
python -m venv .venv
# Activate .venv for your shell before installing dependencies.
python -m pip install -r v_5/app/requirements.txt
docker build -t atlas-v5-sandbox:local v_5/app/sandbox
```

Set `OPENAI_API_KEY` in your environment. Configure PostgreSQL with
`ATLAS_DATABASE_URL`, or `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER` and `PGPASSWORD`
(a libpq password file also works). Without overrides, the database endpoint is
`localhost:5433`, database `atlas`, user `postgres`. Create that database and provide
credentials locally; no password is included in this repository.

```sh
python -B -m v_5.main --port 8766
```

Open http://127.0.0.1:8766. Runtime projects, original uploads, logs and recovery state
are written under `v_5/app/runtime/` and are excluded from Git. `V5_RUNTIME` can select
another local runtime directory. Model requests require access to the model names in
`v_5/app/config.py`.

## Code and tests

- [V5 architecture, workflows and checks](v_5/README.md)
- [Synthetic fixtures and expected energy-reconciler cases](v_5/tests/fixtures/README.md)
- [Energy project files and assets](v_5/tests/fixtures/tough_energy/)

Example offline checks, with scripted model replies and no paid API calls:

```sh
python -B -m v_5.tests.codebooks_check
python -B -m v_5.tests.master_codebook_check
python -B -m v_5.tests.reconciliation_resume_check
```

The tests directory includes its synthetic energy and library fixtures. The energy
workbook and DCT contain seven deliberate name/battery matching cases, with expected
decisions in the fixture README. These are fictional test assets, not user uploads.
Live database, browser and Docker checks have separate requirements documented in V5.

This repository contains application and test source, static UI assets, cleared example
notebooks and synthetic fixtures. It excludes credentials, real datasets, uploaded
projects, runtime outputs, saved agent state, and local Kiro/Codex workspace folders.
Checkpoint implementation code is included because it is part of ingestion recovery.
