import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from v_5.ingest.initial_ingestion.sub_agents.agent import (
    DATA_AGENT_PROMPT, DELIMITED_PROMPT, FILE_AGENT_PROMPT, FileReply, LAYOUT_PROMPT, MAP_PROMPT,
    PDF_PROMPT, TABLE_PROMPT, TEXT_PROMPT,
)
from v_5.ingest.readers import read_delimited, read_layout, read_pdf, read_table, read_text


def create_file_reviewers(session, project, folders, files, folder_notes, user_clarifications):
    """Create the phase-1 file-review conversations and their shared evidence.

    When: Initial ingestion, before context_checker and ingestion_agent reconcile the
        collection.
    Inputs: session: model/logging methods; project/folders/files: upload context;
        folder_notes/user_clarifications: live decisions and answers.
    Returns: (inspect_files, follow_up, previews, findings): two callbacks and two
        dictionaries they fill by file ID. Findings include parser_rules; the loader later
        receives both dictionaries unchanged.
    Flow: Groups primary/continuation files by their assigned agent, gives supporting assets
        their own conversation, and runs independent groups in parallel. CSV/TSV files are
        read as raw lines; Excel previews its first 6 sheets.
    """
    ask_agent, show = session.ask_agent, session.show
    format_prompts = {"pdf": PDF_PROMPT, "txt": TEXT_PROMPT, "md": TEXT_PROMPT, "map": MAP_PROMPT,
        "dcf": LAYOUT_PROMPT, "dct": LAYOUT_PROMPT, "dat": TEXT_PROMPT,
        "csv": DELIMITED_PROMPT, "tsv": DELIMITED_PROMPT, "excel": TABLE_PROMPT,
        "spss": TABLE_PROMPT, "dta": TABLE_PROMPT}
    readers = {"pdf": read_pdf, "txt": read_text, "md": read_text, "map": read_text,
        "dat": read_text, "dcf": read_layout, "dct": read_layout,
        "csv": read_delimited, "tsv": read_delimited, "excel": read_table,
        "spss": read_table, "dta": read_table}
    # Explicit parameters make every executed read easy to inspect.
    read_parameters = {
        "read_text": {"first": 30, "random_count": 30, "encoding": "utf-8-sig"},
        "read_layout": {"first": 30, "random_count": 30, "encoding": "utf-8-sig"},
        "read_delimited": {"first": 10, "random_count": 10, "max_chars": 2000, "encoding": "utf-8-sig"},
        "read_pdf": {"first": 3, "random_count": 5},
        "read_table": {"first": 10, "random_count": 10, "rows": 5, "sheets": 6},
    }
    threads = defaultdict(list)
    previews, readings, findings = {}, {}, {}
    agent_prompts = {file["agent"]: FILE_AGENT_PROMPT + (
        "\n" + DATA_AGENT_PROMPT if file["role"] in {"primary", "continuation"} else "")
        for file in files.values()}


    def file_context(file):
        """Build current context for one file reviewer.

        Inputs: file: metadata record; captures project, folders, notes and clarification lists.
        Returns: Dict of project, folder and file context plus format guidance and user
            clarifications.
        Details: Phase 1 file subagents use this for initial reads and follow-ups, so later
            decisions are included rather than frozen at first inspection.
        """
        # Used for initial inspection and follow-ups, with current folder decisions.
        return {
            "project_context": project,
            "folder_context": {"name": file["folder"],
                "user_description": folders.get(file["folder"], {}).get("description", ""),
                "ingestion_notes": folder_notes.get(file["folder"], {}),
                "dataset_relationship": folders.get(file["folder"], {}).get("relationship", ""),
                "display_name": folders.get(file["folder"], {}).get("display_name", file["folder"])},
            "file_context": {"user_description": file["note"], "metadata": file},
            "user_clarifications": user_clarifications,
            "format_guidance_for_this_file": format_prompts[file["format"]],
        }


    def inspect_group(agent, file_ids):
        """Read and interpret files belonging to one reviewer conversation.

        When: Phase 1 file-review subagent, scheduled by inspect_files.
        Inputs: agent: assigned conversation key; file_ids: ordered files for that agent.
        Returns: List of (file_id, finding) pairs; also fills previews/readings and extends the
            shared conversation.
        Flow: Selects each format reader, records its arguments/results, then asks FileReply for
            one finding. Files within the group run in order.
        """
        results = []
        for file_id in file_ids:
            file = files[file_id]
            label = f"{agent} - {file['filename']} ({file_id})"
            reader = readers[file["format"]]
            parameters = dict(read_parameters[reader.__name__])
            preview = reader(file["path"], **parameters)
            previews[file_id] = preview
            readings[file_id] = {
                "executed_function": reader.__name__,
                "arguments": {"path": file["path"], **parameters},
                "results": preview,
            }
            show(label + " - automatic read", readings[file_id])
            threads[agent].append({"role": "user", "content": json.dumps({
                "instructions": "Interpret these results once and return this file's finding. No tools or code execution are available.",
                "context": file_context(file),
                "current_file_id": file_id,
                "executed_read": {k: readings[file_id][k] for k in ["executed_function", "arguments"]},
                "reading_results": preview,
            }, default=str)})
            reply = ask_agent(label + " - finding", agent_prompts[agent], threads[agent], FileReply)
            results.append((file_id, reply))
        return results

    def inspect_files(file_ids):
        """Run independent file reviewers in parallel.

        Inputs: file_ids: iterable of known upload IDs; captures files and reviewer state.
        Returns: None; updates findings by file ID and populates previews.
        Details: Phase 1 groups files by agent and schedules at most four groups at once;
            inspect_group keeps related files in one conversation.
        """
        groups = defaultdict(list)
        for file_id in file_ids:
            groups[files[file_id]['agent']].append(file_id)
        # Parallelize between conversations only; files sharing an agent must keep their message order.
        with ThreadPoolExecutor(max_workers=4) as workers:
            tasks = [workers.submit(inspect_group, agent, ids) for agent, ids in groups.items()]
            for task in as_completed(tasks):
                findings.update(task.result())

    def follow_up(decision):
        """Ask a file reviewer a targeted question using existing evidence.

        When: Phase 1: ingestion_agent returns action=follow_up.
        Inputs: decision: file_ids and question; captures current metadata, notes, previews and
            conversations.
        Returns: None; replaces each targeted file's finding and extends its conversation.
        Flow: Builds fresh file_context but reuses the saved read result, so follow-up does not
            silently reread or reinterpret another file.
        """
        for file_id in decision["file_ids"]:
            if file_id not in files:
                continue
            file = files[file_id]
            agent = file["agent"]
            threads[agent].append({"role": "user", "content": json.dumps({
                **file_context(file), "target_file_id": file_id,
                "question_from_ingestion_agent": decision["question"],
                "executed_read": {k: readings[file_id][k] for k in ["executed_function", "arguments"]},
                "reading_results": previews[file_id],
                "instruction": "Answer for this target file from existing evidence; return its updated finding."})})
            findings[file_id] = ask_agent(f"{agent} — {file['filename']} (follow-up)",
                agent_prompts[agent], threads[agent], FileReply)

    return inspect_files, follow_up, previews, findings
