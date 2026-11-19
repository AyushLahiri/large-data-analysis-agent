"""Exercise the small exact-copy workflow with toy bytes and no paid model calls."""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from v_5.main_functions import assets
from v_5.main_functions.session import write_json


ROOT = Path(__file__).resolve().parents[2]
DATA = b'id,value\n1,red\n2,blue\n'
MORE = b'id,weight\n1,2\n2,3\n'
BOOK = b'variable_name,variable_label\nid,Identifier\nvalue,Colour\n'
ANSWERS = {'description': 'Two toy rows.\nKeep this exact note.',
           'time_geography': '2026; example town', 'source': 'My original upload',
           'unit': 'One person per row'}


def project(root, number, files, *, user='9999', published=False):
    """Write a tiny real project whose uploads can be read by production matching code.

    Inputs: root, numeric ID and (folder, filename, bytes, role, kind) records.
    Returns: project path/state. Publication metadata stands in for the SQL boundary.
    """
    project_id = f'{number:032x}'
    path = root / 'projects' / project_id
    (path / 'uploads').mkdir(parents=True)
    state = dict(version=2, id=project_id, user_id=user,
                 context={'name': f'Project {number}', 'description': 'Project note\n"verbatim"',
                          'folder_relationships': 'Related examples'},
                 files={}, folders={}, messages=[], preparations={}, status='draft', pending='')
    for index, (folder, name, raw, role, kind) in enumerate(files, 1):
        file_id = f'file_{index:03d}'
        target = path / 'uploads' / (file_id + Path(name).suffix)
        target.write_bytes(raw)
        state['folders'].setdefault(folder, {'display_name': folder.title(),
                                            'description': 'Folder note\n  Keep spacing',
                                            'relationship': 'Join on id'})
        state['files'][file_id] = dict(file_id=file_id, filename=name, path=str(target),
                                      format='csv', bytes=len(raw), folder=folder, role=role,
                                      asset_kind=kind, note=f'Exact note for {name}\nSecond line',
                                      dataset_context=deepcopy(ANSWERS) if role != 'asset' else {},
                                      continuation=['columns'] if role == 'continuation' else [],
                                      ignore=False, agent=f'agent_{file_id}')
    if published:
        state['atlas'] = {'status': 'published', 'schema': project_id, 'data': [
            {'folder': folder, 'dataset_id': f'dataset_{index:03d}', 'dataset_name': folder,
             'name': f'data_{index}', 'row_count': 2}
            for index, folder in enumerate(state['folders'], 1)], 'ingested_codebooks': []}
        for file in state['files'].values():
            if file['asset_kind'] == 'codebook':
                state['atlas']['ingested_codebooks'].append(
                    {'file_id': file['file_id'], 'name': f'ingestedcodebook_{Path(file["filename"]).stem}_csv',
                     'folder': file['folder'], 'dataset_id': 'dataset_001'})
    write_json(path / 'project.json', state)
    return path, state


def exact_bytes(root):
    """Renaming preserves a match; an equal-length byte edit does not."""
    source_path, source = project(root, 1, [('original', 'original.csv', DATA, 'primary', '')])
    path, state = project(root, 2, [('renamed', 'renamed.csv', DATA, 'primary', ''),
                                   ('changed', 'original.csv', DATA.replace(b'red', b'RED'), 'primary', '')])
    matches = assets.matching_files(path, state)
    assert len(matches['file_001']) == 1, matches
    match = matches['file_001'][0]
    assert match['project_id'] == source['id'] and match['filename'] == 'original.csv'
    assert not matches['file_002'], matches
    assert match['project_context']['description'] == source['context']['description']
    assert match['folder_context']['description'] == source['folders']['original']['description']
    assert match['file_context']['dataset_context'] == ANSWERS
    assert match['bytes'] == len(DATA)


def owner_scope(root):
    """Identical files owned by a different user never become duplicate suggestions."""
    project(root, 1, [('private', 'private.csv', DATA, 'primary', '')], user='8888', published=True)
    path, state = project(root, 2, [('mine', 'mine.csv', DATA, 'primary', '')])
    assert not assets.matching_files(path, state)['file_001']
    assert not assets.reuse_options(path, state, 'mine')['dataset_matches']


def folder_sets(root):
    """Only one complete source folder qualifies; partial and cross-folder sets do not."""
    source_path, source = project(root, 1, [('full', 'data.csv', DATA, 'primary', ''),
                                           ('full', 'more.csv', MORE, 'continuation', '')], published=True)
    path, state = project(root, 2, [('copy', 'renamed-data.csv', DATA, 'primary', ''),
                                   ('copy', 'renamed-more.csv', MORE, 'continuation', '')])
    options = assets.reuse_options(path, state, 'copy')
    assert len(options['dataset_matches']) == 1
    assert options['dataset_matches'][0]['files'] == {'file_001': 'file_001', 'file_002': 'file_002'}

    partial = deepcopy(state)
    partial['files'].pop('file_002')
    assert not assets.reuse_options(path, partial, 'copy')['dataset_matches']
    doubled = deepcopy(state)
    doubled['files']['file_003'] = {**doubled['files']['file_002'], 'file_id': 'file_003'}
    assert not assets.reuse_options(path, doubled, 'copy')['dataset_matches']

    same_project = deepcopy(source)
    same_project['folders']['local_copy'] = {'description': 'Another folder in this project'}
    for source_id, target_id in [('file_001', 'file_003'), ('file_002', 'file_004')]:
        old = source['files'][source_id]
        target = source_path / 'uploads' / (target_id + '.csv')
        target.write_bytes(Path(old['path']).read_bytes())
        same_project['files'][target_id] = {**old, 'file_id': target_id, 'path': str(target),
                                           'folder': 'local_copy'}
    write_json(source_path / 'project.json', same_project)
    local = assets.reuse_options(source_path, same_project, 'local_copy')['dataset_matches']
    assert len(local) == 1 and local[0]['project_id'] == source['id']
    assert local[0]['folder'] == 'full'

    source['files']['file_002']['folder'] = 'separate'
    source['folders']['separate'] = {'description': 'Separate dataset'}
    source['atlas']['data'].append({'folder': 'separate', 'dataset_id': 'dataset_002',
                                   'dataset_name': 'Separate', 'name': 'data_2', 'row_count': 2})
    write_json(source_path / 'project.json', source)
    assert not assets.reuse_options(path, state, 'copy')['dataset_matches']


def archived_originals(root):
    """Active uploads retain original bytes and the user's exact saved context."""
    path, state = project(root, 1, [('original', 'data.csv', DATA, 'primary', ''),
                                   ('original', 'book.csv', BOOK, 'asset', 'codebook')])
    metadata = assets.archive_project(path, state)
    saved = json.loads(metadata.read_text(encoding='utf-8'))
    archived = Path(saved['files']['file_001']['path'])
    state['files']['file_001']['note'] = 'Updated user note\n  Keep spacing exactly.'
    assets.archive_project(path, state)
    saved = json.loads(metadata.read_text(encoding='utf-8'))
    assert archived.read_bytes() == DATA
    assert saved['files']['file_001']['dataset_context'] == ANSWERS
    assert saved['files']['file_001']['note'] == state['files']['file_001']['note']
    assert saved['context']['description'] == state['context']['description']
    assert set(saved['files']) == {'file_001', 'file_002'}


def client_for(root):
    """Use real Flask routes with their required local write token."""
    from v_5.main import create_app
    client = create_app(root).test_client()
    return client, {'X-V5-Token': client.get('/api/status').json['token']}


def upload_ignores_unlisted_projects(root):
    """An old hidden project or unfinished deletion must not break a fresh upload."""
    for number in (1, 2):
        old_path, old = project(root, number, [('old', 'data.csv', DATA, 'primary', '')])
        if number == 1:
            assets.archive_project(old_path, old)
        old.pop('version')
        old['folders'] = ['old']
        write_json(old_path / 'project.json', old)
    staged_path, staged = project(root, 3, [('old', 'data.csv', DATA, 'primary', '')])
    staged_path.rename(staged_path.with_name('.deleting-' + staged['id']))
    path, state = project(root, 4, [])
    _, source = project(root, 5, [('source', 'data.csv', DATA, 'primary', '')])
    client, headers = client_for(root)
    response = client.post(f'/api/projects/{state["id"]}/upload', data={
        'role': 'primary', 'folder': '', 'files': (BytesIO(DATA), 'households.csv')},
        headers=headers, content_type='multipart/form-data')
    assert response.status_code == 200, response.data
    saved = client.get(f'/api/projects/{state["id"]}').json
    assert saved['active_folder'] == 'households' and len(saved['files']) == 1
    file = next(iter(saved['files'].values()))
    assert Path(file['path']).read_bytes() == DATA
    assert [match['project_id'] for match in file['duplicates']] == [source['id']]
    assert not (root / 'users' / '9999' / 'projects' / old['id']).exists()
    assert not (root / 'users' / '9999' / 'projects' / staged['id']).exists()


def api_setup(root):
    """Walk question-free upload, exact-copy choices, folder locks, done and restart."""
    source_path, source = project(root, 1, [('source', 'data.csv', DATA, 'primary', ''),
                                           ('source', 'book.csv', BOOK, 'asset', 'codebook')], published=True)
    client, headers = client_for(root)
    with patch('v_5.main.rename_atlas_project'):
        response = client.post('/api/projects', json={'name': 'Destination'}, headers=headers)
    assert response.status_code == 200, response.json
    target = response.json
    assert target['user_id'] == '9999'
    base = f'/api/projects/{target["id"]}'
    path = root / 'projects' / target['id']

    def post(route, body, expected=200):
        """Assert each real route's response while keeping fixture calls readable."""
        response = client.post(base + route, json=body, headers=headers)
        assert response.status_code == expected, (route, response.status_code, response.json)
        return response.json

    def upload(folder, role, raw=DATA, name='renamed.csv', expected=200):
        """Submit raw files first, without forcing the later per-file questionnaire."""
        response = client.post(base + '/upload', data={
            'folder': folder, 'role': role, 'files': (BytesIO(raw), name)},
            headers=headers, content_type='multipart/form-data')
        assert response.status_code == expected, (response.status_code, response.json)

    upload('copy', 'primary')
    upload('too_soon', 'primary', expected=400)
    upload('copy', 'mixed', BOOK, 'book.csv')
    target = client.get(base).json
    data_file = next(f for f in target['files'].values() if f['role'] == 'primary')
    book_file = next(f for f in target['files'].values() if f['filename'] == 'book.csv')
    options = client.get(base + '/reuse?folder=copy').json
    assert len(options['dataset_matches']) == 1
    assert options['files'][data_file['file_id']][0]['other_files'] == ['book.csv']
    choice = {'project_id': source['id'], 'folder': 'source', 'file_id': 'file_002'}
    post('/file-details', {'file_id': data_file['file_id'], 'role': 'primary',
                           'dataset_context': {}, 'note': 'Will be replaced by source answers'})
    post('/file-details', {'file_id': book_file['file_id'], 'role': 'asset',
                           'asset_kind': 'report', 'reuse_codebook': choice}, expected=400)
    post('/file-details', {'file_id': book_file['file_id'], 'role': 'asset',
                           'asset_kind': 'codebook', 'reuse_codebook': choice, 'note': 'Current book note'})
    post('/folder-setup', {'folder': 'copy', 'reuse_dataset': {
        'project_id': source['id'], 'folder': 'source'}})
    target = client.get(base).json
    assert target['files'][data_file['file_id']]['dataset_context'] == ANSWERS
    assert target['files'][data_file['file_id']]['path'] == data_file['path']
    assert target['files'][data_file['file_id']]['folder'] == 'copy'
    assert target['folders']['copy']['description'] == source['folders']['source']['description']
    assert target['files'][book_file['file_id']]['reuse_codebook'] == choice
    assert target['active_folder'] is None
    post('/file-details', {'file_id': data_file['file_id'], 'role': 'primary',
                           'dataset_context': ANSWERS}, expected=400)
    post('/context', {'context': {}, 'folders': {'copy': {'description': 'Changed locked answer'}}}, expected=400)
    post('/rename-folder', {'folder': 'copy', 'name': 'Changed locked label'}, expected=400)
    upload('copy', 'mixed', MORE, 'extra.csv', expected=400)
    post('/finish-folder', {'folder': 'copy'}, expected=400)

    # Completing the form unlocks the next upload before either folder is published.
    upload('next', 'primary', MORE, 'next.csv')
    post('/start', {}, expected=400)
    target = client.get(base).json
    # This is the SQL boundary for the optional publication receipt.
    target['atlas'] = {'status': 'published', 'data': [
        {'folder': 'copy', 'dataset_id': 'dataset_001', 'dataset_name': 'Copy',
         'name': 'copied_data', 'row_count': 2}]}
    write_json(path / 'project.json', target)
    post('/finish-folder', {'folder': 'copy'}, expected=400)
    assert client.get(base).json['active_folder'] == 'next'
    with patch('v_5.main_functions.reuse_routes.delete_atlas_folder', return_value=None) as remove_sql:
        post('/restart-ingestion', {'folder': 'copy', 'confirmation': 'restart'})
    remove_sql.assert_called_once_with(target['id'], 'copy')
    target = client.get(base).json
    assert target['active_folder'] == 'copy'
    assert not target['folders']['copy'].get('setup_reviewed')
    assert not target['folders']['next'].get('setup_complete')
    assert 'reuse_dataset' not in target['folders']['copy']
    assert Path(data_file['path']).read_bytes() == DATA
    post('/delete-file', {'file_id': book_file['file_id'], 'confirmation': 'delete'})
    metadata = root / 'users' / '9999' / 'projects' / target['id'] / 'assets.json'
    archived = json.loads(metadata.read_text(encoding='utf-8'))
    assert book_file['file_id'] not in archived['files']
    assert not Path(book_file['path']).exists()
    assert not (metadata.parent / 'raw' / (book_file['file_id'] + '.csv')).exists()
    assert not assets.matching_files(source_path, source)['file_002']
    with patch('v_5.main.delete_atlas_project'):
        post('/delete', {'confirmation': 'delete'})
    assert not path.exists() and not metadata.parent.exists()


def prepared_fixture(path, state, folder):
    """Add genuine Parquet and manifest artifacts consumed by the production copy path."""
    import pandas as pd
    workspace = path / 'runs' / 'source_release' / 'loader' / 'folder_001'
    (workspace / 'codebooks').mkdir(parents=True)
    pd.DataFrame({'id': [1, 2], 'value': ['red', 'blue']}).to_parquet(workspace / 'data.parquet', index=False)
    manifest = {'tables': [{'name': 'toy', 'path': '/work/data.parquet', 'row_count': 2,
                            'primary_key': ['id'], 'columns': [
                                {'name': 'id', 'original_name': 'id', 'type': 'int', 'sql_definition': 'BIGINT'},
                                {'name': 'value', 'original_name': 'value', 'type': 'character', 'sql_definition': 'TEXT'}]}]}
    write_json(workspace / 'manifest.json', manifest)
    (workspace / 'codebooks' / 'final_codebook.csv').write_text('variable_name\nid\nvalue\n', encoding='utf-8')
    state.setdefault('codebooks', {})[folder] = {'final': str(workspace / 'codebooks' / 'final_codebook.csv')}
    state['preparations'][folder] = {'manifest_path': '/work/manifest.json',
                                     'output_tables': ['/work/data.parquet'],
                                     'loading_function_path': '', 'preparation_notes_path': '',
                                     'summary': 'Two checked rows'}
    state['folder_notes'] = {folder: {'folder': folder, 'primary_codebook': 'book.csv',
                                     'summary': 'Prepared two rows and verified identifiers.'}}
    state['descriptive_reviews'] = {folder: {'action': 'confirm', 'message': 'Two rows; no missing IDs.'}}
    write_json(path / 'project.json', state)


def pipeline_branches(root):
    """Drive the real preparation function through full-copy, changed-books and separate branches."""
    from v_5.ingest.data_loader import functions
    from v_5.ingest import reuse
    from v_5.main_functions.session import Session
    source_path, source = project(root, 1, [('original', 'data.csv', DATA, 'primary', ''),
                                           ('original', 'book.csv', BOOK, 'asset', 'codebook')], published=True)
    prepared_fixture(source_path, source, 'original')
    path, state = project(root, 2, [('copy', 'data.csv', DATA, 'primary', ''),
                                   ('copy', 'book.csv', BOOK, 'asset', 'codebook'),
                                   ('copy', 'new_notes.md', b'New supporting notes.', 'asset', 'report')])
    state['folders']['copy']['reuse_dataset'] = {'project_id': source['id'], 'folder': 'original',
                                                'files': {'file_001': 'file_001'}}
    state['files']['file_002']['reuse_codebook'] = {'project_id': source['id'], 'folder': 'original',
                                                  'file_id': 'file_002'}
    session = Session(path, state)
    protected, findings, notes, summaries = reuse.reused_materials(session, state['files'])
    assert protected == {'file_001', 'file_002'}
    assert state['files']['file_003']['asset_kind'] == 'report'  # The extra asset still needs ordinary review.
    assert summaries['copy']['descriptive_notes']['message'] == 'Two rows; no missing IDs.'
    assert reuse.whole_folder_copy(session, 'copy', state['files'], notes['copy'], findings)
    payload = dict(project_notes='Toy project', folder_notes=notes, files=state['files'], findings=findings,
                   previews={}, user_clarifications=[], decision={'action': 'finish'})
    with patch.object(functions, 'LoaderRunner', side_effect=AssertionError('Loader must not run')) as loader, \
            patch.object(functions, 'complete_codebooks') as codebooks, \
            patch('v_5.ingest.publishing.publish_reused_folder') as copy_sql, \
            patch.object(session, 'run_code', side_effect=AssertionError('Full copy needs no Docker')), \
            patch.object(functions, 'summary_ready', return_value=True), \
            patch.object(functions, 'write_final_summary', side_effect=AssertionError('Full copy needs no summary model')):
        assert functions.prepare_data(session, **payload) == 'complete'
        assert copy_sql.call_count == 1 and codebooks.call_count == 0 and loader.call_count == 0

    # Identical raw data with different documentation keeps the codebook pipeline.
    state['files']['file_002'].pop('reuse_codebook')
    Path(state['files']['file_002']['path']).write_bytes(BOOK.replace(b'Colour', b'Color!'))
    state['files']['file_002'].pop('sha256', None)
    assert not reuse.whole_folder_copy(session, 'copy', state['files'], notes['copy'], findings)
    with patch.object(functions, 'LoaderRunner', side_effect=AssertionError('Copied data needs no loader')), \
            patch.object(functions, 'complete_codebooks') as codebooks, \
            patch('v_5.ingest.publishing.publish_reused_folder') as copy_sql, \
            patch.object(session, 'run_code', return_value={'exit_code': 0, 'output': 'test environment'}), \
            patch.object(functions, 'summary_ready', return_value=False):
        assert functions.prepare_data(session, **payload) == 'complete'
        assert codebooks.call_count == 1 and copy_sql.call_count == 0
        assert codebooks.call_args.args[2]['_reuse_sql']['table_map'] == {'toy': 'data_1'}
        workspace = codebooks.call_args.args[4]
        assert workspace.is_relative_to(path / 'runs')
        assert (workspace / 'reused_inputs.json').is_file()
        import pandas as pd
        assert pd.read_parquet(workspace / 'prepared' / 'table_001.parquet')['value'].tolist() == ['red', 'blue']

    state['folders']['copy'].pop('reuse_dataset')
    state.pop('codebook_resume', None)
    state.pop('loader_resume', None)
    with patch.object(functions, 'LoaderRunner', return_value=SimpleNamespace(run=lambda: {'phase': 'stopped'})) as loader, \
            patch.object(functions, 'LoaderCalls'), \
            patch.object(functions, 'complete_codebooks') as codebooks, \
            patch.object(session, 'run_code', return_value={'exit_code': 0, 'output': 'test environment'}), \
            patch.object(functions, 'summary_ready', return_value=False):
        assert functions.prepare_data(session, **payload) == 'blocked'
        assert loader.call_count == 1 and codebooks.call_count == 0


def ready_folder_batch(root):
    """Review and prepare both ready folders when the explicit start has no active upload."""
    from v_5.ingest.initial_ingestion import functions as ingestion
    from v_5.ingest.data_loader import functions as loader
    from v_5.main_functions.session import Session
    path, state = project(root, 1, [('first', 'first.csv', DATA, 'primary', ''),
                                   ('second', 'second.csv', MORE, 'primary', ''),
                                   ('published', 'old.csv', DATA, 'primary', '')])
    state['active_folder'] = None
    for folder in state['folders'].values():
        folder['setup_reviewed'] = True
    state['atlas'] = {'data': [{'folder': 'published'}]}
    session = Session(path, state)

    def answer(agent, *args):
        """Replace model replies while exercising the real material selection and handoff."""
        if agent == 'context_checker':
            return {'accepted': True, 'questions': []}
        return {'action': 'finish', 'project_notes': '', 'folder_notes': [], 'file_updates': []}

    with patch.object(ingestion, 'create_file_reviewers', return_value=(lambda files: None, None, {}, {})) as reviewers, \
            patch.object(session, 'ask_agent', answer), patch.object(session, 'answer'), patch.object(session, 'save_handoff'):
        payload = ingestion.review_materials(session)
        assert {file['folder'] for file in reviewers.call_args.args[3].values()} == {'first', 'second'}

    def runner(workspace, calls, execute, context, *args, **kwargs):
        """Return a completed toy loader cycle for whichever real folder loop invokes it."""
        return SimpleNamespace(run=lambda: {'phase': 'complete', 'context': context(),
                                           'loader': {'reply': {'manifest_path': '/work/test.json'}}})

    with patch.object(loader, 'LoaderRunner', side_effect=runner), patch.object(loader, 'LoaderCalls'), \
            patch.object(loader, 'validate_prepared_manifest'), patch.object(loader, 'complete_codebooks') as books, \
            patch.object(loader, 'summary_ready', return_value=False), \
            patch.object(session, 'run_code', return_value={'exit_code': 0, 'output': 'test environment'}):
        assert loader.prepare_data(session, **payload) == 'complete'
    assert [call.args[1]['folder'] for call in books.call_args_list] == ['first', 'second']
    assert [call.args[-1] for call in books.call_args_list] == ['second', None]


def codebook_restore(root):
    """Reuse the normal source-ingestion path while preventing any extraction model call."""
    from v_5.ingest.codebook_ingestion import functions
    from v_5.ingest.codebook_ingestion.tables import normalize_rows
    from v_5.main_functions.session import Session
    source_path, source = project(root, 1, [('source', 'data.csv', DATA, 'primary', ''),
                                           ('source', 'book.csv', BOOK, 'asset', 'codebook')], published=True)
    path, state = project(root, 2, [('copy', 'data.csv', DATA, 'primary', ''),
                                   ('copy', 'renamed-book.csv', BOOK, 'asset', 'codebook')])
    prepared_fixture(path, state, 'copy')
    workspace = Path(state['codebooks']['copy']['final']).parent.parent
    file = state['files']['file_002']
    file['reuse_codebook'] = {'project_id': source['id'], 'folder': 'source', 'file_id': 'file_002'}
    frame = normalize_rows([{'variable_name': 'id', 'variable_label': 'Identifier'},
                            {'variable_name': 'value', 'variable_label': 'Colour'}])
    context = {'folder': 'copy', 'ingestion_notes': {}, 'files': {}}
    session = Session(path, state)
    with patch('v_5.main_functions.sql.fetch_ingested_codebook', return_value=(frame, 'Original codebook notes')) as fetch, \
            patch.object(functions, 'structured_codebook', side_effect=AssertionError('Reused book must skip extraction')), \
            patch.object(functions, 'pdf_codebook', side_effect=AssertionError('No PDF is involved')):
        book = functions.ingest_codebooks(session, context, state['preparations']['copy'],
                                         state['files'], workspace, 'test environment')
        assert fetch.call_count == 1 and book['sources'] == 1
        saved = workspace / 'codebooks' / file['file_id'] / 'normalized.csv'
        assert saved.is_file()
        assert (saved.parent / 'general_information.md').read_text(encoding='utf-8') == 'Original codebook notes'
        file['asset_kind'] = 'report'
        book = functions.ingest_codebooks(session, context, state['preparations']['copy'],
                                         state['files'], workspace, 'test environment')
        assert fetch.call_count == 1 and book['sources'] == 0


def run():
    """Run independent focused scenarios, cleaning only their temporary workspaces."""
    cases = [exact_bytes, owner_scope, folder_sets, archived_originals, upload_ignores_unlisted_projects, api_setup,
             pipeline_branches, ready_folder_batch, codebook_restore]
    for case in cases:
        with tempfile.TemporaryDirectory(prefix='_reuse_', dir=ROOT) as temporary:
            case(Path(temporary))
        print(f'PASS {case.__name__}')
    print(f'{len(cases)} focused reuse checks passed; no model calls.')


if __name__ == '__main__':
    run()
