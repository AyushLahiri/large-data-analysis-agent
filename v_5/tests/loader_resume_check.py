"""Play through the actual Prepare data routes with interrupted loader work."""
import json
from pathlib import Path
import tempfile
import time
import threading
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pandas as pd

from v_5.main import create_app
from v_5.main_functions.session import Session
from v_5.ingest.data_loader import functions
from v_5.ingest.data_loader.checkpoint import LoaderRunner
from v_5.ingest.data_loader.calls import LoaderCalls
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json, save_json, file_lock
from v_5.prototype_runtime import Faults
from v_5.tests.loader_fixtures import DATA, BAD_CODE, FakeAPI, local_execute

ROOT = Path(__file__).resolve().parents[2]


def wait_state(client, project, statuses=('complete', 'failed', 'stopped')):
    """Wait for the real background worker's visible state, including a question form."""
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        state = client.get(f'/api/projects/{project}').json
        if state['status'] in statuses:
            time.sleep(.05)
            return state
        time.sleep(.05)
    raise AssertionError(f'App did not reach {statuses}: {state.get("status")}')


def client_for(root):
    """Recreate the Flask app to discard its old sessions and in-memory workflow state."""
    client = create_app(root).test_client()
    return client, {'X-V5-Token': client.get('/api/status').json['token']}


def project_fixture(root):
    """Create toy uploads and an already-completed material-review handoff."""
    project = uuid4().hex
    directory = root / 'projects' / project
    uploads = directory / 'uploads'
    uploads.mkdir(parents=True)
    source = uploads / 'toy.csv'
    source.write_text(DATA, encoding='utf-8')
    files = {'toy': dict(file_id='toy', folder='toy', role='primary', format='csv',
                        filename='toy.csv', path=str(source))}
    handoff = dict(project_notes='Toy dataset', folder_notes={}, files=files,
                   findings={}, previews={}, user_clarifications=[])
    handoff_path = directory / 'runs' / 'review' / 'ingestion_handoff.json'
    save_json(handoff_path, handoff)
    state = dict(version=2, id=project, context={'name': 'Loader app test'},
                 folders={'toy': {}}, files=files, messages=[], preparations={},
                 status='ready', pending='', handoff_path=str(handoff_path))
    save_json(directory / 'project.json', state)
    return project, directory


def app_case(root, point='after_glitch_submit', unit='1', *, scenario='normal', docker=False):
    """Fail, recreate the app, and resume through the same endpoint a user clicks.

    Inputs: interruption boundary and toy scenario. Returns: None after checking the
    saved workspace, remote thread IDs, execution counts and single publication.
    """
    project, directory = project_fixture(root)
    api = FakeAPI(root / 'test_server', scenario=scenario, force_rollover=True)
    controls = root / 'test_controls'
    controls.mkdir()
    plan = [] if point in ('codebooks', 'review', 'question') else [{'point': point, 'unit': unit}]
    faults = Faults(controls, plan)
    actual_runner = LoaderRunner
    actual_docker = functions.execute_code
    review_calls, published, codebook_calls = [], [], []

    def runner(*args, **kwargs):
        """Attach only fault injection; run the production checkpoint implementation."""
        return actual_runner(*args, **kwargs, hook=faults.hit)

    def execute(code, files, workspace, current, **options):
        """Run the actual toy code while recording whether a failed script was repeated."""
        api.event('execute', failed_script=code == BAD_CODE)
        return actual_docker(code, files, workspace, current, **options) if docker else local_execute(code, workspace, current)

    def review(session, context, latest, files, workspace, review_dir, environment):
        """Represent the review boundary, reusing its stable review directory."""
        review_dir.mkdir(exist_ok=True)
        review_calls.append(str(review_dir))
        if point == 'review' and len(review_calls) == 1:
            raise TimeoutError('Review interrupted')
        return {'action': 'confirm'}

    def codebooks(session, context, latest, files, workspace, environment):
        """Check the real transition from loader recovery to codebook recovery."""
        assert 'loader_resume' not in session.state
        assert session.state['codebook_resume']['workspace'] == str(workspace)
        codebook_calls.append(str(workspace))
        if point == 'codebooks' and len(codebook_calls) == 1:
            raise TimeoutError('Codebook phase interrupted')
        return {}

    def publish(session, plan):
        """Verify one confirmed dataset is published from its original workspace."""
        published.append(plan)
        session.state['atlas'] = {'status': 'published', 'data': [{'folder': 'toy'}]}

    with (patch.object(functions, 'LoaderCalls', return_value=api),
          patch.object(functions, 'LoaderRunner', side_effect=runner),
          patch.object(functions, 'execute_code', side_effect=execute),
          patch.object(Session, 'run_code', return_value={'exit_code': 0, 'output': 'pandas and pyarrow'}),
          patch.object(Session, 'call_loader', side_effect=AssertionError('Old non-resumable loader call used')),
          patch.object(functions, 'review_prepared_data', side_effect=review),
          patch('v_5.ingest.codebook_ingestion.functions.ingest_codebooks', side_effect=codebooks),
          patch('v_5.ingest.codebook_ingestion.finalize.finalize_codebook'),
          patch('v_5.ingest.publishing.publish_atlas', side_effect=publish),
          patch.object(functions, 'write_final_summary', return_value='complete')):
        client, headers = client_for(root)
        response = client.post(f'/api/projects/{project}/start', headers=headers, json={'prepare_only': True})
        assert response.status_code == 200, response.json
        if point == 'question':
            waiting = wait_state(client, project, ('awaiting_user', 'failed'))
            assert waiting['status'] == 'awaiting_user', waiting.get('error')
            assert waiting['question_form']['questions'][0]['question'] == 'Keep the source colours?'
            response = client.post(f'/api/projects/{project}/stop', headers=headers, json={})
            assert response.status_code == 200
            failed = wait_state(client, project)
        else:
            failed = wait_state(client, project)
            assert failed['status'] == 'failed', failed.get('error')
        saved = failed.get('loader_resume') or failed.get('codebook_resume')
        workspace = Path(saved['workspace'])
        before = read_json(workspace / 'checkpoint.json')
        requests_before = read_json(api.path)['requests']
        assert before['ingestion_final_output_created']
        assert not published

        client, headers = client_for(root)
        response = client.post(f'/api/projects/{project}/start', headers=headers, json={'prepare_only': True})
        assert response.status_code == 200, response.json
        if point == 'question':
            waiting = wait_state(client, project, ('awaiting_user', 'failed'))
            assert waiting['status'] == 'awaiting_user', waiting.get('error')
            assert read_json(api.path)['requests'] == requests_before
            form = waiting['question_form']
            response = client.post(f'/api/projects/{project}/answer', headers=headers,
                                   json={'form_id': form['id'], 'answers': {'clarification': 'Yes'}})
            assert response.status_code == 200, response.json
        complete = wait_state(client, project)
        assert complete['status'] == 'complete', complete.get('error')
        assert 'loader_resume' not in complete and 'codebook_resume' not in complete
        assert len(published) == 1
        assert published[0]['datasets'][0]['workspace'] == str(workspace)
        assert published[0]['release'] == workspace.parent.parent.name
        assert read_json(workspace / 'checkpoint.json')['phase'] == 'complete'
        assert pd.read_parquet(workspace / 'toy.parquet')['id'].tolist() == [1, 2, 3]
        server = read_json(api.path)
        assert sum(event['kind'] == 'execute' for event in server['events']) == (3 if point == 'after_execute' else 2)
        glitch_calls = [r for r in server['requests'] if r['role'] == 'glitch']
        assert len({r['request']['conversation'] for r in glitch_calls}) == 1
        if point == 'after_glitch_response':
            assert len(glitch_calls) == 1
        if point == 'after_rollover_summary':
            assert sum(r['role'] == 'rollover' for r in server['requests']) == 1
        if point in ('after_complete', 'codebooks'):
            assert server['requests'] == requests_before
            assert len(review_calls) == 1
        if point == 'review':
            assert len(review_calls) == 2 and len(set(review_calls)) == 1
        assert not (workspace / 'context.json').exists()
        assert not (workspace / 'latest_payload.json').exists()
        assert not list(workspace.glob('handoff_*.json'))
    return directory, workspace


def transport_check():
    """Check real API payloads, paginated history and pending-response polling offline."""
    from v_5.ingest.data_loader.agent import LoaderReply
    from v_5.main_functions.session import loader_request
    from v_5.ingest.data_loader.agent import LOADER_PROMPT
    from unittest.mock import Mock
    def item(value):
        return SimpleNamespace(id=value['id'], model_dump=lambda: value)
    items = [item({'id': 'a', 'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'first'}]}),
             item({'id': 'b', 'type': 'reasoning', 'content': []}),
             item({'id': 'c', 'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'last'}]})]
    client, session = Mock(), Mock()
    session.lock, session.state = threading.RLock(), {}
    client.conversations.items.list.side_effect = lambda *a, **k: iter(items)
    session.count_input.return_value = 123
    calls = LoaderCalls(session, client=client)
    request = loader_request(LOADER_PROMPT, [{'role': 'user', 'content': 'next'}], LoaderReply)
    request.update(conversation='thread', background=True, store=True, max_output_tokens=8000)
    assert calls.count_input(request) == 123
    body = session.count_input.call_args.args[0]
    assert 'conversation' not in body
    assert body['input'][:3] == [{'type': 'item_reference', 'id': name} for name in ('a', 'b', 'c')]
    assert calls.messages('thread') == [{'role': 'user', 'content': 'first'}, {'role': 'assistant', 'content': 'last'}]
    client.responses.retrieve.side_effect = [SimpleNamespace(status='in_progress'),
        SimpleNamespace(id='r', status='completed', output_text='{}', usage=None)]
    with patch('v_5.app.conversations.time.sleep'):
        assert calls.retrieve('r')['status'] == 'completed'
    assert client.responses.retrieve.call_count == 2
    client.responses.create.return_value = SimpleNamespace(id='summary', status='queued')
    calls.submit('rollover', {'model': 'gpt-6-luna', 'reasoning': {'effort': 'high'}, 'input': []})
    submitted = client.responses.create.call_args.kwargs
    assert submitted['background'] and submitted['store']
    assert submitted['model'] == 'gpt-6-luna' and submitted['reasoning']['effort'] == 'high'
    assert 'max_output_tokens' not in submitted


def recovery_guards(root):
    """Exercise expired receipts, missing usage and exclusive workspace ownership."""
    from v_5.tests.loader_fixtures import build_case
    from v_5.prototype_runtime import InjectedFailure
    from v_5.ingest.data_loader.state import loader_workspace
    for scenario in ('expired', 'no_usage'):
        case = root / scenario
        plan = [{'point': 'after_input_sent', 'unit': '1'}]
        runner, api = build_case(case, plan=plan, rollover=False)
        try:
            runner.run()
        except InjectedFailure:
            pass
        runner, api = build_case(case, plan=plan, rollover=False)
        old_id = runner.state['loader']['response_id']
        original_retrieve = api.retrieve
        api.missing_response_error = LookupError
        def retrieve(response_id):
            """Replace just the old response; later requests use the ordinary fake server."""
            if response_id == old_id:
                if scenario == 'expired':
                    raise LookupError('Receipt expired')
                return {'status': 'failed', 'output_text': '', 'usage': None}
            return original_retrieve(response_id)
        api.retrieve = retrieve
        final = runner.run()
        assert final['phase'] == 'complete'
        assert final['loader']['used_tokens'] >= 8400
        assert sum(e['kind'] == 'execute' for e in read_json(api.path)['events']) == 2
    runner, api = build_case(root / 'locked')
    with file_lock(runner.root / 'loader.lock'):
        try:
            runner.run()
            raise AssertionError('A second writer entered the same checkpoint')
        except OSError:
            pass
    assert not read_json(api.path)['requests']
    session = SimpleNamespace(path=root / 'project', state={
        'loader_resume': {'folder': 'toy', 'workspace': str(root)}})
    try:
        loader_workspace(session, 'toy', root / 'unused')
        raise AssertionError('Workspace outside the project was accepted')
    except ValueError as error:
        assert 'outside' in str(error)


def run_suite(docker=False):
    """Exercise app restarts and downstream transitions without paid models or SQL writes."""
    cases = [('after_loader_submit', '1'), ('after_input_sent', '1'), ('after_execute', '1'),
             ('after_execution_result', '1'), ('after_glitch_submit', '1'), ('after_glitch_response', '1'),
             ('after_rollover_summary', '2'), ('after_rollover_handoff', '2'),
             ('after_input_sent', '2'), ('after_complete', '3'), ('review', '3'), ('codebooks', '3')]
    if docker:
        cases = [('after_glitch_submit', '1'), ('after_complete', '3')]
    with tempfile.TemporaryDirectory(prefix='_la_', dir=ROOT) as temporary:
        root = Path(temporary)
        for index, (point, unit) in enumerate(cases):
            app_case(root / str(index), point, unit, docker=docker)
            print('App recovery passed:', point, unit, flush=True)
        if not docker:
            app_case(root / 'question', 'question', scenario='clarify')
            transport_check()
            recovery_guards(root / 'guards')
    print(f'PASS: {len(cases)} app restart scenarios' + (' plus restored question form, transport, expired receipts, unknown usage and workspace guards' if not docker else ' using real Docker') + '; no paid API calls.', flush=True)


if __name__ == '__main__':
    run_suite()
