import json
from .checkpoint import LoaderRunner
from .calls import LoaderCalls
from .state import loader_workspace
from v_5.app.docker import run_code as execute_code
from pathlib import Path
from textwrap import dedent
from v_5.ingest.codebook_ingestion.functions import complete_codebooks, saved_codebook_inputs
from v_5.ingest.data_loader.agent import LOADER_PROMPT
from v_5.ingest.descriptives_check.functions import review_prepared_data
from v_5.ingest.manifest import validate_prepared_manifest
from v_5.ingest.reuse import copy_prepared_inputs, source_for_folder, whole_folder_copy
from v_5.ingest.summary import summary_ready, write_final_summary
from v_5.main_functions.sql import published_folders


def prepare_data(session, project_notes, folder_notes, files, findings, previews, user_clarifications, decision):
    """Prepare, review and publish each data folder in order.

    When: Phase 2: data_loader_agent receives phase 1's saved review context.
    Inputs: session: runtime services; project_notes/folder_notes: decisions; files:
        metadata; findings/previews: evidence; user_clarifications: answers; decision:
        ingestion outcome.
    Returns: complete or stopped; saves a current loader checkpoint, review/codebook outputs and
        publication state.
    Flow: Prepares all unpublished folders when no upload folder is active. Checks Docker,
        builds folder context, budgets calls and invokes the rollover
        summarizer when needed. Confirmed outputs pass through phases 3 and 4, then
        publish_atlas; corrections return to this loader conversation. A failed codebook
        phase reuses its confirmed workspace and skips loading and review on retry. Loader
        failures resume the current API reply, execution or glitch diagnosis in that workspace.
    """
    project = session.state['context']
    folders = session.state['folders']
    show = session.show
    run_code = session.run_code
    loader_root = session.run_path / "loader"
    loader_root.mkdir(parents=True, exist_ok=True)

    environment_code = dedent("""
    import sys, json, importlib.metadata
    packages = ['pandas', 'pyarrow', 'pyreadstat', 'pypdf', 'markitdown', 'openpyxl', 'xlrd']
    print(json.dumps({'python': sys.version, 'packages': {
        name: importlib.metadata.version(name) for name in packages}}))
    """)
    environment = None

    def ensure_environment():
        """Check Docker once, only when this run actually needs preparation/codebook code."""
        nonlocal environment
        if environment is None:
            environment = run_code(environment_code, {}, loader_root, loader_root / "environment")
            show("Docker environment", environment)
            if environment.get("exit_code") != 0:
                detail = environment.get("glitch_report", {}).get("actual_error") or environment.get("output") or environment.get("error")
                raise RuntimeError(f"Environment check failed: {detail}")
        return environment

    EXISTING_TABLES = ["survey", "survey_part1", "respondents"]  # Prototype schema inventory.

    if decision["action"] != "finish":
        raise RuntimeError("Finish the ingestion conversation before loading its folders.")

    published = published_folders(session.state)
    if session.state.get('codebook_resume', {}).get('folder') in published:
        session.state.pop('codebook_resume', None)
        session.save()
    if session.state.get('loader_resume', {}).get('folder') in published:
        session.state.pop('loader_resume', None)
        session.save()
    folder_names = list(dict.fromkeys(file['folder'] for file in files.values()
                        if not file.get('ignore') and file['role'] in {'primary', 'continuation'}
                        and file['folder'] not in published
                        and (not session.state.get('active_folder') or file['folder'] == session.state['active_folder'])))

    all_copied = bool(folder_names)
    for folder_number, folder in enumerate(folder_names, 1):
        folder_files = {key: file for key, file in files.items()
                        if file["folder"] == folder and not file.get("ignore")}
        data_ids = [key for key, file in folder_files.items() if file["role"] in {"primary", "continuation"}]
        next_folder = folder_names[folder_number] if folder_number < len(folder_names) else None
        reuse_source = source_for_folder(session, folder)
        if reuse_source and whole_folder_copy(session, folder, folder_files, folder_notes.get(folder, {}), findings):
            from v_5.ingest.publishing import publish_reused_folder
            choice = reuse_source[2]
            publish_reused_folder(session, folder, choice['project_id'], choice['folder'], next_folder=next_folder)
            continue
        all_copied = False
        ensure_environment()
        # The environment is ordinary system context for the existing loader prompt.
        loader_prompt = LOADER_PROMPT + "\nInstalled Docker environment:\n" + environment["output"]
        saved = saved_codebook_inputs(session, folder)
        if saved is not None:
            workspace, context, latest = saved
            validate_prepared_manifest(workspace, latest['manifest_path'])
            session.message('codebook_agent', 'Continuing codebook ingestion from the saved prepared data.')
            complete_codebooks(session, context, latest, folder_files, workspace,
                               environment['output'], next_folder)
            continue
        workspace = loader_root / f"folder_{folder_number:03d}"
        if reuse_source:
            workspace.mkdir(parents=True, exist_ok=True)
        else:
            workspace = loader_workspace(session, folder, workspace)

        def make_context():
            """Build initial folder evidence only until its completed payload is saved.

            Details: Each file's evidence is the preview its ingestion reviewer saw; the loader
                writes its own code for any further reading.
            """
            extracts = {}
            for file_id, file in folder_files.items():
                if (reuse_source and file_id in data_ids) or file.get('reuse_codebook'):
                    extracts[file_id] = {'reused': True, 'instruction': 'Use the selected saved data/codebook outputs; this source is not being reingested.'}
                    continue
                extracts[file_id] = previews.get(file_id) or {
                    'instruction': 'No ingestion preview was saved for this file. Inspect it with code.'}
            ingestion_notes = folder_notes.get(folder, {})
            primary_name = ingestion_notes.get('primary_codebook')
            external = next((key for key, file in folder_files.items()
                             if file['role'] == 'asset' and not file.get('embedded_for')
                             and file['filename'] == primary_name), None)
            embedded = {file['embedded_for']: key for key, file in folder_files.items() if file.get('embedded_for')}
            context = {
                "initial_file_context": extracts, "existing_tables": EXISTING_TABLES,
                "sql_type_mapping": {"character": "TEXT", "int": "BIGINT", "numeric": "NUMERIC"},
                "project": project, "project_notes": project_notes,
                "folder": folder, "folder_description": folders.get(folder, {}),
                "ingestion_notes": ingestion_notes,
                "codebook_references": {key: {'embedded_file_id': embedded.get(key), 'primary_external_file_id': external}
                                        for key in data_ids},
                "user_clarifications": list(user_clarifications), "data_file_ids": data_ids,
                "files": {key: {**file, "path": f"/input/{key}{Path(file['path']).suffix}",
                                "finding": findings.get(key, {})} for key, file in folder_files.items()},
            }
            # Preserve the previous loader's treatment of timestamps and metadata scalars.
            return json.loads(json.dumps(context, default=str))

        if reuse_source:
            context, prepared = copy_prepared_inputs(session, folder, workspace, make_context())
            session.message('data_loader_agent', 'Reusing the confirmed prepared data and descriptives. Continuing with this folder’s codebooks.')
            complete_codebooks(session, context, prepared, folder_files, workspace,
                               environment['output'], next_folder)
            continue

        def execute(code, work, current):
            """Run raw Docker execution; LoaderRunner saves its result before glitch diagnosis."""
            session.check_stop()
            return execute_code(code, folder_files, work, current, cancelled=session.cancelled)

        def observe(event, state):
            """Restore the existing progress, phase reports, error display and question forms."""
            loader = state['loader']
            if event == 'reply':
                session.progress(folder, loader['session_number'], loader['used_tokens'])
                session.phase_report(folder, loader['reply'])
                session.present_reply('data_loader_agent', json.dumps(loader['reply']),
                                      allow_user_questions=loader['attempted_investigation'])
            elif event == 'execution':
                result = loader['result']
                session.message('execution', 'Code finished successfully.' if result.get('exit_code') == 0
                                else 'The code encountered an error.', 'thinking',
                                {'code': loader['reply']['code'], 'result': result})
            elif event == 'glitch':
                session.present_reply('glitch_agent', json.dumps(loader['glitch']['response']))
            elif event == 'manifest_error':
                session.phase_report(folder, loader['reply'])
                session.record('manifest_validation_failed', 'data_loader_agent', {
                    'folder': folder, 'error': loader['reply']['phases']['loading_artefacts']['details']})

        def review(prepared):
            """Resume this loader iteration's review; corrections start a new review iteration."""
            context = runner.state['context']
            review_dir = workspace / f"review_after_turn_{runner.state['loader']['cycle']:04d}"
            return review_prepared_data(session, context, prepared, folder_files, workspace,
                                        review_dir, environment['output'])

        runner = LoaderRunner(workspace, LoaderCalls(session), execute, make_context, review,
                              session.answer, prompt=loader_prompt, observer=observe,
                              check_stop=session.check_stop)
        completed = runner.run()
        if completed['phase'] == 'stopped':
            return 'blocked'
        context, latest = completed['context'], completed['loader']['reply']
        validate_prepared_manifest(workspace, latest['manifest_path'])
        complete_codebooks(session, context, latest, folder_files, workspace,
                           environment['output'], next_folder)

    if all_copied:
        return 'complete'
    if summary_ready(session.state):
        return write_final_summary(session)
    return 'complete'
