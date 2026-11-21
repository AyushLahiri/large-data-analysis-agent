"""Break the isolated loader with real process death; production code stays unchanged."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pandas as pd

from v_5.loader_prototype import SESSION_TOKENS, OUTPUT_TOKENS
from v_5.tests.loader_fixtures import BAD_CODE, GOOD_CODE, FakeAPI, build_case, context, local_execute
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import atomic_text, read_json

ROOT = Path(__file__).resolve().parents[2]
KILLS = [
    ('before_ingestion', '0'), ('after_ingestion', '0'),
    ('before_ingestion_output', '1'), ('after_ingestion_output', '1'),
    ('after_loader_thread', '1'), ('before_loader_submit', '1'),
    ('after_loader_submit', '1'), ('after_input_sent', '1'),
    ('after_loader_retrieve', '1'), ('before_loader_reply', '1'),
    ('after_loader_reply', '1'), ('after_code_save', '1'),
    ('before_execute', '1'), ('after_execute', '1'),
    ('before_execution_result', '1'), ('after_execution_result', '1'),
    ('after_glitch_thread', '1'), ('before_glitch_submit', '1'),
    ('after_glitch_submit', '1'), ('after_glitch_retrieve', '1'),
    ('before_glitch_response', '1'), ('after_glitch_response', '1'),
    ('before_advance', '2'), ('after_advance', '2'),
    ('after_rollover_start', '2'), ('before_summary_submit', '2'),
    ('after_summary_submit', '2'), ('after_summary_retrieve', '2'),
    ('after_rollover_summary', '2'), ('after_rollover_thread', '2'),
    ('after_rollover_handoff', '2'), ('after_input_sent', '2'),
    ('after_loader_reply', '2'), ('after_execute', '2'),
    ('after_execution_result', '2'), ('after_advance', '3'),
    ('after_review_ready', '3'), ('after_review', '3'), ('after_complete', '3'),
]


def verify(root, stopped=False):
    """Check real artifacts, current-only checkpoint fields and model/contract settings."""
    state = read_json(root / 'work' / 'checkpoint.json')
    assert state['phase'] == ('stopped' if stopped else 'complete'), state['phase']
    server = read_json(root / 'test_server' / 'server.json')
    if not stopped:
        actual = pd.read_parquet(root / 'work' / 'toy.parquet')
        assert actual.to_dict('list') == {'id': [1, 2, 3], 'colour': ['red', 'blue', 'red'], 'score': [10, 20, 30]}
        assert actual['id'].is_unique
        for name in ('manifest.json', 'load.py', 'preparation_notes.md'):
            assert (root / 'work' / name).is_file(), name
    assert set(state) == {'phase', 'ingestion_final_output_created', 'context', 'loader', 'rollover_summary'}
    assert 'history' not in state['loader'] and 'applied_batches' not in json.dumps(state)
    for call in server['requests']:
        request = call['request']
        if call['role'] == 'rollover':
            assert request['model'] == 'gpt-6-luna' and request['reasoning']['effort'] == 'high'
        else:
            assert request['model'] == 'gpt-6.2-sol' and request['reasoning']['effort'] == 'medium'
            assert request['max_output_tokens'] == OUTPUT_TOKENS
            assert request['text']['format']['name'] == ('LoaderReply' if call['role'] == 'loader' else 'GlitchReply')
    return state, server


def worker(root, scenario, plan, docker=False):
    """Start a fresh worker; injected os._exit cannot be caught by the application."""
    runner, _ = build_case(root, scenario=scenario, plan=plan, docker=docker)
    runner.run()


def launch(root, scenario, plan, docker=False):
    """Launch one worker process using only the isolated prototype entry point."""
    command = [sys.executable, '-B', '-m', 'v_5.tests.loader_checkpoint_check', '--worker', str(root),
               '--scenario', scenario, '--plan', json.dumps(plan)]
    if docker:
        command.append('--docker')
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)


def kill_case(base, index, point, unit, scenario='normal', docker=False):
    """Kill at one boundary, restart, and assert which external operations may repeat."""
    root = base / f'k{index}'
    plan = [{'point': point, 'unit': unit, 'action': 'exit'}]
    first = launch(root, scenario, plan, docker)
    assert first.returncode == 86, (point, first.returncode, first.stdout, first.stderr)
    before = read_json(root / 'test_server' / 'server.json')
    second = launch(root, scenario, plan, docker)
    assert second.returncode == 0, (point, second.stdout, second.stderr)
    state, server = verify(root)
    scripts = [e for e in server['events'] if e['kind'] == 'execute']
    expected = 3 if point in ('after_execute', 'before_execution_result') else 2
    assert len(scripts) == expected, (point, len(scripts), expected)
    if 'glitch' in point:
        calls = [r for r in server['requests'] if r['role'] == 'glitch']
        assert len({r['request']['conversation'] for r in calls}) == 1, point
        assert sum(e['failed_script'] for e in scripts) == 1, point
        if point == 'after_glitch_response':
            assert len(calls) == 1
    if point == 'after_rollover_summary':
        assert sum(r['role'] == 'rollover' for r in server['requests']) == 1
    if point == 'after_input_sent':
        assert len([r for r in server['requests'] if r['role'] == 'loader']) == 3
    # A completed restart must perform no model, execution or review work at all.
    runner, _ = build_case(root, scenario=scenario)
    runner.run()
    assert read_json(root / 'test_server' / 'server.json') == server
    if point == 'after_complete':
        assert server == before
    return point


def branches(base):
    """Exercise alternative loader replies, retries, token limits and atomic saves."""
    names = []
    for scenario in ('clarify', 'early_clarify', 'unfinished', 'bad_manifest', 'review_handoff',
                     'stop', 'review_stop', 'incomplete_loader', 'invalid_loader',
                     'incomplete_glitch', 'invalid_glitch'):
        root = base / scenario
        runner, api = build_case(root, scenario=scenario)
        if scenario.endswith('glitch'):
            try:
                runner.run()
                raise AssertionError('Invalid glitch output was accepted')
            except (ValueError, RuntimeError) as error:
                assert not isinstance(error, AssertionError)
            saved = read_json(root / 'work' / 'checkpoint.json')['loader']
            assert saved['code_ran'] and saved['glitch']['response'] is None
            thread = saved['glitch']['thread_id']
            runner, api = build_case(root, scenario=scenario)
            runner.run()
            calls = [r for r in read_json(api.path)['requests'] if r['role'] == 'glitch']
            assert len(calls) == 2 and all(r['request']['conversation'] == thread for r in calls)
        else:
            runner.run()
        state, server = verify(root, stopped=scenario in ('stop', 'review_stop'))
        if scenario == 'early_clarify':
            assert not any(e['kind'] == 'answer' for e in server['events'])
        if scenario in ('incomplete_loader', 'invalid_loader', 'incomplete_glitch', 'invalid_glitch'):
            assert sum(e['kind'] == 'execute' for e in server['events']) == 2
        if scenario == 'review_handoff':
            assert sum(e['kind'] == 'review' for e in server['events']) == 2
        names.append(scenario)

    root = base / 'too_large'
    runner, _ = build_case(root, scenario='oversized_handoff')
    try:
        runner.run()
        raise AssertionError('Oversized handoff was submitted')
    except RuntimeError as error:
        assert 'cannot fit' in str(error)
    names.append('oversized handoff stops before submission')

    root = base / 'glitch_network'
    for point, action in [('before_glitch_submit', 'timeout'), ('after_glitch_submit', 'timeout'),
                          ('after_glitch_retrieve', 'disk')]:
        plan = [{'point': point, 'unit': '1', 'action': action}]
        case = root / point
        runner, _ = build_case(case, plan=plan)
        try:
            runner.run()
            raise AssertionError('Fault did not fire')
        except (TimeoutError, OSError):
            pass
        runner, _ = build_case(case, plan=plan)
        runner.run()
        _, server = verify(case)
        assert sum(e['kind'] == 'execute' for e in server['events']) == 2
        names.append(point + ' ' + action)

    path = base / 'atomic.json'
    atomic_text(path, '{"result":"old","code_ran":false}')
    def fail_replace():
        """Simulate a failed replacement after a complete temporary write."""
        raise OSError('disk unavailable before replace')
    try:
        atomic_text(path, '{"result":"new","code_ran":true}', before_replace=fail_replace)
    except OSError:
        pass
    assert read_json(path) == {'result': 'old', 'code_ran': False}
    atomic_text(path, '{"result":"new","code_ran":true}')
    assert read_json(path) == {'result': 'new', 'code_ran': True}
    names.append('result and flag survive failed atomic replacement together')

    root = base / 'execution_timeout'
    runner, api = build_case(root)
    normal_execute = runner.execute
    def timed_out(code, workspace, current):
        """Supply Docker's real timeout-result shape without waiting four minutes."""
        if code == BAD_CODE:
            api.event('execute', failed_script=True)
            log = current / 'stdout.txt'
            log.write_text('', encoding='utf-8')
            return {'error': 'Code exceeded 240 seconds; use smaller batches.', 'full_log': str(log)}
        return normal_execute(code, workspace, current)
    runner.execute = timed_out
    runner.run()
    _, server = verify(root)
    glitch = next(r for r in server['requests'] if r['role'] == 'glitch')
    payload = json.loads(glitch['request']['input'][-1]['content'])
    assert '240 seconds' in payload['error_output']['error']
    assert sum(e['kind'] == 'execute' for e in server['events']) == 2
    names.append('Docker timeout shape reaches glitch and then repair')
    return names


def production_parity(base):
    """Compare the app entry point with the shared runner used by the notebook.

    Only network, sandbox launch, user review and publication are controlled boundaries.
    The real loader loop, glitch wrapper, reply validation and manifest checks execute.
    """
    from v_5.ingest.data_loader import functions
    from v_5.main_functions import session as session_module
    from v_5.main_functions.session import Session, loader_request
    root = base / 'production'
    root.mkdir()
    source = root / 'toy.csv'
    from v_5.tests.loader_fixtures import DATA
    source.write_text(DATA, encoding='utf-8')
    files = {'toy': {'file_id': 'toy', 'path': str(source), 'folder': 'toy', 'role': 'primary',
                     'format': 'csv', 'filename': 'toy.csv'}}
    api = FakeAPI(root / 'server')

    class AppSession:
        def begin_phase(self, *args):
            """Ignore chat phase timing in this test."""

        def close_phase(self):
            """Ignore chat phase timing in this test."""

        def __init__(self):
            """Supply UI services without starting the app or touching user project state."""
            self.path = root
            self.run_path = root / 'run'
            import threading
            self.cancelled = threading.Event()
            self.state = {'context': {'name': 'Toy'}, 'folders': {'toy': {}}, 'files': files}
            self.thread = api.new_thread('loader')
            self.actions = []

        def noop(self, *args, **kwargs):
            """Discard UI logging; observable protocol calls remain in the fake server."""

        check_stop = record = message = show = print = save = progress = present_reply = noop

        def phase_report(self, folder, value):
            """Record the app's action sequence for comparison with the prototype."""
            self.actions.append(value['action'])

        def answer(self, prompt):
            """Supply the same toy user answer as the isolated runner."""
            return 'Yes, keep the original colours.'

        def count_input(self, request):
            """Keep this comparison within one session; separate tests exercise rollover."""
            return 400

        def call_loader(self, name, request, *args, **kwargs):
            """Use the scripted server while retaining the app-built request and contract."""
            role = 'rollover' if name == 'loader_summarizer_agent' else 'loader'
            request = dict(request, conversation=self.thread)
            result = api.retrieve(api.submit(role, request)['id'])
            return result['output_text'], result['usage'], result['status']

        def ask_agent(self, name, prompt, history, contract):
            """Use the real glitch wrapper's payload and validate its returned contract."""
            request = loader_request(prompt, history, contract)
            request['conversation'] = api.new_thread('glitch')
            result = api.retrieve(api.submit('glitch', request)['id'])
            return contract.model_validate_json(result['output_text']).model_dump()

        run_code = Session.run_code

    def execute(code, files, workspace, current, **kwargs):
        """Only replace Docker launch; the production wrapper still diagnoses failures."""
        current.mkdir(parents=True, exist_ok=True)
        if current.name == 'environment':
            return {'exit_code': 0, 'output': 'pandas and pyarrow test environment'}
        return local_execute(code, workspace, current)

    app = AppSession()
    with (patch.object(session_module, 'run_code', side_effect=execute),
          patch.object(functions, 'execute_code', side_effect=execute),
          patch.object(functions, 'LoaderCalls', return_value=api),
          patch.object(functions, 'review_prepared_data', return_value={'action': 'confirm'}),
          patch.object(functions, 'complete_codebooks') as codebooks):
        result = functions.prepare_data(app, '', {}, files, {}, {'toy': {'row_count': 3}}, [], {'action': 'finish'})
    assert result == 'complete' and codebooks.call_count == 1
    assert app.actions == ['run', 'run', 'finish']
    runner, prototype_api = build_case(base / 'parity', rollover=False)
    runner.run()
    server = read_json(api.path)
    twin = read_json(prototype_api.path)
    for role in ('loader', 'glitch'):
        app_calls = [r['request'] for r in server['requests'] if r['role'] == role]
        proto_calls = [r['request'] for r in twin['requests'] if r['role'] == role]
        assert len(app_calls) == len(proto_calls)
        for a, b in zip(app_calls, proto_calls):
            assert a['model'] == b['model'] and a['reasoning'] == b['reasoning'] and a['text'] == b['text']
        if role == 'glitch':
            a = json.loads(app_calls[0]['input'][-1]['content'])
            b = json.loads(proto_calls[0]['input'][-1]['content'])
            assert set(a) == set(b)
            for key in ('requesting_agent', 'executed_code', 'output_was_excerpted'):
                assert a[key] == b[key]
            assert "KeyError: 'missing'" in a['error_output'] and "KeyError: 'missing'" in b['error_output']
    production_table = pd.read_parquet(root / 'run' / 'loader' / 'folder_001' / 'toy.parquet')
    pd.testing.assert_frame_equal(production_table, pd.read_parquet(base / 'parity' / 'work' / 'toy.parquet'))


def main():
    """Run the repeatable suite, or a worker used by process-death tests."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--scenario', default='normal')
    parser.add_argument('--plan', default='[]')
    parser.add_argument('--docker', action='store_true')
    parser.add_argument('--app', action='store_true')
    args = parser.parse_args()
    if args.app:
        from v_5.tests.loader_resume_check import run_suite
        run_suite(docker=args.docker)
        return
    if args.worker:
        worker(args.worker, args.scenario, json.loads(args.plan), args.docker)
        return
    with tempfile.TemporaryDirectory(prefix='_ldr_', dir=ROOT) as temporary:
        base = Path(temporary)
        if args.docker:
            # Docker runs the same real scripts, including recovery after diagnosis failure.
            for i, (point, unit) in enumerate([('after_glitch_submit', '1'),
                                               ('after_glitch_response', '1'),
                                               ('after_execute', '2'),
                                               ('after_rollover_summary', '2')]):
                kill_case(base, i, point, unit, docker=True)
                print('Docker recovery passed:', point, flush=True)
            print('PASS: 4 real-Docker process-kill recoveries; scripted API, $0 API spend.', flush=True)
            return
        production_parity(base)
        print('Production loop / contract / glitch / artifacts parity passed.', flush=True)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(kill_case, base, i, point, unit) for i, (point, unit) in enumerate(KILLS)]
            for i, future in enumerate(futures, 1):
                future.result()
                if i % 10 == 0:
                    print(f'{i}/{len(KILLS)} process-kill recoveries passed.', flush=True)
        checks = branches(base)
        for i, (point, unit) in enumerate([('after_answer', '3'), ('after_advance', '4')], len(KILLS)):
            kill_case(base, i, point, unit, scenario='clarify')
        print(f'PASS: {len(KILLS) + 2} process-kill recoveries, {len(checks)} branch/failure checks, production parity; $0 API spend.', flush=True)


if __name__ == '__main__':
    main()
