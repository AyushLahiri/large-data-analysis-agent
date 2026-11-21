"""Exercise PDF recovery through the real Prepare data route, without paid calls."""
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch
from uuid import uuid4
import pandas as pd

from v_5.main import create_app
from v_5.main_functions.session import Session
from v_5.prototype_data import DOCUMENT
from v_5.prototype_runtime import FakeProvider, Faults, read_json, save_json
from v_5.ingest.codebook_ingestion.sub_agents import pdf
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import Checkpoints, SESSION_BUDGET
from v_5.ingest.codebook_ingestion.sub_agents.agent import PdfCodebookReply
from v_5.ingest.codebook_ingestion.functions import saved_codebook_inputs


def wait_finished(client, project_id):
    """Wait for the app's short offline worker to finish and return its saved state."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        state = client.get(f'/api/projects/{project_id}').json
        if state['status'] in ('complete', 'failed', 'stopped'):
            # The final status can be visible just before launch releases its active entry.
            time.sleep(.05)
            return state
        time.sleep(.05)
    raise AssertionError('Offline preparation did not finish within 30 seconds')


def check_app_retry(root):
    """Fail inside a child merger, recreate the app, then use Prepare data to resume.

    The loader, review, Docker and publication are controlled at their external boundaries;
    the actual route, saved-workspace handling, source assembly and PDF pipeline all run.
    """
    project_id = uuid4().hex
    project = root / 'projects' / project_id
    workspace = project / 'runs' / 'confirmed_run' / 'loader' / 'folder_001'
    workspace.mkdir(parents=True)
    uploads = project / 'uploads'
    uploads.mkdir()
    (uploads / 'data.csv').write_text('colour,size\nred,small\n', encoding='utf-8')
    (uploads / 'book.txt').write_text('Strings supplied by the test reader.', encoding='utf-8')
    pd.DataFrame({'colour': ['red'], 'size': ['small']}).to_parquet(workspace / 'data.parquet')
    manifest = {'tables': [{'name': 'data', 'path': '/work/data.parquet', 'row_count': 1,
        'primary_key': ['colour'], 'columns': [{'name': name, 'original_name': name,
        'type': 'character', 'sql_definition': 'TEXT'} for name in ('colour', 'size')]}]}
    save_json(workspace / 'manifest.json', manifest)
    prepared = {'manifest_path': '/work/manifest.json'}
    files = {
        'data': {'file_id': 'data', 'folder': 'Survey', 'role': 'primary', 'format': 'csv',
                 'filename': 'data.csv', 'path': str(uploads / 'data.csv')},
        'book': {'file_id': 'book', 'folder': 'Survey', 'role': 'asset', 'asset_kind': 'codebook',
                 'format': 'pdf', 'filename': 'book.pdf', 'path': str(uploads / 'book.txt')},
    }
    context = {'folder': 'Survey', 'files': files, 'ingestion_notes': {'primary_codebook': 'book.pdf'}}
    save_json(workspace / 'context.json', context)
    save_json(workspace / 'latest_payload.json', prepared)
    handoff = {'project_notes': '', 'folder_notes': {}, 'files': files,
               'findings': {}, 'previews': {}, 'user_clarifications': []}
    handoff_path = project / 'runs' / 'confirmed_run' / 'ingestion_handoff.json'
    save_json(handoff_path, handoff)
    state = {'version': 2, 'id': project_id, 'context': {'name': 'Resume test'},
             'folders': {'Survey': {}}, 'files': files, 'messages': [], 'status': 'failed',
             'pending': '', 'preparations': {'Survey': prepared}, 'handoff_path': str(handoff_path),
             'codebook_resume': {'folder': 'Survey', 'workspace': str(workspace)}}
    save_json(project / 'project.json', state)
    server = root / 'test_server'
    faults = Faults(root / 'test_controls', [
        {'point': 'after_output_save', 'unit': 'merge_b01_p003', 'action': 'raise'}])
    real_pdf = pdf.pdf_codebook

    def source(session, file, directory):
        """Supply only the fake API and failure hook; run the real production pipeline."""
        return real_pdf(session, file, directory, provider=FakeProvider(server), hook=faults.hit)

    def publish(session, plan):
        """Verify the original release/workspace is published only after recovery."""
        assert plan['release'] == 'confirmed_run'
        assert Path(plan['datasets'][0]['workspace']) == workspace
        assert plan['next_folder'] is None
        session.state['atlas'] = {'status': 'published', 'data': [{'folder': 'Survey'}]}

    with (patch.object(Session, 'run_code', return_value={'exit_code': 0, 'output': 'test packages'}),
          patch.object(Session, 'call_loader', side_effect=AssertionError('Loader was restarted')),
          patch.object(Session, 'answer', side_effect=AssertionError('User review was repeated')),
          patch.object(pdf, 'read_pdf', return_value=DOCUMENT),
          patch('v_5.ingest.codebook_ingestion.functions.pdf_codebook', side_effect=source),
          patch('v_5.ingest.codebook_ingestion.finalize.finalize_codebook') as finalize,
          patch('v_5.ingest.publishing.publish_atlas', side_effect=publish) as publication,
          patch('v_5.ingest.data_loader.functions.write_final_summary', return_value='complete')):
        app = create_app(root)
        client = app.test_client()
        headers = {'X-V5-Token': client.get('/api/status').json['token']}
        result = client.post(f'/api/projects/{project_id}/start', headers=headers, json={'prepare_only': True})
        assert result.status_code == 200, result.json
        failed = wait_finished(client, project_id)
        assert failed['status'] == 'failed'
        assert failed['codebook_resume']['workspace'] == str(workspace)
        finalize.assert_not_called()
        publication.assert_not_called()
        checkpoint_path = workspace / 'codebooks' / 'book' / 'checkpoint.json'
        assert checkpoint_path.is_file(), failed['messages'][-3:]
        before = read_json(checkpoint_path)
        child = before['readers']['reader_001']['sub_agent']
        assert child['output_received'] == 1 and child['table_created'] == 0
        assert 'merged' not in child
        old_ids = {p.name for p in (server / 'simulated_api').glob('resp_*.json')}

        # A fresh Flask app proves recovery comes from disk, not the old Session object.
        client = create_app(root).test_client()
        headers = {'X-V5-Token': client.get('/api/status').json['token']}
        result = client.post(f'/api/projects/{project_id}/start', headers=headers, json={'prepare_only': True})
        assert result.status_code == 200, result.json
        complete = wait_finished(client, project_id)
        assert complete['status'] == 'complete', complete['messages'][-3:]
        assert 'codebook_resume' not in complete
        finalize.assert_called_once()
        publication.assert_called_once()
        after = read_json(checkpoint_path)
        assert after['phase'] == 'complete'
        assert all('merged' not in r['sub_agent'] for r in after['readers'].values())
        for path in (server / 'simulated_api').glob('resp_*.json'):
            if path.name in old_ids:
                continue
            payload = json.loads(read_json(path)['request']['input'][0]['content'])
            if 'current_page' in payload:
                page = payload['current_page']['page']
                reader = before['readers'][f'reader_{(page - 1) // 10 + 1:03d}']
                assert page >= reader['current_pages'][0]
        frame = pd.read_csv(workspace / 'codebooks' / 'book' / 'normalized.csv', keep_default_na=False)
        assert len(frame) == 6
        assert all(f'F{n:02d}' in '\n'.join(frame.variable_description) for n in range(1, 21))
        notes = (workspace / 'codebooks' / 'book' / 'general_information.md').read_text(encoding='utf-8')
        assert all(notes.splitlines().count(f'N{n:02d}') == 1 for n in range(1, 21))
    # The pointer cannot be used to read an unrelated path.
    session = Session(project, {**state, 'codebook_resume': {'folder': 'Survey', 'workspace': str(root)}})
    try:
        saved_codebook_inputs(session, 'Survey')
    except ValueError:
        pass
    else:
        raise AssertionError('An external workspace was accepted')


def check_budget_and_expired_response(root):
    """Exercise production token limits and an API response that is no longer stored."""
    from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import new_agent
    provider = FakeProvider(root)
    checkpoint = Checkpoints(root / 'source', provider)
    checkpoint.commit((), {'readers': {'reader_001': new_agent('reader_001')}})
    path = ('readers', 'reader_001')
    checkpoint.commit(path, {'used_tokens': SESSION_BUDGET - 100})
    payload = {'current_page': DOCUMENT['pages'][0]}
    try:
        checkpoint.invoke(path, 'page1', 'reader', 'prompt', payload, PdfCodebookReply)
    except RuntimeError as error:
        assert f'{SESSION_BUDGET:,}-token' in str(error)
    else:
        raise AssertionError('Over-budget call was allowed')
    assert not list(provider.root.glob('resp_*.json'))
    checkpoint.commit(path, {'used_tokens': 5000, 'reserved_tokens': 1000,
                             'sent': 1, 'response_id': 'expired'})
    try:
        checkpoint.invoke(path, 'page1', 'reader', 'prompt', payload, PdfCodebookReply)
    except FileNotFoundError:
        pass
    else:
        raise AssertionError('Missing response was silently accepted')
    assert checkpoint.get(path)['sent'] == 0
    assert checkpoint.get(path)['used_tokens'] == 5000
    checkpoint.invoke(path, 'page1', 'reader', 'prompt', payload, PdfCodebookReply)
    assert checkpoint.get(path)['output_received'] == 1


def check_token_count_request():
    """Count paginated conversation references without attaching another live request."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from v_5.ingest.codebook_ingestion.sub_agents.pdf_calls import PdfCalls
    items = Mock(return_value=iter([SimpleNamespace(id='old_input'), SimpleNamespace(id='old_output')]))
    count = Mock(return_value=345)
    client = SimpleNamespace(conversations=SimpleNamespace(items=SimpleNamespace(list=items)))
    session = SimpleNamespace(check_stop=lambda: None, count_input=count)
    provider = PdfCalls(session, client=client)
    request = {'model': provider.model, 'conversation': 'conversation_test', 'instructions': 'Read',
               'input': [{'role': 'user', 'content': 'Current page'}],
               'background': True, 'store': True, 'max_output_tokens': 16000}
    assert provider.count_input(request, 'reader') == 345
    body = count.call_args.args[0]
    assert 'conversation' not in body and 'background' not in body and 'store' not in body
    assert body['input'] == [{'type': 'item_reference', 'id': 'old_input'},
                             {'type': 'item_reference', 'id': 'old_output'}, *request['input']]
    assert request['conversation'] == 'conversation_test' and len(request['input']) == 1
    items.assert_called_once_with('conversation_test', order='asc', limit=100)


def check_single_block_merge(root):
    """A reader sends all 35 repeated variables in one call, with no final batch needed."""
    from v_5.prototype_runtime import oracle
    class ManyRows(FakeProvider):
        def __init__(self, directory):
            super().__init__(directory)
            self.merge_sizes = []

        def submit(self, request, name):
            payload = json.loads(request['input'][0]['content'])
            if 'current_page' in payload:
                page = payload['current_page']['page']
                template = oracle({'current_page': {'page': page,
                    'markdown': f'variable | Variable | Fact {page}'}}, 'reader')['rows'][0]
                result = {'rows': [dict(template, variable_name=f'v{i}') for i in range(35)],
                          'general_information': [], 'unfinished_entry': '', 'issues': []}
            else:
                assert 'existing_rows' in payload, 'A single block must not need cross-block reconciliation'
                self.merge_sizes.append(len(payload['existing_rows']))
                result = oracle(payload, 'merge')
            response_id = 'resp_fake_' + uuid4().hex
            save_json(self.root / (response_id + '.json'), {'request': request, 'response': {
                'id': response_id, 'status': 'completed', 'output_text': json.dumps(result),
                'usage': {'input_tokens': 100, 'output_tokens': 100}}})
            return {'id': response_id, 'status': 'queued'}
    provider = ManyRows(root / 'server')
    document = {'page_count': 2, 'pages': DOCUMENT['pages'][:2]}
    with patch.object(pdf, 'read_pdf', return_value=document):
        frame = pdf.pdf_codebook(None, {'filename': 'book.pdf', 'path': 'strings'},
                                 root / 'source', provider=provider)
    assert len(frame) == 35 and provider.merge_sizes == [35]
    state = read_json(root / 'source' / 'checkpoint.json')
    assert set(state['reconciler']) == {'master_table'}
    child = state['readers']['reader_001']['sub_agent']
    assert 'merged' not in child and 'current_rows' not in child
    assert state['readers']['reader_001']['merged'] == 1


def run():
    """Run app-boundary recovery and provider-lifetime checks in temporary projects."""
    # The Windows sandbox temp root is long enough to exceed legacy event-path limits.
    with tempfile.TemporaryDirectory(prefix='_pdf_', dir=Path(__file__).resolve().parents[2]) as temporary:
        root = Path(temporary)
        check_app_retry(root / 'runtime')
        check_budget_and_expired_response(root / 'budget')
        check_token_count_request()
        check_single_block_merge(root / 'single_block')
    print('PASS: actual Prepare data retry after app restart, child recovery, saved workspace, '
          'no loader/review replay, single publication, token cap and expired-response retry. No paid calls.')


if __name__ == '__main__':
    run()
