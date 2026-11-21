import json
from pathlib import Path
import pandas as pd
from v_5.ingest.codebook_ingestion.sub_agents.pdf import pdf_codebook
from v_5.ingest.codebook_ingestion.sub_agents.structured import structured_codebook
from v_5.ingest.codebook_ingestion.tables import create_master, finish_codebooks, save_source_table, save_csv
from v_5.ingest.reuse import file_digest, restore_codebook, selected_codebooks


def ingest_codebooks(session, context, prepared, files, workspace, environment):
    """Extract source codebooks and align them with prepared variables.

    When: Phase 4, after descriptives confirmation; coordinates PDF and structured codebook
        subagents.
    Inputs: session: services; context: folder decisions; prepared: loader report; files:
        uploads; workspace: output root; environment: installed packages.
    Returns: Book dict containing counts, combined/unmatched tables and source mapping for
        finalize_codebook.
    Flow: Builds the variable roster, selects codebook sources, dispatches by format,
        records source precedence and joins the saved tables through finish_codebooks.
        PDF and structured checkpoints resume in this same workspace. One project flag
        covers source-column renaming, the master merge and unmatched output; an unfinished
        assembly rewrites its cheap assets, reusing each source's saved extraction.
    """
    directory = workspace / 'codebooks'
    directory.mkdir(exist_ok=True)
    primary = context.get('ingestion_notes', {}).get('primary_codebook', '')
    selected = selected_codebooks(files, primary,
        {key: value.get('finding', {}) for key, value in context['files'].items()})
    books = session.state.setdefault('codebooks', {})
    previous = books.get(context['folder'], {})
    if (previous.get('merged_with_master')
            and Path(previous['combined']).parent == directory
            and [source['file_id'] for source in previous['source_tables'].values()] == [file['file_id'] for file in selected]
            and all(Path(previous[key]).is_file() for key in ('master', 'combined', 'unmatched', 'source_mapping'))
            and all(Path(source['table']).is_file() for source in previous['source_tables'].values())):
        return previous
    session.begin_phase('Codebook tables', context['folder'])
    books[context['folder']] = {**previous, 'merged_with_master': False}
    session.record('codebook_assembly_started', 'codebook_agent', {'folder': context['folder']})
    master = create_master(workspace, prepared)
    save_csv(directory / 'data_variables.csv', master)
    sources, mapping = [], {}
    session.message('codebook_agent', 'The data review is accepted. I’m now organizing each codebook separately, preserving its definitions and source references.')
    for file in selected:
        source_dir = directory / file['file_id']
        source_dir.mkdir(exist_ok=True)
        if file.get('reuse_codebook') and (file.get('asset_kind') == 'codebook' or file.get('embedded_for')):
            frame = restore_codebook(session, file, source_dir)
        elif file['format'] == 'pdf':
            frame = pdf_codebook(session, file, source_dir)
        elif not (source_dir / 'checkpoint.json').exists() and all(
                (source_dir / name).is_file() for name in ('normalized.csv', 'general_information.md', 'verification.txt')):
            # Only pre-checkpoint completed runs use this shortcut. A partial new export
            # must go through its saved flags even when some files already exist.
            from v_5.ingest.codebook_ingestion.tables import normalize_rows
            frame = normalize_rows(pd.read_csv(source_dir / 'normalized.csv', dtype=str,
                                               keep_default_na=False).to_dict('records'))
        else:
            frame = structured_codebook(session, file, files, context, prepared, workspace, source_dir, environment)
        prefix = save_source_table(directory, source_dir, file['filename'], frame, mapping)
        mapping[prefix]['embedded_for'] = file.get('embedded_for')
        mapping[prefix]['primary'] = bool(not file.get('embedded_for') and file['filename'] == primary)
        mapping[prefix]['file_id'] = file['file_id']
        mapping[prefix]['sha256'] = file_digest(file)
        sources.append((prefix, file['filename'], frame))
    return finish_codebooks(session, context['folder'], directory, master, sources, mapping)


def complete_codebooks(session, context, prepared, files, workspace, environment, next_folder):
    """Finish codebooks and publish a folder whose data review was already confirmed.

    Inputs: the existing folder context, loader report, sources, workspace and packages;
        next_folder: publication's continuation label. Returns: None.
    The project stores only the workspace pointer needed by Prepare data after failure.
    Context and the loader report live in its completed checkpoint (legacy runs use
    their original context/report files); no second copy is created.
    """
    from v_5.ingest.codebook_ingestion.finalize import finalize_codebook
    from v_5.ingest.publishing import publish_atlas
    session.state['codebook_resume'] = {'folder': context['folder'], 'workspace': str(workspace)}
    session.state.pop('loader_resume', None)
    session.state['activity'] = 'Organizing codebooks'
    session.save()
    book = ingest_codebooks(session, context, prepared, files, workspace, environment)
    finalize_codebook(session, context, prepared, workspace, book)
    session.close_phase()  # Atlas publication is not part of the codebook phases.
    # Keep the original release when a new app run resumes this saved workspace.
    publish_atlas(session, {'release': workspace.parent.parent.name, 'next_folder': next_folder,
        'datasets': [{'folder': context['folder'], 'workspace': str(workspace),
                      'prepared': prepared, 'book': book}]})
    session.state.pop('codebook_resume', None)
    session.save()


def saved_codebook_inputs(session, folder):
    """Load the confirmed folder's existing workspace when preparation is retried.

    Inputs: session project state and folder key. Returns: (workspace, context, prepared)
        or None for a new folder. Paths must remain inside this project's saved runs.
    Missing saved inputs raise visibly instead of silently restarting data preparation.
    """
    saved = session.state.get('codebook_resume')
    if not saved or saved['folder'] != folder:
        return None
    workspace = Path(saved['workspace']).resolve()
    if not workspace.is_relative_to((session.path / 'runs').resolve()):
        raise ValueError('Saved codebook workspace is outside this project.')
    if (workspace / 'reused_inputs.json').is_file():
        reused = json.loads((workspace / 'reused_inputs.json').read_text(encoding='utf-8'))
        context, prepared = reused['context'], reused['prepared']
    elif (workspace / 'checkpoint.json').is_file():
        saved_loader = json.loads((workspace / 'checkpoint.json').read_text(encoding='utf-8'))
        if saved_loader['phase'] != 'complete':
            raise ValueError('Saved loader outputs have not been confirmed.')
        context, prepared = saved_loader['context'], saved_loader['loader']['reply']
    else:
        # Existing confirmed runs predate the loader checkpoint and retain these two files.
        context = json.loads((workspace / 'context.json').read_text(encoding='utf-8'))
        prepared = json.loads((workspace / 'latest_payload.json').read_text(encoding='utf-8'))
    if context['folder'] != folder:
        raise ValueError('Saved codebook workspace belongs to a different folder.')
    return workspace, context, prepared
