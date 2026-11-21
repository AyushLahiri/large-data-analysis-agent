# V5: local ingestion and analysis

Install dependencies and configure credentials as described in the root README, then run from the repository root:

```powershell
python -B -m v_5.main --port 8766
```

Open http://127.0.0.1:8766. Start Docker Desktop before preparing data.
The sandbox image is `atlas-v5-sandbox:local`; rebuild it with
`docker build -t atlas-v5-sandbox:local v_5/app/sandbox` when its packages change.
The API key uses `OPENAI_API_KEY` or the repository's `keys.txt`.
Database connection setup lives in `app/database.py`.

## Exact-file reuse and folder setup

Projects currently belong to admin user `9999`; `public.atlas_projects.user_id`
stores that owner. Original uploads and verbatim user descriptions are saved locally
under the runtime's `users/9999/projects/<project-id>` directory. Deleting a file or
project also removes those retained originals and excludes them from copy matching.
Duplicate notices are derived from live files, not a saved list of past matches.

Work through one folder at a time: upload the main data file without questions, then
upload and tag the remaining files. Single uploads open their tag window immediately;
batches use Save and next. Choose **Complete ingestion** once everything is uploaded.
All data questionnaires appear together: the first answers fill the other forms until
you edit a field individually. Save and next reviews each file before the folder form.
A published folder's
**Done** button unlocks the next folder. Submitted answers stay frozen until **Restart
ingestion**, which resets that folder and later results while retaining active uploads.

Exact matching uses SHA256 and raw size. Partial matches remain ordinary ingestion.
An explicitly selected complete data-set match copies the original answers and skips
loading/descriptives. Changed codebooks still go through planning and reconciliation;
an identical, reused codebook set can copy the finished SQL assets directly. A file
tagged as a report is never reused as a codebook. Independent ingestion is always an
available choice.

Each normalized source codebook is published as
`<project-id>.ingestedcodebook_<filename>_<extension>` (with a suffix for name collisions).
Its file identity, folder/dataset and table name are recorded separately in the registry's
`publication.ingested_codebooks`. Completed older local codebooks are registered on demand
when a matching upload needs them. SQL copies create independent destination tables and
remap shared codebook references in the same publication transaction.

After final reconciliation, `master_to_codebook_reconciler` checks only master variables
with zero exact-name matches in any source codebook. Each fresh 50-variable conversation
proposes up to three whole-name patterns per variable; Python returns the full matching
unmatched-source rows. No hits means no further attempt. Supported updates change only
returned master variables. Used source rows move into `codebook_soft_matched`; its
`soft_match` array lists the master names, with explanations under 30 words keyed by
those names in `other_information`. Remaining source rows stay in `codebook_unmatched`.
The new stage uses the same Sol/medium settings as final reconciliation.
Check it offline with `python -B -m v_5.tests.master_codebook_check`.

Focused checks: `python -B -m v_5.tests.reuse_check` (no paid calls) and
`python -B -m v_5.tests.reuse_sql_check` (temporary Atlas project schemas, cleaned afterward).

## Code map

```text
v_5/
  ingest/
    1_initial_ingestion/   Material review, file placement and saved handoff
      agent.py            Ingestion agent prompt and response contracts
      sub_agents/         Context checker and parallel file reviewers
    2_data_loader/        Data preparation loop and progress state
      agent.py            Loader prompt and prepared-data contracts
      sub_agents/         Loader handoff summarizer and rollover implementation
    3_descriptives_check/ Data review and correction handoff to the loader
      agent.py            Descriptives reviewer prompt and contract
      sub_agents/         Shared execution-error diagnostic contract
    4_codebook_ingestion/ Codebook assembly, reconciliation and finalization
      agent.py            Final codebook agent prompts and contracts
      sub_agents/         Structured, PDF, PDF merge and overview workers
    agent.py              Shared user-question contract and final-summary agent
    workflow.py           Initial review -> preparation entry point
    readers.py            Readers used across ingestion phases
    manifest.py           Prepared-data manifest paths and validation
    publishing.py         Ingestion-specific Atlas publication
    summary.py            Project completion summary and persistence
    prototype/            Legacy notebook helpers and compatibility imports
  analyse/
    1_main/
      agent.py            Orchestrator, analyst and formatter prompts and contracts
      analyst.py          Analyst actions and skill loading
      workflow.py         Orchestration of analysis requests
      memory.py           Context accounting and completed-turn rollover
      execution.py        SQL validation, execution and Python-frame extraction
      skills/             Analyst method guides (general/, stats_methods/)
      sub_agents/         SQL review, glitch diagnostics and visualizer prompts, contracts and worker
    2_managers/
      agent.py            Session manager prompt and response contracts
      turns.py            Turn grouping, navigation, summaries and transcripts
      chats.py            Chat records and their SQL persistence
      tables.py           Local source tables, stable IDs and parent-turn context
      routes.py           Analysis chat HTTP endpoints and job management
    3_manageSQLassets/
      catalog.py          Atlas metadata, codebooks and variable search
      joins.py            Join plans, identifier matching and saved join notes
    general.py            Shared analysis progress, result limits and errors
  main_functions/
    agent.py              One shared glitch-agent prompt and contract
    session.py            Model calls, cancellation, logging and session API
    sql.py                Shared SQL identifiers, transactions and project lifecycle
  app/                    Technical infrastructure and local runtime
    config.py             Model settings, formats and runtime paths
    database.py           Database connections
    docker.py             Sandbox process execution
    sandbox/              Docker image and package requirements
    runtime/              Projects, uploads, logs, outputs and analysis artifacts
  UI/                     Browser HTML, JavaScript, styles and vendor assets
    analysis.js           Chat messages, table actions and visualization requests
    charts.js             Chart card shared by chat and Chrome draft previews
  main.py                 Mixed application entry point and project HTTP routes
  tests/                  Offline checks and manual-upload fixtures
  ingestion.ipynb         String-only PDF-worker checkpoint/recovery experiment
  prototype_data.py       Twenty tiny source strings and simplified Luna prompts
  prototype_runtime.py    Fake/capped API adapters for the production PDF pipeline
```

Every numbered ingestion phase has one `agent.py` and one
`sub_agents/agent.py`. Prompt text and its response contracts stay together.
Specialized worker functions live beside that phase's subagents. A phase that
uses the shared glitch agent imports its single definition rather than copying
it. The final project-summary agent remains at the ingestion root because it
spans all prepared folders.

The ingestion agent, loader, descriptives reviewer and final codebook agent are
the main phase agents. Context checking, file findings, loader summaries,
structured/PDF codebook extraction, PDF reconciliation and planning overviews
are subordinate calls. The analysis orchestrator routes questions to the analyst.
SQL review, error diagnosis, answer formatting and table visualization have
dedicated workers. The session manager lives in `2_managers`.

Numbered directories use readable Python package aliases registered in the
parent `__init__.py`. For example:

```python
from v_5.ingest.data_loader.agent import LoaderReply
from v_5.analyse.main.analyst import Analyst
from v_5.analyse.managers.turns import new_turn
from v_5.analyse.sql_assets.catalog import AtlasCatalog
```

These imports resolve directly to the numbered folders. Do not import the same
modules using numeric names through `importlib`, since that creates a second
module identity. The root prototype notebook imports the same live codebook contracts.

The root `ingestion.ipynb` calls the production PDF pipeline with four workers and
twenty short strings, with no PDF file or parser. Only the test inputs, prompts,
block sizes and output allowance change; there is no second workflow implementation.
Run All uses an offline simulated provider. `python -B -m v_5.tests.prototype_checkpoint_check`
runs the failure matrix; add `--live` to test Luna/high with the shared $5 ceiling.
Each PDF source keeps one `checkpoint.json`: its current range, thread/response IDs,
current output/table and accumulated master/notes. Readers and final cross-reader
batches use `sent`, `output_received`, `table_created`, `merged`. The within-reader
child has only the first three flags: its parent owns the master update. Children
have no separate range; final reconciliation tracks its 30-variable batch range.
Each reader covers five pages (up to 20 readers for the 100-page maximum), so a Luna
conversation stays short. Each agent has a 350,000-token budget of cumulative usage and
the current pending request's reservation; unknown submissions retain that reservation.
Data and flags are saved together. Restarting continues the saved phase and range;
completed pages are not replayed. Advancing replaces the previous cycle. Chats stay
in API conversations; there are no local chat copies or per-call archives.
Known responses are retrieved; unknown submissions are retried in the same thread.
A rejected reply (incomplete, invalid JSON, blank or repeated variable names, or a
merger's wrong variables) or an expired response is asked again in the same thread with
the exact problem; `rejections` and `feedback` are saved with the agent, and the third
rejection stops the run. Final-codebook and soft-match calls follow the same rule.
Final exports are the joined table, normalized table, master rows, notes and verification.
The app's Prepare data retry follows the saved `codebook_resume` workspace pointer
and skips the already-confirmed loader/review. Editing source metadata or starting
a new material review clears that pointer. Completed structured sources are reused;
unfinished structured work may repeat model work using its saved execution feedback.
Test controls and
the shared spending ledger are separate from recovery state. The tests cover real
process kills, interrupted submissions/retrievals, failed replies, disk-write errors
and resuming a child or a later batch. No rollover is exercised by this short toy.
All experiment outputs stay in `app/runtime/prototype_checkpoint_lab/`; current
results are `simple_offline_results.json` and `simple_live_results.json`.

## Workflow and rollover ownership

Initial ingestion reviews files, resolves context and saves a handoff before
the user chooses Prepare data. Preparation then handles each folder: loading,
descriptives confirmation, codebook ingestion/finalization and SQL publication.
A descriptives correction returns to the same loader loop. Each folder is
published before the next folder is prepared; the final summary covers the
completed project.

The loader retains its 250,000-token session budget and 8,000-token turn output
reserve. Its subagent summarizes history in 60,000-character chunks, retaining
saved work and the latest payload. Codebook calls have their own 250,000-token
budget in `4_codebook_ingestion/sessions.py`; only existing supported callers
roll over. Each analysis chat keeps separate GPT-6.2 Sol API conversations for its
orchestrator and analyst. Each role rolls over at an 800,000-token context window,
including an 8,000-token response allowance. The orchestrator resumes from its last
five question exchanges, including its own recall steps and replies. The analyst
resumes with ten completed-turn summaries and the preceding ten headers and can
request more detail by turn ID. Each delegated question includes up to ten previously
selected variables in scope. A fresh GPT-6 Luna formatter at `xhigh` writes the
answer from the analyst's handoff. Turn grouping and persistent chat records live
in `2_managers`.

`main_functions` contains shared workflow support. `app` contains configuration,
database connectivity, sandbox infrastructure and runtime assets. Publication
SQL stays in ingestion; analysis catalog/join operations stay in analysis;
chat-specific SQL stays with chat management. The `Session` API delegates its
ingestion-specific state updates to the relevant phase without changing callers.

In analysis chat, a small timer under each answer counts seconds from the
question until the first visible reply. At one minute it displays minutes and
remaining seconds. Its duration comes from the saved question and reply
timestamps, so it is available when the chat reopens. Beside it is the turn's
model cost in dollars (input, cached input and output tokens at the `PRICES`
rates in `app/config.py`), kept per turn in `turn_costs`.

Ingestion chat shows the same time and cost for each phase under that phase's
messages: Ingestor, Loader, Descriptives, Codebook tables and Codebook
reconciliation, per folder. A phase's timer runs until work is handed to the
next phase; Atlas publication is not counted. Stop and Retry ingestion share one
action row above the reply box.

### Analysis tables and visualizations

The analyst names which executed SQL or Python result each answer table shows; the app copies
its rows rather than having the model retype them. A Python run can return several results. The data columns those results read, with
their codebook descriptions, become the turn's selected variables.

Each table receives a `<chat_id>_<parent_id>_<source_turn_id>_<table_id>`
identifier. For example, the second table in a turn ends in `table_002`.
Its columns, rows, display precision and full ID live in the chat directory at
`tables/<source_turn_id>_table_002.json`. Regrouping the source turn preserves
the issued ID.

Clicking **Visualize** selects the table directly. For a typed request the
orchestrator resolves the table in the current analysis scope. Both paths give
the analyst the selected table and its source-turn summary. The analyst passes
on any data-specific guidance. The visualizer also receives the source turn's
current parent group for context. Each chat reuses one GPT-6.2 Sol visualizer
conversation at `high` reasoning effort, including for edits. The analyst's
handoff instructions are `VISUALIZATION_BRIEF` in [sub_agents/agent.py](analyse/1_main/sub_agents/agent.py).

The worker runs Python in the existing Docker sandbox with the selected table
mounted read-only. Headless Chrome renders each proposed view with the same
card component as chat. The worker receives a PNG at the requested card width,
sampled values and diagnostics for clipping and overlapping labels. It can
revise the Python and inspect the next render. Failed drafts enter the shared
glitch-agent error path. The output contract and design guidance are `VISUALIZER`
and its contracts in [sub_agents/agent.py](analyse/1_main/sub_agents/agent.py).

Publishing writes the Python, one to three chart views and the source table ID
to `visualizations/<publication_turn_id>.json`. An edit gets its own publication
turn. Switching views or palettes, hovering over a point and opening **View
associated table** use data already loaded in the browser.

## Logs and artifacts

Each project lives under `app/runtime/projects/<id>/`. Its `project.json` holds
current UI state, and `inputs.jsonl` records upload/context edits. Originals are
in `uploads/`. Each run has a timestamped directory containing `timeline.md`,
`timeline.jsonl`, full `events/` records, and loader work under `loader/folder_*/`.
Prepared tables, reusable loading code and manifests remain with that run. The loader
overwrites its current code/log and checkpoint; earlier runs are left in place. Analysis chats and their runs are stored under the same
project. SQL data and codebook assets live in the project's Atlas schema.

New runs retain earlier logs. Sandbox code has no network or credentials; input
files are mounted read-only. Loader execution retains the existing 1 GiB RAM,
one CPU and 240-second limits. This is a local single-user app. Restarting does not
automatically launch work. Retry preparation resumes the loader's saved workspace,
API response, code result or glitch diagnosis, then continues into review/codebooks.
PDF response/page/batch recovery remains available in the codebook phase.

Descriptives uses a stable `review_after_turn_<loader cycle>/` directory. Its single
checkpoint saves the current reply, pending input, execution result and glitch diagnosis
with their flags. API threads retain the conversation; review evidence stays under
`current/`, with prepared data mounted read-only. An interrupted review resumes there;
a loader correction starts a new review directory. A saved confirm/handoff/stop reply
is the completion marker, so a later failure does not repeat that decision or question.
Only the structured concern and evidence are handed back to the loader; no local raw
review transcript is created.

The Luna planning overview keeps its existing sample of the first 300 variables plus
up to 60 later variables. It saves `planning_overview.json` atomically only after all
samples and consolidation finish. That file is the completion marker: a failed attempt
restarts the entire overview on Retry preparation, while a completed overview is reused
if later finalization fails. There are no per-sample checkpoints or additional flags.

The loader keeps one `checkpoint.json`: current input/reply, execution result,
glitch thread and response, rollover summary, and the counters needed for budgeting.
No local raw conversation archive or per-cycle checkpoint history is required.
An execution error is saved with `code_ran=true` before glitch runs. A missing glitch
response retries diagnosis in its same thread; a saved response is reused. Rollover
preserves pending execution feedback and uses the normal input-delivery flag.
Code may repeat if it finished before its result was saved. Changed uploads/context
or a new material review invalidate the loader workspace pointer. Already-confirmed
legacy codebook workspaces still resume from their original saved context/report files.

`loader_prototype.ipynb` imports this production runner with toy data. The app-route
checks recreate Flask between attempts; `--docker` uses the real sandbox. Agents are
scripted in these checks, with no paid model calls or live SQL writes.

Non-PDF codebook readers now keep one current `checkpoint.json` per source: thread and
response IDs, current turn/input/reply, `input_sent`, `agent_returned`, `code_ran`, the
execution result, glitch thread/response, and `complete`. Token counters retain the
existing 250k rollover limit. Known responses are retrieved; unknown submissions and
missing glitch diagnoses retry in their saved threads with a short retry note.
CSV, notes and verification are rewritten together on a failed export, and `complete`
is saved last. Existing turn files remain readable by later generated code.

One project codebook flag, `merged_with_master`, covers source-column renaming, joining
the codebooks to the master variable list, and creating the unmatched-source table.
If False, the cheap assembly runs again and overwrites the same derived assets, reusing
completed source extractions. It becomes True only after all assembly files are saved.
This flag does not cover the later agent-driven final reconciliation or soft matching.

Final reconciliation and master-to-codebook soft matching share one overwritten
`reconciliation_checkpoint.json`. It replaces the two draft files for new runs and holds
accepted rows, taxonomy, the current phase/batch cursor and only the pending agent exchange.
That exchange has `input_sent` and `output_received`, its input/output, thread/response IDs
and the counters already needed for the budget and validation limit. Completed batches
discard their agent record when their rows and next cursor are saved together.

Each 50-variable batch starts a fresh thread. Battery lookahead is up to five requests
for ten extra rows in that same thread. Soft matching keeps search and decision in the
same thread, skips no-hit variables and saves returned candidate rows as the decision
input. Saved user-review answers are reused. Failed final exports overwrite the existing
CSV files from accepted checkpoint rows, without rerunning completed agents.

## Checks

Run these from the repository root using the same Python environment:

```text
python -B -m v_5.tests.phase_layout_check
python -B -m v_5.tests.codebooks_check
python -B -m v_5.tests.structured_resume_check
python -B -m v_5.tests.pdf_resume_check
python -B -m v_5.tests.prototype_checkpoint_check
python -B -m v_5.tests.final_codebook_check
python -B -m v_5.tests.reconciliation_resume_check
python -B -m v_5.tests.descriptives_check
python -B -m v_5.tests.descriptives_resume_check
python -B -m v_5.tests.descriptives_resume_check --app
python -B -m v_5.tests.descriptives_resume_check --docker
python -B -m v_5.tests.overview_resume_check
python -B -m v_5.tests.loader_checkpoint_check
python -B -m v_5.tests.loader_checkpoint_check --app
python -B -m v_5.tests.loader_checkpoint_check --app --docker
python -B -m v_5.tests.analysis_turns_check
python -B -m v_5.tests.variable_picker_check
python -B -m v_5.tests.visualization_check
python -B -m v_5.tests.ui_check
```

The checks above simulate model calls. The browser checks require Playwright
and local Google Chrome. `visualization_check` also runs the configured Docker
sandbox image. It exercises table selection, a typed revision on the same
visualizer, glitch recovery, screenshot width and the interactive card in a
temporary project. The `--docker` checks also require Docker. Manual-upload
fixtures are described in the [fixture README](tests/fixtures/README.md).
