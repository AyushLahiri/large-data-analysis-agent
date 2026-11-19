import json
from v_5.app.config import LOADER_SUMMARIZER_MODEL
from v_5.ingest.data_loader.sub_agents.agent import LOADER_SUMMARIZER_PROMPT
from v_5.main_functions.session import loader_request


def summarize_loader_history(session, loader_history, latest, workspace, session_number, *, save_chunks=True):
    """Produce a continuation note before the loader starts a new conversation.

    When: Phase 2 subagent loader_summarizer_agent, when the next loader call would exceed
        its budget.
    Inputs: session: API/logger; loader_history: messages; latest: loader payload;
        workspace: saved work root; session_number: handoff file label; save_chunks:
        legacy per-chunk logs, disabled by the checkpointed loader.
    Returns: Final summary text; the loader saves it with its current recovery state.
    Flow: Processes roughly 60,000-character history chunks in order, carrying the previous
        summary forward. Includes latest work context and rejects incomplete summaries.
    """
    call_loader = session.call_loader
    summary, chunk, chunk_chars = "", [], 0
    chunks = []
    for message in loader_history:
        chunk.append(message)
        chunk_chars += len(message["content"])
        if chunk_chars >= 60_000:
            chunks.append(chunk)
            chunk, chunk_chars = [], 0
    if chunk:
        chunks.append(chunk)
    # Each chunk receives the preceding summary, so the last result covers the whole conversation.
    for index, chunk in enumerate(chunks, 1):
        summary_request = loader_request(LOADER_SUMMARIZER_PROMPT, [{"role": "user", "content": json.dumps({
            "earlier_handoff": summary, "subsequent_turns": chunk,
            "latest_payload": latest, "saved_work": "/work"})}])
        summary_request.update(model=LOADER_SUMMARIZER_MODEL, reasoning={"effort": "high"})
        summary, usage, status = call_loader("loader_summarizer_agent", summary_request,
                                             allow_user_questions=False)
        if save_chunks:
            (workspace / f"handoff_{session_number}_{index}.json").write_text(json.dumps({
                "summary": summary, "usage": usage, "status": status}, indent=2), encoding="utf-8")
        if status != "completed":
            raise RuntimeError("Handoff was incomplete; saved work is retained. Do not continue from a truncated handoff.")
    return summary
