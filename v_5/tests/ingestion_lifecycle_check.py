"""Exercise stop/retry, fresh starts and folder removal without paid model calls."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from v_5.app.conversations import ConversationCalls
from v_5.ingest.lifecycle import RETRY_MESSAGE, reset_ingestion
from v_5.main_functions.assets import archive_project
from v_5.main_functions.session import write_json
from v_5.tests.delete_project_check import client_for
from v_5.tests.reuse_check import DATA, ROOT, prepared_fixture, project


def ready_project(root, number=1, folders=('first',)):
    """Seed answered forms using the same persisted shape as the folder wizard."""
    path, state = project(root, number, [(folder, folder + '.csv', DATA, 'primary', '') for folder in folders])
    for folder in state['folders'].values():
        folder['setup_reviewed'] = True
    archive_project(path, state)
    write_json(path / 'project.json', state)
    return path, state


def wait_done(jobs, project_id):
    """Wait until launch has saved the final status and released the active worker."""
    deadline = time.monotonic() + 10
    while project_id in jobs['sessions']:
        if time.monotonic() > deadline:
            raise AssertionError('Test ingestion worker did not stop.')
        time.sleep(.01)


def folder_deletion(root):
    """Delete submitted folders before ingestion, including the first and last folder."""
    path, state = ready_project(root, folders=('first', 'second'))
    client, headers, _ = client_for(root)
    base = f'/api/projects/{state["id"]}'
    for folder, remaining in [('first', ['second']), ('second', [])]:
        response = client.post(base + '/delete-folder', json={'folder': folder, 'confirmation': 'delete'}, headers=headers)
        assert response.status_code == 200, response.json
        saved = client.get(base).json
        assert list(saved['folders']) == remaining
        assert all(file['folder'] in remaining for file in saved['files'].values())
        archive = root / 'users' / '9999' / 'projects' / state['id']
        assert list(json.loads((archive / 'assets.json').read_text())['folders']) == remaining
        assert len(list((path / 'uploads').iterdir())) == len(remaining)
        assert len(list((archive / 'raw').iterdir())) == len(remaining)
    assert not saved.get('active_folder') and saved['status'] == 'draft'


def stop_retry_and_edits(root):
    """Use actual routes to wait for a response, resume checkpoints and reject changed inputs."""
    path, state = ready_project(root)
    client, headers, jobs = client_for(root)
    base = f'/api/projects/{state["id"]}'
    entered, release = threading.Event(), threading.Event()
    starts = []

    def workflow(session):
        """Replace only model work; preserve the real Session and launch lifecycle."""
        starts.append((deepcopy(session.state), session.handoff))
        if len(starts) == 1:
            handoff = session.run_path / 'ingestion_handoff.json'
            checkpoint = session.run_path / 'loader' / 'checkpoint.json'
            write_json(handoff, {'saved': 'material review'})
            session.state.update(handoff_path=str(handoff), loader_resume={'checkpoint': str(checkpoint)})
            session.save()
            entered.set()
            assert release.wait(10), 'Test reply was not released.'
            write_json(checkpoint, {'reply': 'finished response', 'code_ran': False})
            session.message('data_loader', 'Saved the in-flight reply.')
            session.check_stop()
            raise AssertionError('Stop allowed the next workflow action.')
        return 'blocked'

    with patch('v_5.main.run_project', workflow):
        assert client.post(base + '/start', json={}, headers=headers).status_code == 200
        assert entered.wait(5)
        try:
            assert client.post(base + '/stop', json={}, headers=headers).status_code == 200
            assert client.get(base).json['status'] == 'stopping'
            assert client.post(base + '/context', json={'context': {'description': 'Too early'}}, headers=headers).status_code == 400
            assert client.post(base + '/delete-folder', json={'folder': 'first', 'confirmation': 'delete'}, headers=headers).status_code == 400
            assert client.post(base + '/start', json={'retry': True}, headers=headers).status_code == 400
        finally:
            release.set()
        wait_done(jobs, state['id'])
        stopped = client.get(base).json
        assert stopped['status'] == 'stopped' and stopped['messages']
        old_run = Path(stopped['log_directory'])
        checkpoint = Path(stopped['loader_resume']['checkpoint'])
        assert json.loads(checkpoint.read_text()) == {'reply': 'finished response', 'code_ran': False}

        # Saving identical text must not invalidate or discard the handoff.
        assert client.post(base + '/context', json={'context': {'description': stopped['context']['description']}}, headers=headers).status_code == 200
        assert not client.get(base).json.get('ingestion_changed')
        assert client.post(base + '/start', json={'retry': True}, headers=headers).status_code == 200
        wait_done(jobs, state['id'])
        assert starts[1][1] == {'saved': 'material review'}
        assert starts[1][0]['messages'] == stopped['messages'] and checkpoint.exists()
        assert starts[1][0]['loader_resume'] == stopped['loader_resume']

        response = client.post(base + '/context', json={'context': {}, 'files': {'file_001': 'Changed file description'}}, headers=headers)
        assert response.status_code == 200, response.json
        assert client.get(base).json['ingestion_changed']
        rejected = client.post(base + '/start', json={'retry': True}, headers=headers)
        assert rejected.status_code == 400 and rejected.json['error'] == RETRY_MESSAGE
        assert client.post(base + '/publish', json={}, headers=headers).status_code == 400

        assert client.post(base + '/start', json={}, headers=headers).status_code == 200
        wait_done(jobs, state['id'])
        fresh, handoff = starts[2]
        assert handoff is None and fresh['messages'] == []
        assert not any(key in fresh for key in ('loader_resume', 'handoff_path', 'ingestion_changed'))
        assert not old_run.exists() and not checkpoint.exists() and not (path / 'inputs.jsonl').exists()
        assert fresh['files']['file_001']['note'] == 'Changed file description'
        assert Path(fresh['files']['file_001']['path']).is_file()


def restarted_server(root):
    """A stale running state permits edits after restart but still blocks an unsafe retry."""
    path, state = ready_project(root)
    state.update(status='stopping', log_directory=str(path / 'runs' / 'previous'))
    write_json(path / 'project.json', state)
    client, headers, _ = client_for(root)
    base = f'/api/projects/{state["id"]}'
    assert client.get(base).json['status'] == 'interrupted'
    response = client.post(base + '/rename-folder', json={'folder': 'first', 'name': 'Updated folder'}, headers=headers)
    assert response.status_code == 200, response.json
    assert client.get(base).json['ingestion_changed']
    assert client.post(base + '/start', json={'retry': True}, headers=headers).status_code == 400


def fresh_cleanup(root):
    """Remove old run/chat assets while preserving originals and published table workspaces."""
    path, state = ready_project(root, folders=('published', 'pending'))
    state['atlas'] = {'data': [{'folder': 'published'}]}
    prepared_fixture(path, state, 'published')
    kept = Path(state['codebooks']['published']['final']).parent.parent
    checkpoint = kept.parent / 'pending' / 'checkpoint.json'
    write_json(checkpoint, {'old': True})
    write_json(path / 'analysis' / 'keep.json', {'chat': 'analysis'})
    state.update(log_directory=str(path / 'runs' / 'old'), messages=[{'text': 'Old chat'}],
                 loader_resume={'workspace': str(checkpoint.parent)}, codebook_resume={'folder': 'pending'})
    write_json(path / 'runs' / 'old' / 'event.json', {'old': True})
    write_json(path / 'project.json', state)
    reset_ingestion(path, state)
    assert kept.is_dir() and Path(state['codebooks']['published']['final']).is_file()
    assert not checkpoint.exists() and not (path / 'runs' / 'old').exists()
    assert state['messages'] == [] and 'loader_resume' not in state and 'codebook_resume' not in state
    assert (path / 'analysis' / 'keep.json').is_file()
    assert all(Path(file['path']).is_file() for file in state['files'].values())
    assert state['atlas']['data'] == [{'folder': 'published'}]


def response_draining(root):
    """An accepted request drains to completion on Stop; new requests remain blocked."""
    cancelled = threading.Event()
    calls = []

    def check_stop():
        if cancelled.is_set():
            raise InterruptedError('Stopped')

    def retrieve(response_id):
        calls.append(response_id)
        cancelled.set()
        return SimpleNamespace(id=response_id, status='in_progress' if len(calls) == 1 else 'completed',
                               output_text='Saved response', usage=None)

    session = SimpleNamespace(analysis=False, check_stop=check_stop)
    transport = ConversationCalls(session, client=SimpleNamespace(responses=SimpleNamespace(retrieve=retrieve)))
    with patch('v_5.app.conversations.time.sleep'):
        response = transport.retrieve('response_001')
    assert response['status'] == 'completed' and len(calls) == 2
    try:
        transport.new_thread()
        raise AssertionError('A new agent started after Stop.')
    except InterruptedError:
        pass


def checkpoint_stops(root):
    """Stop loader/descriptives after receiving a reply, then reuse it without another call."""
    from v_5.tests.loader_fixtures import build_case as loader_case
    from v_5.tests.descriptives_resume_check import build_case as review_case
    for label, build in [('loader', loader_case), ('descriptives', review_case)]:
        directory = root / label
        runner, provider = build(directory)
        stopped = threading.Event()
        original_retrieve = provider.retrieve

        def receive(response_id):
            response = original_retrieve(response_id)
            stopped.set()
            return response

        def check_stop():
            if stopped.is_set():
                raise InterruptedError('Test Stop')

        runner.check_stop = check_stop
        with patch.object(provider, 'retrieve', receive):
            try:
                runner.run()
                raise AssertionError('A returned reply did not stop before execution.')
            except InterruptedError:
                pass
        saved = json.loads(runner.path.read_text())
        payload = saved['loader'] if label == 'loader' else saved
        assert payload['reply'] and not payload['code_ran']
        before = json.loads(provider.path.read_text())['requests']
        resumed, provider = build(directory)

        def execute(*args):
            assert json.loads(provider.path.read_text())['requests'] == before
            raise InterruptedError('Reached saved code without another model call')

        resumed.execute = execute
        try:
            resumed.run()
            raise AssertionError('Saved code was not reached.')
        except InterruptedError as error:
            assert str(error) == 'Reached saved code without another model call'


def pdf_checkpoint_stop(root):
    """A codebook reader saves an accepted reply before Stop and reuses it on Retry."""
    from v_5.prototype_data import PAGES
    from v_5.prototype_runtime import FakeProvider
    from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import Checkpoints, new_agent
    from v_5.ingest.codebook_ingestion.sub_agents.agent import PdfCodebookReply
    provider = FakeProvider(root)
    checkpoint = Checkpoints(root / 'work', provider)
    checkpoint.commit((), {'readers': {'reader_001': new_agent('reader_001')}})
    stopped = threading.Event()
    original_retrieve = provider.retrieve

    def receive(response_id):
        response = original_retrieve(response_id)
        stopped.set()
        return response

    def check_stop():
        if stopped.is_set():
            raise InterruptedError('Test Stop')

    args = (('readers', 'reader_001'), 'page_1', 'reader', 'Read the toy string.',
            {'current_page': PAGES[0]}, PdfCodebookReply)
    with patch.object(provider, 'retrieve', receive), patch.object(provider, 'check_stop', check_stop):
        try:
            checkpoint.invoke(*args)
            raise AssertionError('Reader did not stop after saving its reply.')
        except InterruptedError:
            pass
    assert checkpoint.get(('readers', 'reader_001'))['output_received'] == 1
    with patch.object(provider, 'submit', side_effect=AssertionError('Must reuse saved output')):
        assert Checkpoints(root / 'work', provider).invoke(*args)['rows']


def main():
    """Run a limited set of lifecycle checks against isolated, automatically removed projects."""
    with tempfile.TemporaryDirectory(prefix='_ingest_lifecycle_', dir=ROOT) as temporary:
        checks = (folder_deletion, stop_retry_and_edits, restarted_server, fresh_cleanup,
                  response_draining, checkpoint_stops, pdf_checkpoint_stop)
        for check in checks:
            check(Path(temporary) / check.__name__)
            print('PASS', check.__name__, flush=True)
    print(f'{len(checks)} ingestion lifecycle checks passed; no model calls.', flush=True)


if __name__ == '__main__':
    main()
