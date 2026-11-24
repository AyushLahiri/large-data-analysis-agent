# v5 analysis search agent

The analysis backend owns one Sol search orchestrator for each live session, project and chat. The first search creates it. Subsequent searches in the same scope reuse its identity, recent answers and decision history. Sol can recall earlier completed answers from that chat. This package contains the service and its Python transport. The service is built on Jina AI's DeepResearch sub-agent (see the license note below).

## How analysis uses it

The analyst calls it through `analyse/1_main/sub_agents/web_search.py` with the `web_search` and `web_search_reply` actions, after reading the `search/search` skill. The app keeps one `SearchSession` per chat (`analyse/2_managers/routes.py`) and closes it when the chat is deleted.

- **One search per user turn.** A second `web_search` in the same turn is refused; a failed call still uses the turn's search.
- Up to five compound questions, the analyst's dataset context and up to five executed results (as text assets). Each request also carries the project data summary's paragraphs, marked as background that must not steer the search.
- Up to two clarifying questions go back to the analyst, which must reply before it can finish.
- The answer's sources are always listed under the chat answer. Chat shows only "Searching…" while it runs.
- Stop cancels a running search: the client call runs in a thread, and on Stop the chat's session is closed (the service cancels its work) and replaced on the next search.
- Each search result carries `usage`: billed Sol and Luna tokens per model (input, cached, output, calls), collected for that run in `model.ts` (`usageScope`). The app prices them, logs them with the time and adds them to the turn cost. Post-delivery curation is not included. Redeploy with `start.ps1` after updating.
- Each completed search is appended to the project's `summaries/summary.txt`, and every step is logged as an `analysis_web_search_*` event.

## Running locally

Start Docker and run `./start.ps1` in this directory. The service listens on loopback port 8767. The launcher reads the existing v5 OpenAI key and creates a private backend credential in the current user's local application data directory. A deployment can supply its own credential through `V5_ANALYSIS_SEARCH_TOKEN`. Health is available at `GET /health`.

## Backend contract

Create one `SearchSession` per live analysis session. Pass its project and chat IDs to each operation. Its default session ID is randomly generated. A backend with its own ephemeral session identity can supply it explicitly.

```python
from v_5.analyse.search_agent import SearchSession

session = SearchSession()
result = session.search(project_id, chat_id, question, assets=assets)
# When status is needs_input, analysis can supply context or requested assets.
while result["status"] == "needs_input":
    analysis_context = resolve_from_analysis(result["questions"])
    result = session.reply(project_id, chat_id, result["search_id"], answer=analysis_context)
# Invoke only after analysis has received the completed findings.
session.acknowledge_delivery(project_id, chat_id, result["search_id"])
# At analysis session teardown, cancel active work and release every chat agent in it.
session.close()
```

A context request is addressed exclusively to the analysis agent. At most two messages can be sent per search. Each search has a two-minute wall clock, including time spent waiting for a reply. At 90 seconds research ends and synthesis can use checkpointed evidence.

Request authentication uses a backend bearer credential plus `X-Session-Id`, `X-Project-Id`, and `X-Chat-Id`. The service binds searches, resumes, status reads, assets and delivery receipts to that scope. The Python transport implements the following operations.

| Method | Path | Operation |
|---|---|---|
| POST | /v1/search | Start from an analysis question and optional URL, text or PDF assets |
| POST | /v1/search/{id} | Reply with analysis context or assets, or retry an interrupted search |
| GET | /v1/search/{id} | Read current status and the scoped orchestrator identity |
| POST | /v1/search/{id}/delivered | Confirm delivery to analysis and curate reusable materials |
| DELETE | /v1/session | End the session across all its chats |
| GET | /v1/projects/{project}/references | Read the scoped project library |

## Models and state lifetime

Sol uses `gpt-6.2-sol` at high effort. Each generation has a 500,000-token input-plus-output allowance spanning searches and post-delivery curation. The runtime counts input and reserves output before each model call. If a call would exceed the allowance, a fresh generation takes over with a new agent ID, compact handoff, access to this chat's prior answers, and the current search's assignments, passages, remaining time and worker ledger. Calls with unknown usage retain their reservation.

Luna workers use `gpt-6-luna` at xhigh effort. The first web wave has up to three workers. Later waves use at most twenty concurrent workers total, including up to three readers of saved project material. A search has a shared Luna budget of 250,000 tokens, with one optional 100,000-token replenishment. Web discovery is billed separately.

Chat memory and run checkpoints are held in the service process. Temporary source files live in the container's tmpfs. Closing a session discards both. Restarting the service starts fresh sessions. Only the curated project library persists in the Docker volume. The delivery receipt triggers selection of materials, 70–100 word summaries, source and publication-year metadata, and retention of useful documents.

The search implementation incorporates code from Jina AI's Apache-2.0-licensed DeepResearch project. See [LICENSE](LICENSE).

