"""Reuse explicitly selected, byte-identical inputs without repeating their agents."""
from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import save_json
from v_5.ingest.manifest import validate_prepared_manifest, work_path
from v_5.main_functions.assets import source_project
from v_5.main_functions.sql import published_folders


def file_digest(file):
    """Return the exact-byte fingerprint, reading legacy uploads only when needed."""
    if file.get('sha256'):
        return file['sha256']
    with Path(file['path']).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def selected_codebooks(files, primary='', findings=None):
    """Select the same source files as codebook ingestion, including embedded dictionaries."""
    findings = findings or {}
    return [file for file in files.values() if not file.get('ignore') and file.get('asset_kind') != 'report' and (
        file.get('asset_kind') == 'codebook' or file.get('embedded_for') or
        findings.get(file['file_id'], {}).get('classification') == 'data book' or
        file['filename'] == primary)]


def source_for_folder(session, folder):
    """Resolve a confirmed reuse choice to this user's still-published source folder.

    Inputs: destination session and folder key. Returns: (source path, state, choice),
        or None when this folder is being ingested independently.
    The persisted choice is a source reference, never a local path from the browser.
    """
    choice = session.state['folders'].get(folder, {}).get('reuse_dataset')
    if not choice:
        return None
    path, state = source_project(session.path, choice['project_id'],
                                 user_id=session.state.get('user_id', '9999'))
    if choice['folder'] not in published_folders(state):
        raise ValueError('The selected source folder is no longer published. Choose a source again.')
    return path, state, choice


def reused_materials(session, files):
    """Seed phase 1 with saved findings and notes for user-approved copies.

    Returns: (protected file IDs, findings, folder notes, reuse summaries). Data files
        and reused codebooks skip file readers. Other assets keep their ordinary review.
    Saved descriptive results are shown directly and also sent to the main ingester.
    """
    protected, findings, notes, summaries = set(), {}, {}, {}
    for folder in dict.fromkeys(file['folder'] for file in files.values()):
        resolved = source_for_folder(session, folder)
        if resolved is None:
            continue
        _, source, choice = resolved
        original = choice['folder']
        mapping = choice['files']
        target_data = {key for key, file in files.items() if file['folder'] == folder
                       and file['role'] in {'primary', 'continuation'} and not file.get('ignore')}
        if target_data != set(mapping):
            raise ValueError('The selected copied dataset changed. Review its files again.')
        for target_id, source_id in mapping.items():
            file, old = files[target_id], source['files'][source_id]
            if file_digest(file) != file_digest(old):
                raise ValueError('A copied data file changed. Review its reuse choice again.')
            protected.add(target_id)
            findings[target_id] = deepcopy(source.get('findings', {}).get(source_id, {
                'classification': 'data', 'summary': 'Reused confirmed data from the selected folder.'}))
        # Embedded dictionaries belong to their data file; reuse only exact extracted bytes.
        for file in files.values():
            if file['folder'] != folder or file.get('embedded_for') not in mapping:
                continue
            old = next((candidate for candidate in source['files'].values()
                        if candidate.get('embedded_for') == mapping[file['embedded_for']]
                        and file_digest(candidate) == file_digest(file)), None)
            if old and any(item.get('file_id') == old['file_id'] for item in
                           source.get('atlas', {}).get('ingested_codebooks', [])):
                file['reuse_codebook'] = {'project_id': choice['project_id'],
                                         'folder': original, 'file_id': old['file_id']}
        source_name = source['folders'].get(original, {}).get('display_name') or original
        label = f"{source['context']['name']} / {source_name}"
        notes[folder] = {**deepcopy(source.get('folder_notes', {}).get(original, {})), 'folder': folder}
        for file in files.values():
            reuse = file.get('reuse_codebook') or {}
            old = source['files'].get(reuse.get('file_id'), {})
            if (file['folder'] == folder and reuse.get('project_id') == choice['project_id']
                    and old.get('filename') == notes[folder].get('primary_codebook')):
                notes[folder]['primary_codebook'] = file['filename']
                break
        summaries[folder] = {
            'message': f'These datasets are a copy from {label}. Reusing prepared data; the loader will not run.',
            'ingestion_notes': deepcopy(source.get('folder_notes', {}).get(original, {})),
            'descriptive_notes': deepcopy(source.get('descriptive_reviews', {}).get(original, {})),
        }
        session.message('ingestion_agent', summaries[folder]['message'])
        review = summaries[folder]['descriptive_notes']
        session.state.setdefault('descriptive_reviews', {})[folder] = review
        if review:
            text = review.get('message') or review.get('summary') or json.dumps(review, ensure_ascii=False)
            session.message('descriptives_agent', f'Reused descriptives from {label}:\n{text}')
    for file_id, file in files.items():
        choice = file.get('reuse_codebook')
        if not choice or file.get('ignore') or not (file.get('asset_kind') == 'codebook' or file.get('embedded_for')):
            continue
        _, source = source_project(session.path, choice['project_id'],
                                   user_id=session.state.get('user_id', '9999'))
        old = source['files'][choice['file_id']]
        if file_digest(file) != file_digest(old):
            raise ValueError('A reused codebook changed. Review its reuse choice again.')
        protected.add(file_id)
        findings[file_id] = deepcopy(source.get('findings', {}).get(old['file_id'], {}))
        findings[file_id].update(classification='data book',
                                 summary=f"Reusing the saved ingested codebook for {old['filename']}.")
    return protected, findings, notes, summaries


def whole_folder_copy(session, folder, files, notes, findings):
    """Check whether the selected data and every governing codebook share one source.

    Returns: True only for an identical source set with the same primary codebook.
    Extra supporting assets keep their phase-1 review without repeating data/codebook
    processing. An independent codebook choice still retains normal reconciliation.
    """
    resolved = source_for_folder(session, folder)
    if resolved is None:
        return False
    _, source, choice = resolved
    old_files = {key: file for key, file in source['files'].items() if file['folder'] == choice['folder']}
    old_primary = source.get('folder_notes', {}).get(choice['folder'], {}).get('primary_codebook', '')
    primary = notes.get('primary_codebook', '')
    current = selected_codebooks(files, primary, findings)
    previous = selected_codebooks(old_files, old_primary, source.get('findings', {}))
    if Counter(file_digest(file) for file in current) != Counter(file_digest(file) for file in previous):
        return False
    for file in current:
        reuse = file.get('reuse_codebook') or {}
        if reuse.get('project_id') != choice['project_id'] or reuse.get('folder') != choice['folder']:
            return False
    def primary_digest(books, name):
        return next((file_digest(file) for file in books if file['filename'] == name), None)
    return primary_digest(current, primary) == primary_digest(previous, old_primary)


def copy_prepared_inputs(session, folder, workspace, context):
    """Copy confirmed local Parquets and their manifest for new codebook reconciliation.

    Inputs: destination session/folder/workspace and the current folder context.
    Returns: Saved (context, prepared report). Only prepared data and reusable loading
        assets are copied; source conversations and checkpoints stay in their own run.
    The final atomic input file is the retry handoff once every required file exists.
    """
    saved = workspace / 'reused_inputs.json'
    if saved.is_file():
        payload = json.loads(saved.read_text(encoding='utf-8'))
        return payload['context'], payload['prepared']
    source_path, source, choice = source_for_folder(session, folder)
    sql_tables = [item for item in source['atlas']['data'] if item['folder'] == choice['folder']]
    original_folder = choice['folder']
    seen = set()
    while True:
        # A SQL-only copy may point to an earlier prepared run; follow that declared source.
        key = (source['id'], original_folder)
        if key in seen:
            raise ValueError('The copied dataset has a circular source reference.')
        seen.add(key)
        book = source.get('codebooks', {}).get(original_folder, {})
        source_workspace = Path(book['final']).resolve().parent.parent if book.get('final') else None
        if source_workspace and source_workspace.is_relative_to((source_path / 'runs').resolve()) and source_workspace.is_dir():
            break
        previous = source['folders'].get(original_folder, {}).get('reuse_dataset')
        if not previous:
            raise ValueError('The copied folder has no saved prepared-data workspace.')
        source_path, source = source_project(session.path, previous['project_id'],
                                             user_id=session.state.get('user_id', '9999'))
        original_folder = previous['folder']
    prepared = deepcopy(source['preparations'][original_folder])
    manifest = json.loads(work_path(source_workspace, prepared['manifest_path']).read_text(encoding='utf-8'))
    if len(manifest['tables']) != len(sql_tables):
        raise ValueError('The copied folder’s prepared tables no longer match its SQL publication.')
    prepared['_reuse_sql'] = {'project_id': choice['project_id'], 'folder': choice['folder'],
                              'table_map': {table['name']: saved_table['name']
                                            for table, saved_table in zip(manifest['tables'], sql_tables)}}
    workspace.mkdir(parents=True, exist_ok=True)
    for index, table in enumerate(manifest['tables'], 1):
        old_path = table.get('path') or table.get('file_path') or table.get('parquet_path')
        destination = workspace / 'prepared' / f'table_{index:03d}.parquet'
        destination.parent.mkdir(exist_ok=True)
        shutil.copyfile(work_path(source_workspace, old_path), destination)
        table['path'] = '/work/' + destination.relative_to(workspace).as_posix()
        table.pop('file_path', None)
        table.pop('parquet_path', None)
    save_json(workspace / 'manifest.json', manifest)
    prepared['manifest_path'] = '/work/manifest.json'
    for key in ('loading_function_path', 'preparation_notes_path'):
        if prepared.get(key):
            original = work_path(source_workspace, prepared[key])
            destination = workspace / ('prepared' + original.suffix if key == 'loading_function_path' else 'preparation_notes.md')
            shutil.copyfile(original, destination)
            prepared[key] = '/work/' + destination.name
    prepared['output_tables'] = [table['path'] for table in manifest['tables']]
    validate_prepared_manifest(workspace, prepared['manifest_path'])
    save_json(saved, {'context': context, 'prepared': prepared})
    session.state.setdefault('preparations', {})[folder] = prepared
    session.save()
    return context, prepared


def restore_codebook(session, file, directory):
    """Pull a selected ingested codebook from SQL into this run's normal source inputs.

    Returns: Its normalized DataFrame. Saves the same normalized rows, source notes and
        verification metadata that ordinary source-table assembly reads afterward.
    """
    from v_5.main_functions.sql import fetch_ingested_codebook
    choice = file['reuse_codebook']
    source_path, source = source_project(session.path, choice['project_id'],
                                        user_id=session.state.get('user_id', '9999'))
    old = source['files'][choice['file_id']]
    if file_digest(file) != file_digest(old) or not (old.get('asset_kind') == 'codebook' or old.get('embedded_for')):
        raise ValueError('The selected source is not the same previously tagged codebook.')
    frame, notes = fetch_ingested_codebook(choice['project_id'], choice['file_id'],
                                           user_id=session.state.get('user_id', '9999'))
    frame.to_csv(directory / 'normalized.csv', index=False)
    (directory / 'general_information.md').write_text(notes, encoding='utf-8')
    (directory / 'verification.txt').write_text('Reused the completed, exact-match SQL codebook.', encoding='utf-8')
    if file['format'] == 'pdf':
        original = next((item for item in source.get('codebooks', {}).get(choice['folder'], {}).get('source_tables', {}).values()
                         if item.get('file_id') == choice['file_id']), {})
        report = {'issues': ['Reused the saved SQL codebook; its original PDF verification is unavailable.']}
        if original.get('verification'):
            path = Path(original['verification']).resolve()
            if path.is_relative_to((source_path / 'runs').resolve()) and path.is_file():
                report = json.loads(path.read_text(encoding='utf-8'))
        save_json(directory / 'verification.json', report)
    session.message('codebook_agent', f"Reusing the ingested codebook for {file['filename']}.")
    return frame
