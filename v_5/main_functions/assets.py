"""Keep active original uploads and user-written context outside disposable runs."""

from collections import Counter, defaultdict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil

ADMIN_USER_ID = '9999'
FILE_FIELDS = ('file_id', 'filename', 'format', 'bytes', 'folder', 'role', 'asset_kind',
               'note', 'dataset_context', 'continuation', 'sha256', 'ignore')
FOLDER_FIELDS = ('display_name', 'description', 'relationship')


def file_digest(path):
    """Return the SHA256 of the original bytes; filenames and parsing do not affect it."""
    with Path(path).open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def source_project(project_path, project_id, user_id=ADMIN_USER_ID):
    """Resolve another live project owned by this user for a confirmed reuse choice.

    Inputs: destination project path, source ID and owner. Returns: source path/state.
    Deleted projects and files never supply duplicate matches or reuse.
    """
    if len(project_id) != 32 or any(c not in '0123456789abcdef' for c in project_id):
        raise ValueError('Invalid source project ID.')
    path = Path(project_path).parent / project_id
    state = json.loads((path / 'project.json').read_text(encoding='utf-8'))
    if str(state.get('user_id', ADMIN_USER_ID)) != str(user_id):
        raise ValueError('The source belongs to another user.')
    return path, state


def archive_project(path, state, *, edited_folder=None):
    """Retain active original uploads and verbatim answers independently of ingestion runs.

    Inputs: live project path/state. Returns: archive metadata path; adds file hashes to
    live state. Original bytes are copied once. The small metadata file is updated when
    the user edits answers. Removed files and their metadata are deleted here too.
    """
    from v_5.main_functions.session import write_json
    path = Path(path)
    user = str(state.setdefault('user_id', ADMIN_USER_ID))
    directory = path.parent.parent / 'users' / user / 'projects' / state['id']
    metadata = directory / 'assets.json'
    saved = json.loads(metadata.read_text(encoding='utf-8')) if metadata.exists() else {'files': {}, 'folders': {}}
    active_ids = {key for key, file in state['files'].items() if not file.get('embedded_for')}
    for file_id in set(saved['files']) - active_ids:
        raw = Path(saved['files'][file_id]['path']).resolve()
        if not raw.is_relative_to((directory / 'raw').resolve()):
            raise ValueError('Invalid saved original path.')
        raw.unlink(missing_ok=True)
        del saved['files'][file_id]
    protected = locked_setup_folders(state) | {item['folder'] for item in state.get('atlas', {}).get('data', [])}
    saved.update(id=state['id'], user_id=user, context=deepcopy(state['context']))
    saved['folders'] = {key: value for key, value in saved['folders'].items() if key in state['folders']}
    for name, folder in state['folders'].items():
        if name in protected and name != edited_folder and name in saved['folders']:
            continue
        saved['folders'][name] = {key: deepcopy(folder[key]) for key in FOLDER_FIELDS if key in folder}
    for file_id, file in state['files'].items():
        if file.get('embedded_for'):
            continue  # Generated dictionaries are reproducible; this archive is for user originals.
        source = Path(file['path'])
        if not source.is_file():
            continue
        if not file.get('sha256'):
            file['sha256'] = file_digest(source)
        if file['folder'] in protected and file['folder'] != edited_folder and file_id in saved['files']:
            continue
        raw = directory / 'raw' / (file_id + source.suffix.lower())
        if not raw.exists():
            raw.parent.mkdir(parents=True, exist_ok=True)
            temporary = raw.with_suffix(raw.suffix + '.tmp')
            shutil.copyfile(source, temporary)
            temporary.replace(raw)
        saved['files'][file_id] = {**{key: deepcopy(file[key]) for key in FILE_FIELDS if key in file},
                                   'path': str(raw.resolve())}
    write_json(metadata, saved)
    return metadata


def delete_archived_project(path, state):
    """Remove this project's retained originals after explicit project deletion.

    Inputs: project path and its saved ID/owner. Returns: None; removes originals and
    their description metadata. The delete route calls this only after SQL commits.
    """
    users = Path(path).parent.parent / 'users'
    parent = users / str(state.get('user_id', ADMIN_USER_ID)) / 'projects'
    target = (parent / state['id']).resolve()
    if (not target.is_relative_to(users.resolve()) or not target.is_relative_to(parent.resolve())
            or target.name != state['id'] or Path(path).name != state['id']):
        raise ValueError('Invalid original-file directory.')
    if target.exists():
        shutil.rmtree(target)


def prune_deleted_assets(path, state):
    """Remove originals excluded by ingestion without rewriting the user's saved answers."""
    from v_5.main_functions.session import write_json
    directory = Path(path).parent.parent / 'users' / str(state.get('user_id', ADMIN_USER_ID)) / 'projects' / state['id']
    metadata = directory / 'assets.json'
    if not metadata.exists():
        return
    saved = json.loads(metadata.read_text(encoding='utf-8'))
    for key in set(saved['files']) - set(state['files']):
        raw = Path(saved['files'][key]['path']).resolve()
        if not raw.is_relative_to((directory / 'raw').resolve()):
            raise ValueError('Invalid saved original path.')
        raw.unlink(missing_ok=True)
        del saved['files'][key]
    saved['folders'] = {key: value for key, value in saved['folders'].items() if key in state['folders']}
    write_json(metadata, saved)


def matching_files(path, state, *, refresh=True):
    """Find byte-identical uploads for this user and expose only relevant user context.

    Returns: file-ID to source matches. Each match includes project/folder descriptions,
    this file's answers and other filenames, without unrelated files' descriptions.
    Existing version-2 projects are archived lazily so their uploads also participate.
    Old-format projects and staged deletions are excluded, as they are from normal use.
    """
    path = Path(path)
    user = str(state.get('user_id', ADMIN_USER_ID))
    if refresh:
        for record in path.parent.glob('*/project.json'):
            other = json.loads(record.read_text(encoding='utf-8'))
            # Match the project picker's format rule; hidden old folders are not inputs.
            if other.get('version') != 2 or record.parent.name != other['id']:
                continue
            existing = path.parent.parent / 'users' / user / 'projects' / other['id'] / 'assets.json'
            if str(other.get('user_id', ADMIN_USER_ID)) == user and not existing.exists():
                archive_project(record.parent, state if other['id'] == state['id'] else other)
        archive_project(path, state)
    by_hash = defaultdict(list)
    requested = {(file.get('sha256'), file['bytes']) for file in state['files'].values()}
    for record in (path.parent.parent / 'users' / user / 'projects').glob('*/assets.json'):
        owner = json.loads(record.read_text(encoding='utf-8'))
        try:
            _, live = source_project(path, owner['id'], user)
        except FileNotFoundError:
            continue
        if live.get('version') != 2:
            continue
        publication = live.get('atlas', {})
        if (refresh and live.get('status') == 'complete' and 'ingested_codebooks' not in publication
                and any(book.get('source_tables') for book in live.get('codebooks', {}).values())
                and any((f.get('sha256'), f['bytes']) in requested and f.get('asset_kind') == 'codebook'
                        for f in owner['files'].values())):
            from v_5.ingest.publishing import register_existing_codebooks
            publication = register_existing_codebooks(path.parent / live['id'], live)
        for source in owner['files'].values():
            if source['file_id'] not in live.get('files', {}):
                continue
            folder = source['folder']
            details = owner['folders'].get(folder, {})
            table = next((item for item in publication.get('ingested_codebooks', [])
                          if item['file_id'] == source['file_id']), None)
            is_current = source['file_id'] in live.get('files', {})
            by_hash[(source.get('sha256'), source['bytes'])].append({
                'project_id': owner['id'], 'project_name': owner['context']['name'],
                'folder': folder, 'folder_name': details.get('display_name') or folder,
                'file_id': source['file_id'], 'filename': source['filename'], 'bytes': source['bytes'],
                'project_context': owner['context'], 'folder_context': details,
                'file_context': {key: source.get(key) for key in ('note', 'dataset_context', 'role', 'asset_kind', 'continuation')},
                'other_files': [f['filename'] for f in owner['files'].values()
                                if f['folder'] == folder and f['file_id'] != source['file_id']],
                'published': is_current and any(t['folder'] == folder for t in publication.get('data', [])),
                'was_codebook': source.get('role') == 'asset' and source.get('asset_kind') == 'codebook',
                'source_table': table['name'] if table and is_current else None,
            })
    return {key: [match for match in by_hash[(file.get('sha256'), file['bytes'])]
                  if (match['project_id'], match['folder']) != (state['id'], file['folder'])]
            for key, file in state['files'].items() if not file.get('embedded_for')}


def data_files(state, folder):
    """Select the raw data set for matching, preserving duplicate-file multiplicity."""
    return [file for file in state['files'].values() if file['folder'] == folder
            and file['role'] in {'primary', 'continuation'} and not file.get('ignore')]


def data_key(file):
    """Identify exact bytes and the anchor role; continuation order/names may differ."""
    return (file.get('sha256') or file_digest(file['path']), file['bytes'], file['role'] == 'primary')


def reuse_options(path, state, folder):
    """Offer full-folder reuse only when one published folder has the identical data set.

    Returns: per-file duplicate details, eligible codebooks and complete dataset matches.
    Matches spread across folders remain informational and never become a copy plan.
    """
    matches = matching_files(path, state)
    files = data_files(state, folder)
    wanted = Counter(data_key(file) for file in files)
    candidates, seen = [], set()
    for file in files:
        for match in matches.get(file['file_id'], []):
            source_key = (match['project_id'], match['folder'])
            if not match['published'] or source_key in seen:
                continue
            seen.add(source_key)
            _, source = source_project(path, match['project_id'], state.get('user_id', ADMIN_USER_ID))
            source_files = data_files(source, match['folder'])
            if wanted != Counter(data_key(item) for item in source_files):
                continue
            pool = defaultdict(list)
            for item in source_files:
                pool[data_key(item)].append(item['file_id'])
            candidates.append({**match, 'files': {item['file_id']: pool[data_key(item)].pop(0) for item in files}})
    return {'files': {key: value for key, value in matches.items() if state['files'][key]['folder'] == folder},
            'dataset_matches': candidates,
            'codebook_matches': {key: [m for m in value if m['was_codebook'] and m['source_table'] and m['published']]
                                 for key, value in matches.items() if state['files'][key]['folder'] == folder}}


def locked_setup_folders(state):
    """Lock saved forms normally, but allow unpublished material edits after a stop/failure."""
    if state.get('status') in {'stopped', 'failed', 'interrupted', 'blocked'} or state.get('ingestion_changed'):
        return set()
    return {key for key, value in state['folders'].items()
            if value.get('setup_reviewed') or value.get('setup_complete')}
