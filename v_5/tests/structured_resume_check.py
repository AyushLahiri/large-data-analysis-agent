"""Offline non-PDF checkpoint faults using real app assembly and tiny source tables."""
import json
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import pandas as pd

from v_5.ingest.codebook_ingestion import functions, tables
from v_5.ingest.codebook_ingestion.sub_agents import structured
from v_5.ingest.codebook_ingestion.sub_agents.structured_state import StructuredRunner, BUDGET
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json, save_json
from v_5.main_functions.session import Session


class Failure(RuntimeError):
    """An injected interruption; resuming creates a new runner from disk."""


def reply(action, **fields):
    """Build the real extraction contract without asking a model."""
    return {**dict(action=action, code='', table_path='', notes_path='', verification_path='',
                   message='Test extraction'), **fields}


class API:
    """Retain server-side replies across local runner recreation, like API threads."""

    missing_response_error = LookupError

    def __init__(self):
        """Start isolated fake threads, submitted requests and sandbox counters."""
        self.threads, self.requests, self.responses, self.executions = [], [], {}, []
        self.invalid_once = False

    def new_thread(self, role):
        """Create an observable conversation ID for this role."""
        key = f'{role}_{len(self.threads) + 1}'
        self.threads.append(key)
        return key

    def count_input(self, request):
        """Keep token counts small unless a test explicitly fills the saved budget."""
        return 100

    def submit(self, role, request):
        """Return bad code, corrected code, then file paths using the real payload shape."""
        self.requests.append((role, deepcopy(request)))
        payload = json.loads(request['input'][-1]['content'])
        if role == 'glitch':
            result = dict(error_kind='Python', actual_error='KeyError: label',
                          failing_lines=['1: bad code'], explanation='The named column is absent.')
        else:
            feedback = payload.get('latest_result', {})
            execution = feedback.get('execution_result')
            if execution and execution.get('exit_code') == 0:
                directory = feedback['saved_review_directory']
                result = reply('finish', table_path=directory + '/table.csv',
                    notes_path=directory + '/notes.md', verification_path=directory + '/checks.txt')
            else:
                result = reply('run', code='GOOD' if execution else 'BAD')
        key = f'response_{len(self.responses) + 1}'
        text = json.dumps(result)
        if role == 'codebook' and self.invalid_once:
            text, self.invalid_once = '{incomplete', False
        self.responses[key] = {'id': key, 'status': 'completed', 'output_text': text,
                               'usage': {'input_tokens': 100, 'output_tokens': 100}}
        return {'id': key, 'status': 'completed'}

    def retrieve(self, response_id):
        """Retrieve the same reply without submitting another agent turn."""
        return self.responses[response_id]

    def execute(self, code, current):
        """Simulate one failed script and one successful source-table export."""
        assert code in ('BAD', 'GOOD')
        self.executions.append(code)
        if code == 'GOOD':
            frame = tables.normalize_rows([{'variable_name': 'ID', 'variable_label': 'Identifier'},
                                           {'variable_name': 'extra', 'variable_label': 'Source only'}])
            frame.to_csv(current / 'table.csv', index=False)
            (current / 'notes.md').write_text('Source notes', encoding='utf-8')
            (current / 'checks.txt').write_text('Two unique variables', encoding='utf-8')
        return {'exit_code': int(code == 'BAD'), 'output': 'KeyError: label' if code == 'BAD' else 'Two rows'}


def fail_at(point, turn=None):
    """Raise once at a requested boundary; subsequent attempts can finish normally."""
    fired = []
    def hook(actual, unit):
        """Interrupt at this exact stage, optionally within one loop iteration."""
        if not fired and actual == point and (turn is None or unit == str(turn)):
            fired.append(True)
            raise Failure(point)
    return hook


def runner(root, api, hook=None):
    """Recreate the production runner using its persisted current state."""
    return StructuredRunner(root / 'codebooks' / 'source', root,
        {'source_file': {'filename': 'toy.csv'}, 'saved_turns': '/work/codebooks/source'},
        'Test extraction prompt', api, api.execute, hook=hook)


def must_fail(call):
    """Require an injected interruption rather than accidentally passing a fault test."""
    try:
        call()
    except Failure:
        return
    raise AssertionError('The requested fault was not reached.')


def recovery_faults(root):
    """Retry real stage boundaries and verify saved outputs prevent duplicate work."""
    points = [('after_submit', 1), ('after_input_sent', 1), ('after_retrieve', 1),
              ('after_reply', 1), ('after_execute', 1), ('after_execution_result', 1),
              ('after_glitch_submit', 1), ('after_glitch_response', 1), ('after_advance', 2),
              ('after_normalized', 3), ('after_notes', 3), ('after_verification', 3)]
    for point, turn in points:
        case, api = root / point, API()
        first = runner(case, api, fail_at(point, turn))
        must_fail(first.run)
        saved = read_json(first.path)
        if point in ('after_normalized', 'after_notes', 'after_verification'):
            assert not saved['complete'] and saved['reply']['action'] == 'finish'
            (first.root / 'normalized.csv').write_text('partial export', encoding='utf-8')
        result = runner(case, api).run()
        assert list(result.variable_name) == ['id', 'extra'], point
        assert api.executions.count('BAD') == (2 if point == 'after_execute' else 1), point
        assert api.executions.count('GOOD') == 1, point
        code_calls = [request for role, request in api.requests if role == 'codebook']
        assert len(code_calls) == (4 if point == 'after_submit' else 3), point
        assert len({request['conversation'] for request in code_calls}) == 1
        if point == 'after_submit':
            assert saved['input_sent'] is None and 'could not recover' in code_calls[1]['input'][1]['content']
        glitch = [request for role, request in api.requests if role == 'glitch']
        assert len(glitch) == (2 if point == 'after_glitch_submit' else 1), point
        assert len({request['conversation'] for request in glitch}) == 1
        if len(glitch) == 2:
            assert 'no saved diagnosis' in glitch[1]['input'][1]['content']
        counts = len(api.requests), len(api.executions)
        runner(case, api).run()
        assert counts == (len(api.requests), len(api.executions)), 'Completed source reran'
        assert read_json(first.path)['complete']
        assert not list(first.root.rglob('*conversation*.json'))
        assert all(request['model'] == 'gpt-6.1-sol' and request['reasoning']['effort'] == 'medium'
                   for _, request in api.requests)
    print(f'PASS {len(points)} extraction interruption/retry boundaries')


def thread_branches(root):
    """Check rollover recovery, expired receipts and malformed reply handling."""
    api = API()
    first = runner(root / 'rollover', api, fail_at('after_advance', 2))
    must_fail(first.run)
    saved = read_json(first.path)
    pending = deepcopy(saved['current_input'])
    saved['used_tokens'] = BUDGET
    save_json(first.path, saved)
    must_fail(runner(root / 'rollover', api, fail_at('after_rollover')).run)
    rolled = read_json(first.path)
    assert rolled['current_input'] == pending and rolled['thread_id'] != saved['thread_id']
    runner(root / 'rollover', api).run()
    assert api.executions == ['BAD', 'GOOD']
    assert len([name for name in api.threads if name.startswith('codebook')]) == 2

    api = API()
    first = runner(root / 'expired', api, fail_at('after_input_sent', 1))
    must_fail(first.run)
    del api.responses[read_json(first.path)['response_id']]
    runner(root / 'expired', api).run()
    calls = [request for role, request in api.requests if role == 'codebook']
    assert calls[0]['conversation'] == calls[1]['conversation']
    assert 'could not recover' in calls[1]['input'][1]['content']
    assert api.executions == ['BAD', 'GOOD']

    api = API()
    api.invalid_once = True
    runner(root / 'invalid', api).run()
    assert api.executions == ['BAD', 'GOOD']

    api = API()
    stopped = []
    original = api.retrieve
    def receive_and_stop(response_id):
        """Model completion can arrive after Stop; its reply must be saved first."""
        result = original(response_id)
        stopped.append(True)
        return result
    def check_stop():
        """Interrupt the runner after its pending model response has arrived."""
        if stopped:
            raise InterruptedError('Stopped')
    first = runner(root / 'stop', api)
    first.check_stop = check_stop
    with patch.object(api, 'retrieve', side_effect=receive_and_stop):
        try:
            first.run()
        except InterruptedError:
            pass
        else:
            raise AssertionError('Stop was ignored')
    assert read_json(first.path)['agent_returned'] and not api.executions
    runner(root / 'stop', api).run()
    assert len([role for role, _ in api.requests if role == 'codebook']) == 3
    assert api.executions == ['BAD', 'GOOD']
    print('PASS rollover, expired response, malformed reply and Stop/retry branches')


def fixture(root):
    """Create an isolated app session with two data variables and one codebook upload."""
    workspace = root / 'work'
    workspace.mkdir(parents=True)
    pd.DataFrame({'id': [1, 2], 'age': [20, 30]}).to_parquet(workspace / 'data.parquet')
    save_json(workspace / 'manifest.json', {'tables': [{'name': 'data', 'path': '/work/data.parquet',
        'columns': {'id': 'int', 'age': 'int'}}]})
    source = root / 'toy.csv'
    source.write_text('variable,label\nid,Identifier\nextra,Source only\n', encoding='utf-8')
    file = dict(file_id='source', filename='toy.csv', path=str(source), format='csv', asset_kind='codebook')
    files = {'source': file}
    session = Session(root / 'project', {'messages': [], 'files': files})
    context = {'folder': 'toy', 'files': files, 'ingestion_notes': {}}
    prepared = {'manifest_path': '/work/manifest.json'}
    return session, context, prepared, files, workspace


def app_assembly(root):
    """Fail real app exports/joins, reload project state, and check overwritten assets."""
    for boundary in ('export', 'source', 'combined', 'unmatched'):
        session, context, prepared, files, workspace = fixture(root / boundary)
        api = API()
        real_runner, real_save = StructuredRunner, tables.save_csv
        fault = fail_at('after_verification', 3) if boundary == 'export' else None

        def make_runner(*args, **kwargs):
            """Inject one interrupted final export through the real production wrapper."""
            return real_runner(*args, **kwargs, hook=fault)

        def sandbox(code, _files, _workspace, current, **kwargs):
            """Keep production sandbox arguments intact while executing fixed toy results."""
            assert kwargs['read_only'] and kwargs['cancelled'] is session.cancelled
            return api.execute(code, current)

        def save(path, frame):
            """Fail after writing a cheap asset but before the shared completion flag."""
            real_save(path, frame)
            names = {'source': 'toy_codebook.csv', 'combined': 'combined_codebook.csv',
                     'unmatched': 'unmatched_variables.csv'}
            if Path(path).name == names.get(boundary):
                raise Failure(boundary)

        def ingest():
            """Call the same entry point used by the folder preparation workflow."""
            return functions.ingest_codebooks(session, context, prepared, files, workspace, 'test packages')

        with patch.object(structured, 'StructuredCalls', return_value=api), \
                patch.object(structured, 'run_code', side_effect=sandbox), \
                patch.object(structured, 'StructuredRunner', side_effect=make_runner), \
                patch.object(tables, 'save_csv', side_effect=save):
            must_fail(ingest)
        state = read_json(session.state_path)
        assert state['codebooks']['toy']['merged_with_master'] is False
        count = len(api.requests)
        # All files may already exist, but the False flag still requires a full rewrite.
        derived = workspace / 'codebooks' / 'toy_codebook.csv'
        if derived.exists():
            derived.write_text('partial asset', encoding='utf-8')
        session = Session(session.path, state)
        with patch.object(structured, 'StructuredCalls', return_value=api), \
                patch.object(structured, 'run_code', side_effect=sandbox):
            book = ingest()
        assert len(api.requests) == count and api.executions == ['BAD', 'GOOD']
        assert book['merged_with_master'] and read_json(session.state_path)['codebooks']['toy']['merged_with_master']
        combined = pd.read_csv(book['combined'])
        unmatched = pd.read_csv(book['unmatched'])
        assert combined.variable_name.tolist() == ['id', 'age']
        assert combined.loc[0, 'variable_label_toy'] == 'Identifier'
        assert combined.loc[1, 'source_presence_toy'] == 'not in source'
        assert unmatched.variable_name.tolist() == ['extra']
        assert pd.read_csv(derived).variable_name.tolist() == ['id', 'extra']
        assert not (derived.parent / 'toy_toy_codebook.csv').exists()
        with patch.object(functions, 'create_master', side_effect=AssertionError('Completed merge repeated')), \
                patch.object(functions, 'structured_codebook', side_effect=AssertionError('Completed reader repeated')):
            assert ingest() == book
    print('PASS 4 real-app export/assembly failures; one completion flag and stable output paths')


def run():
    """Run focused checkpoint checks with no API, Docker, SQL or user-project changes."""
    with tempfile.TemporaryDirectory(prefix='structured_resume_') as temporary:
        root = Path(temporary)
        recovery_faults(root / 'recovery')
        thread_branches(root / 'threads')
        app_assembly(root / 'app')
    print('PASS non-PDF checkpoints and assembly recovery; no paid calls')


if __name__ == '__main__':
    run()
