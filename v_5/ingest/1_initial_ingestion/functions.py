import json
from .sub_agents.files import create_file_reviewers
from collections import defaultdict
from v_5.ingest.initial_ingestion.agent import INGESTION_PROMPT, IngestionReply
from v_5.ingest.initial_ingestion.sub_agents.agent import CONTEXT_CHECKER_PROMPT, ContextReply
from v_5.ingest.summary import summary_ready, write_final_summary
from v_5.ingest.reuse import reused_materials
from v_5.main_functions.sql import published_folders


def review_materials(session):
    """Resolve file roles and context before data preparation.

    When: Phase 1: ingestion_agent coordinates file reviewers and context_checker.
    Inputs: session: uploaded files/folders, saved state and agent/user interaction methods.
    Returns: A context dict with notes, files, findings, previews, clarifications and
        decision; or complete/stopped for an early exit.
    Flow: Reviews all unpublished folders when no upload folder is active, substitutes saved findings for approved exact copies,
        and preserves their settled placement. The main ingester receives reused ingestion
        and descriptive notes; a saved handoff skips review on preparation retry.
    """
    session.begin_phase('Ingestor')
    project = session.state['context']
    folders = session.state['folders']
    files = session.state['files']
    ask_agent = session.ask_agent
    call_loader = session.call_loader
    count_input = session.count_input
    show = session.show
    input = session.answer
    print = session.print
    run_code = session.run_code
    if session.handoff is None:
        locked = published_folders(session.state)
        active_folder = session.state.get('active_folder')
        if active_folder:
            locked |= {name for name in folders if name != active_folder}
        prior_files = {key: file for key, file in files.items() if file['folder'] in locked}
        files = {key: file for key, file in files.items() if key not in prior_files}
        if not files:
            return write_final_summary(session) if summary_ready(session.state) else 'complete'
        user_clarifications = []

        protected, reused_findings, folder_notes, reuse_summaries = reused_materials(session, files)
        project_notes = ""
        review_files = {key: file for key, file in files.items() if key not in protected}
        inspect_files, follow_up, previews, findings = create_file_reviewers(
            session, project, folders, review_files, folder_notes, user_clarifications)
        findings.update(reused_findings)
        session.message('ingestion_agent', 'I’m reviewing the uploaded files before asking about anything unclear.', 'thinking')
        inspect_files(review_files)

        checker_history = [{"role": "user", "content": json.dumps({"project": project, "folders": folders,
            "files": list(files.values()), "findings": findings})}]
        questions_used = 0
        while review_files and questions_used < 2:
            checker_history.append({"role": "user", "content": f"Remaining question allowance: {2 - questions_used}. Accept usable context without further questions."})
            answer = ask_agent("context_checker", CONTEXT_CHECKER_PROMPT, checker_history, ContextReply)
            if answer["accepted"] or not answer["questions"]:
                break
            response = input("Your clarification: ")
            user_clarifications.append(response)
            questions_used += len(answer["questions"])
            checker_history.append({"role": "user", "content": response})

        history = [{"role": "user", "content": json.dumps({"project": project, "folders": folders,
            "files": list(files.values()), "user_clarifications": user_clarifications,
            "file_findings": findings, "published_folders": sorted(locked),
            "reused_folders": reuse_summaries, "reused_file_ids": sorted(protected),
            "saved_folder_notes": folder_notes,
            "task": "Reconcile only the supplied files. Published folders are read-only context; do not assign files to them. "
                    "User-approved reused_file_ids already have confirmed findings. Do not inspect, follow up, move, "
                    "ignore or retag those files. Explain that their saved ingestion is reused and surface the supplied "
                    "descriptive notes. Review any new codebooks normally and retain the source primary choice unless "
                    "the new inputs require a different choice."})}]

        while True:
            decision = ask_agent("ingestion_agent", INGESTION_PROMPT, history, IngestionReply)
            project_notes = decision["project_notes"]
            for note in decision["folder_notes"]:
                if note['folder'] not in locked:
                    folder_notes[note["folder"]] = note
            for update in decision["file_updates"]:
                file_id = update["file_id"]
                if file_id not in files or file_id in protected or update['folder'] in locked:
                    continue
                original_folder = files[file_id]['folder']
                fixed_folder = active_folder or (original_folder if folders.get(original_folder, {}).get('setup_reviewed') else None)
                if fixed_folder and update['folder'] != fixed_folder:
                    if not update['ignore']:
                        raise ValueError('A file appears to belong in a different folder. Restart this folder’s setup and separate the datasets before continuing.')
                    update = {**update, 'folder': fixed_folder}
                files[file_id].update({key: update[key] for key in ["folder", "role", "ignore"]})
                files[file_id]['placement_note'] = update['reason']
                folders.setdefault(update['folder'], {'description': ''})
                findings.setdefault(file_id, {}).update({key: update[key] for key in ["classification", "summary"]})
            for file in files.values():
                source = files.get(file.get('embedded_for'))
                if source:
                    file['folder'] = source['folder']
                    if source.get('ignore'):
                        file['ignore'] = True
            for name, note in folder_notes.items():
                folders.setdefault(name, {'description': ''})['summary'] = note['summary']
            for name, folder in folders.items():
                if name.casefold() == 'other' and name not in locked:
                    reasons = [f.get('placement_note', '') for f in files.values() if f['folder'] == name]
                    explanation = folder.get('summary') or ' '.join(dict.fromkeys(filter(None, reasons)))
                    folder['description'] = 'Materials whose intended placement is unresolved. ' + explanation
                    folder['relationship'] = 'No continuous dataset relationship has been established. ' + explanation
                    folder['summary'] = explanation
            session.save()
            action = decision["action"]

            if action == "inspect":
                inspect_files([key for key in decision['file_ids'] if key in review_files])

            elif action == "follow_up":
                follow_up({**decision, 'file_ids': [key for key in decision['file_ids'] if key in review_files]})

            elif action == "clarify":
                response = input("Your reply: ")
                user_clarifications.append(response)
                history.append({"role": "user", "content": response})

            elif action in {"finish", "stop"}:
                materials = {file_id: {"metadata": files[file_id], "finding": finding} for file_id, finding in findings.items()}
                show("Ingestion " + action, {"project_notes": project_notes, "folder_notes": folder_notes, "materials": materials})
                break

            # Conditional context after the action: findings by folder and the saved decisions.
            reports = defaultdict(list)
            for file_id, finding in findings.items():
                reports[files[file_id]["folder"]].append({"file": files[file_id], "finding": finding})
            history.append({"role": "user", "content": json.dumps({"folder_reports": reports,
                "saved_folder_notes": folder_notes, "saved_project_notes": project_notes}, default=str)})

        if decision['action'] == 'stop':
            return 'stopped'
        # Restore protected published files before saving the full handoff; only new materials were reviewed.
        files = {**prior_files, **files}
        folder_notes = {**{name: note for name, note in session.state.get('folder_notes', {}).items() if name in locked}, **folder_notes}
        findings = {**{key: value for key, value in session.state.get('findings', {}).items() if key in prior_files}, **findings}
        session.save_handoff(project_notes, folder_notes, files, findings, previews, user_clarifications, protected_folders=locked)
        session.answer('Material review is complete. Select Prepare data to continue.', ready=True)
    else:
        handoff = session.handoff
        project_notes, folder_notes = handoff['project_notes'], handoff['folder_notes']
        files, findings = handoff['files'], handoff['findings']
        previews = handoff['previews']
        user_clarifications = handoff['user_clarifications']
        decision = {'action': 'finish'}
        session.record('preparation_retry', 'user', {'handoff': session.state['handoff_path']})
    return dict(project_notes=project_notes, folder_notes=folder_notes, files=files, findings=findings,
                previews=previews, user_clarifications=user_clarifications, decision=decision)

