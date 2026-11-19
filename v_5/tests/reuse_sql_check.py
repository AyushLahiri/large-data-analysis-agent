"""Three small Atlas integration checks; temporary projects only, no model calls."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import pandas as pd
from psycopg2 import sql

from v_5.ingest.codebook_ingestion.tables import COLUMNS, normalize_rows
from v_5.ingest.publishing import collect_outputs, publish_reused_folder, write_publication
from v_5.main_functions.assets import archive_project
from v_5.main_functions.session import write_json
from v_5.main_functions.sql import (
    CODEBOOK_FIELDS, SOURCE_CODEBOOK_FIELDS, CORE, INFO, UNMATCHED, SOFT_MATCHED, atlas_transaction, delete_atlas_folder,
    delete_atlas_project, ensure_registry, fetch_ingested_codebook, project_table,
    rename_atlas_project,
)
from v_5.tests.reuse_check import BOOK, DATA, ROOT, project


def session(path, state):
    """Provide the real publication functions with a quiet local session."""
    run = path / 'runs' / 'check'
    run.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(path=path, state=state, run_path=run,
                           check_stop=lambda: None, message=lambda *a: None,
                           save=lambda: write_json(path / 'project.json', state))


def seed(path, state):
    """Publish two toy rows and one individually normalized source codebook."""
    work = path / 'runs' / 'check' / 'loader' / 'folder_001'
    books = work / 'codebooks'
    books.mkdir(parents=True)
    data = pd.DataFrame({'id': [1, 2], 'value': ['red', 'blue']})
    data.to_parquet(work / 'toy.parquet', index=False)
    columns = {'id': {'type': 'int', 'nullable': False}, 'value': {'type': 'character'}}
    manifest = {'tables': [{'name': 'toy_data', 'path': '/work/toy.parquet', 'row_count': 2,
                            'columns': columns, 'primary_key': ['id']}]}
    write_json(work / 'manifest.json', manifest)
    normalized = normalize_rows([{'variable_name': 'id', 'variable_label': 'Identifier'},
                                 {'variable_name': 'value', 'variable_label': 'Colour'}])
    normalized.rename(columns={c: c + '_boo' for c in COLUMNS if c != 'variable_name'}).to_csv(books / 'source.csv', index=False)
    (books / 'notes.md').write_text('Original codebook notes\nKeep this line.', encoding='utf-8')
    source = {'filename': 'book.csv', 'file_id': 'file_002', 'table': str(books / 'source.csv'),
              'notes': str(books / 'notes.md'), 'primary': True}
    state['codebooks'] = {'source': {'source_tables': {'boo': source}, 'final': str(books / 'final_codebook.csv')}}
    state['preparations'] = {'source': {'manifest_path': '/work/manifest.json'}}
    state['folder_notes'] = {'source': {'folder': 'source', 'summary': 'Two confirmed toy rows.', 'primary_codebook': 'book.csv'}}
    state['descriptive_reviews'] = {'source': {'message': 'Two rows, no missing IDs.'}}
    archive_project(path, state)
    target = session(path, state)
    items = [{'dataset_id': 'dataset_001', 'dataset_name': 'Source', 'folder': 'source',
              'name': 'toy_data', 'path': work / 'toy.parquet', 'columns': columns,
              'row_count': 2, 'primary_key': ['id']}]
    core = []
    for variable in ['id', 'value']:
        values = dict.fromkeys(CODEBOOK_FIELDS)
        values.update(variable_name=variable, variable_label=variable, variable_description='',
                      battery_members='[]', table_name='toy_data', table_names=['toy_data'], column_name=variable)
        core.append(('dataset_001', *values.values()))
    source_values = dict.fromkeys(SOURCE_CODEBOOK_FIELDS)
    source_values.update(variable_name='value_root', variable_label='Colour root', battery_members='[]',
                         other_information='Soft matches: {"value": "The documented root defines the colour field."}',
                         soft_match='["value"]')
    soft = [('dataset_001', *source_values.values())]
    unused_values = {**source_values, 'variable_name': 'unused_root', 'soft_match': '[]', 'other_information': 'Unused source.'}
    unmatched = [('dataset_001', *unused_values.values())]
    outputs = (items, core, unmatched, soft, [('dataset_001', 'book.csv', 'Original notes')],
               {'project': 'Source project', 'datasets': [{'dataset_id': 'dataset_001', 'notes': 'Confirmed source folder'}]})
    with atlas_transaction() as cursor:
        ensure_registry(cursor)
        publication = write_publication(cursor, target, 'check', outputs,
                                        [json.dumps([1.0] + [0.0] * 1999), None])
    state['atlas'] = publication
    # Final CSV is needed only by the changed-codebook continuation check below.
    rows = [dict(zip(['dataset_id', *CODEBOOK_FIELDS], row)) for row in core]
    pd.DataFrame(rows).to_csv(books / 'final_codebook.csv', index=False)
    pd.DataFrame([unused_values]).to_csv(books / 'final_unmatched_codebook.csv', index=False)
    pd.DataFrame([source_values]).to_csv(books / 'soft_matched_codebook.csv', index=False)
    state['codebooks']['source']['final_unmatched'] = str(books / 'final_unmatched_codebook.csv')
    state['codebooks']['source']['soft_matched'] = str(books / 'soft_matched_codebook.csv')
    collected = collect_outputs(path, state, {'datasets': [{'folder': 'source', 'workspace': str(work),
        'prepared': state['preparations']['source'], 'book': state['codebooks']['source']}]})
    assert len(collected) == 6 and len(collected[2]) == len(collected[3]) == 1
    assert json.loads(collected[3][0][-1]) == ['value']
    target.save()
    return target, normalized


def main():
    """Check normalized source SQL, copied references, retry and selective folder cleanup."""
    ids = []
    with tempfile.TemporaryDirectory(prefix='_reuse_sql_', dir=ROOT) as temporary:
        root = Path(temporary)
        try:
            number = int(uuid4().hex, 16)
            path, state = project(root, number, [('source', 'data.csv', DATA, 'primary', ''),
                                                ('source', 'book.csv', BOOK, 'asset', 'codebook')])
            ids.append(state['id'])
            rename_atlas_project(state['id'], state['context']['name'])
            original, normalized = seed(path, state)
            restored, notes = fetch_ingested_codebook(state['id'], 'file_002')
            assert restored.to_dict('records') == normalized.to_dict('records')
            assert notes == 'Original codebook notes\nKeep this line.'
            assert state['atlas']['ingested_codebooks'][0]['name'] == 'ingestedcodebook_book_csv'
            with atlas_transaction() as cursor:
                cursor.execute('SELECT user_id FROM public.atlas_projects WHERE project_id=%s', (state['id'],))
                assert cursor.fetchone()[0] == '9999'
            print('PASS normalized codebook SQL and admin registry')

            for same_project in (False, True):
                if same_project:
                    destination, target_state = path, state
                    folder = 'same_project_copy'
                    target_state['folders'][folder] = {'description': 'Copy'}
                    for old_id, new_id in [('file_001', 'file_003'), ('file_002', 'file_004')]:
                        target_state['files'][new_id] = {**deepcopy(state['files'][old_id]), 'file_id': new_id, 'folder': folder}
                    data_id, book_id = 'file_003', 'file_004'
                else:
                    destination, target_state = project(root, int(uuid4().hex, 16),
                        [('copied', 'data.csv', DATA, 'primary', ''), ('copied', 'book.csv', BOOK, 'asset', 'codebook')])
                    folder, data_id, book_id = 'copied', 'file_001', 'file_002'
                    ids.append(target_state['id'])
                    rename_atlas_project(target_state['id'], target_state['context']['name'])
                target_state['folders'][folder]['reuse_dataset'] = {'project_id': state['id'], 'folder': 'source', 'files': {data_id: 'file_001'}}
                target_state['files'][book_id]['reuse_codebook'] = {'project_id': state['id'], 'folder': 'source', 'file_id': 'file_002'}
                target = session(destination, target_state)
                result = publish_reused_folder(target, folder, state['id'], 'source')
                assert publish_reused_folder(target, folder, state['id'], 'source') == result
                copied = next(t for t in result['data'] if t['folder'] == folder)
                with atlas_transaction() as cursor:
                    cursor.execute(sql.SQL('SELECT * FROM {} ORDER BY id').format(project_table(target_state['id'], copied['name'])))
                    assert cursor.fetchall() == [(1, 'red'), (2, 'blue')]
                    cursor.execute(sql.SQL('SELECT DISTINCT table_name,table_names FROM {} WHERE dataset_id=%s').format(
                        project_table(target_state['id'], CORE)), (copied['dataset_id'],))
                    assert cursor.fetchall() == [(copied['name'], [copied['name']])]
                    cursor.execute(sql.SQL('SELECT count(embedding) FROM {} WHERE dataset_id=%s').format(
                        project_table(target_state['id'], CORE)), (copied['dataset_id'],))
                    assert cursor.fetchone()[0] == 1
                    cursor.execute(sql.SQL('SELECT variable_name,soft_match,other_information FROM {} WHERE dataset_id=%s').format(
                        project_table(target_state['id'], SOFT_MATCHED)), (copied['dataset_id'],))
                    assert cursor.fetchall() == [('value_root', ['value'], 'Soft matches: {"value": "The documented root defines the colour field."}')]
                    cursor.execute(sql.SQL('SELECT variable_name,soft_match FROM {} WHERE dataset_id=%s').format(
                        project_table(target_state['id'], UNMATCHED)), (copied['dataset_id'],))
                    assert cursor.fetchall() == [('unused_root', [])]
                    cursor.execute(sql.SQL('SELECT count(*) FROM {}').format(project_table(state['id'], 'toy_data')))
                    assert cursor.fetchone()[0] == 2
                restored, _ = fetch_ingested_codebook(target_state['id'], book_id)
                assert restored.to_dict('records') == normalized.to_dict('records')
                if same_project:
                    assert copied['name'] != 'toy_data' and copied['dataset_id'] == 'dataset_002'
                    remaining = delete_atlas_folder(state['id'], folder)
                    assert len(remaining['data']) == 1 and len(remaining['ingested_codebooks']) == 1
                    assert remaining['codebook'][SOFT_MATCHED] == remaining['codebook'][UNMATCHED] == 1
                print('PASS ' + ('same-project copy, name remapping and cleanup' if same_project else 'cross-project copy and repeat-safe publication'))
        finally:
            for project_id in reversed(ids):
                delete_atlas_project(project_id)


if __name__ == '__main__':
    main()
