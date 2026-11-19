"""Break the real descriptives checkpoint and exercise its actual app retry path."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch

import pandas as pd

from v_5.ingest.descriptives_check import functions as review_functions
from v_5.ingest.descriptives_check.agent import DESCRIPTIVES_PROMPT
from v_5.ingest.descriptives_check.checkpoint import DescriptivesRunner
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json, save_json
from v_5.tests.descriptives_check import review
from v_5.tests.loader_fixtures import FakeAPI, DATA, local_execute as loader_execute
from v_5.prototype_runtime import Faults, InjectedFailure

ROOT = Path(__file__).resolve().parents[2]
BAD = "import pandas as pd\ndf = pd.read_parquet('/work/toy.parquet')\nprint(df['missing'])\n"
GOOD = ("import json\nfrom pathlib import Path\nimport pandas as pd\n"
        "df = pd.read_parquet('/work/toy.parquet')\n"
        "assert len(df) == 3 and df['id'].is_unique\n"
        "Path('/review/nulls.json').write_text(json.dumps(df.isna().sum().to_dict()), encoding='utf-8')\n"
        "print(json.dumps({'rows': len(df), 'columns': len(df.columns), 'nulls': '/review/nulls.json'}))\n")
WRITE_ATTEMPT = "from pathlib import Path\nPath('/work/toy.parquet').write_text('must not succeed')\n"


class ReviewAPI(FakeAPI):
    """Represent remote conversations separately from recovery state, using real contracts."""

    def submit(self, role, request):
        """Produce review replies from their actual input while retaining fake server history."""
        if role != 'descriptives':
            return super().submit(role, request)
        server = read_json(self.path)
        thread = request['conversation']
        messages = server['threads'][thread]['messages']
        messages.extend(m for m in request['input'] if m['role'] != 'system')
        server['requests'].append({'role': role, 'request': request})
        payload = None
        for message in reversed(messages):
            if message['role'] != 'user':
                continue
            try:
                value = json.loads(message['content'])
            except ValueError:
                continue
            if 'incomplete or invalid' not in value.get('instruction', ''):
                payload = value
                break
        if 'folder_context' in payload or 'No question was shown' in payload.get('instruction', ''):
            action = 'stop' if self.scenario == 'stop' else 'run'
            result = review(action, code=WRITE_ATTEMPT if self.scenario == 'write_attempt' else BAD)
            if self.scenario == 'early_question' and 'early' not in server['once']:
                server['once'].append('early')
                result = review('clarify', question='Premature question')
        elif 'execution_result' in payload:
            if payload['execution_result'].get('exit_code') != 0:
                result = review('run', code=GOOD)
            else:
                result = review('clarify', question='Does this look right?')
        elif 'user_answer' in payload:
            wrong = 'wrong' in payload['user_answer'].lower()
            result = review('handoff' if wrong else 'confirm',
                            issue='User reports an incorrect colour value.' if wrong else '',
                            evidence=['Three rows checked; see saved null counts.'],
                            requested_correction='Investigate the colour mapping.' if wrong else '')
        else:
            raise AssertionError(payload)
        status, text = 'completed', json.dumps(result)
        if self.scenario in ('invalid_review', 'incomplete_review') and self.scenario not in server['once']:
            server['once'].append(self.scenario)
            if self.scenario == 'invalid_review':
                text = '{invalid'
            else:
                status = 'incomplete'
        key = f'response_{len(server["responses"]) + 1}'
        server['responses'][key] = {'id': key, 'output_text': text, 'status': status,
                                    'usage': {'input_tokens': 100, 'output_tokens': 100}}
        messages.append({'role': 'assistant', 'content': text})
        save_json(self.path, server)
        return {'id': key, 'status': status}


def local_execute(code, workspace, current):
    """Execute only the two trusted read-only review fixtures in a fresh Python process."""
    if code not in (BAD, GOOD):
        raise ValueError('Only fixed review fixtures may run on the host.')
    mapped = code.replace("'/work/toy.parquet'", repr(str(workspace / 'toy.parquet')))
    mapped = mapped.replace("'/review/nulls.json'", repr(str(current / 'nulls.json')))
    script = current / 'code.py'
    script.write_text(mapped, encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', str(script)], capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    log = current / 'stdout.txt'
    log.write_text(output, encoding='utf-8')
    return dict(exit_code=result.returncode, output=output[:24000], truncated=len(output) > 24000, full_log=str(log))


def build_case(root, plan=(), scenario='normal'):
    """Create the real runner with tiny prepared data, scripted API and fault injection."""
    root = Path(root)
    workspace = root / 'work'
    workspace.mkdir(parents=True, exist_ok=True)
    if not (workspace / 'toy.parquet').exists():
        from io import StringIO
        pd.read_csv(StringIO(DATA)).to_parquet(workspace / 'toy.parquet', index=False)
    api = ReviewAPI(root / 'server', scenario=scenario)
    controls = root / 'controls'
    controls.mkdir(exist_ok=True)
    faults = Faults(controls, plan)
    def execute(code, current):
        """Count sandbox launches so diagnosis retries cannot conceal repeated execution."""
        api.event('execute', failed=code == BAD)
        return local_execute(code, workspace, current)
    def answer(question):
        """Supply a deterministic user confirmation for process-crash tests."""
        api.event('answer', question=question)
        return 'Yes'
    runner = DescriptivesRunner(workspace / 'review', workspace,
        {'folder_context': {'folder': 'toy'}, 'loader_report': {}}, DESCRIPTIVES_PROMPT,
        api, execute, answer, hook=faults.hit)
    return runner, api


KILLS = [('before_submit', '1'), ('after_submit', '1'), ('after_input_sent', '1'),
         ('after_retrieve', '1'), ('after_reply', '1'), ('after_execute', '1'),
         ('after_execution_result', '1'), ('after_glitch_thread', '1'),
         ('after_glitch_submit', '1'), ('after_glitch_retrieve', '1'),
         ('after_glitch_response', '1'), ('before_advance', '2'), ('after_advance', '2'),
         ('after_input_sent', '2'), ('after_execute', '2'), ('after_execution_result', '2'),
         ('after_advance', '3'), ('after_reply', '3'), ('after_answer', '3'),
         ('after_advance', '4'), ('after_reply', '4'), ('before_return', '4')]


def kill_case(root, point, unit):
    """Kill a worker process, recreate it, and verify only unfinished work repeats."""
    plan = [{'point': point, 'unit': unit, 'action': 'exit'}]
    command = [sys.executable, '-B', '-m', 'v_5.tests.descriptives_resume_check',
               '--worker', str(root), '--plan', json.dumps(plan)]
    first = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert first.returncode == 86, (point, first.stdout, first.stderr)
    second = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=90)
    assert second.returncode == 0, (point, second.stdout, second.stderr)
    runner, api = build_case(root, plan)
    server = read_json(api.path)
    assert runner.run()['action'] == 'confirm'
    assert read_json(api.path) == server  # Saved terminal reply returns without new calls.
    expected = 3 if point == 'after_execute' else 2
    assert sum(e['kind'] == 'execute' for e in server['events']) == expected, point
    calls = [c['request'] for c in server['requests'] if c['role'] == 'glitch']
    assert len({c['conversation'] for c in calls}) == 1
    if point == 'after_glitch_response':
        assert len(calls) == 1
    assert read_json(runner.root / 'current' / 'nulls.json') == {'id': 0, 'colour': 0, 'score': 0}
    assert not list(runner.root.rglob('conversation.json'))
    assert not list(runner.root.glob('turn_*'))
    assert runner.state['cycle'] == 4 and runner.state['agent_returned']
    assert pd.read_parquet(runner.workspace / 'toy.parquet')['id'].tolist() == [1, 2, 3]
    for call in server['requests']:
        request = call['request']
        assert request['model'] == 'gpt-6.1-sol' and request['reasoning']['effort'] == 'medium'
        assert request['max_output_tokens'] == 8000


def branch_checks(root):
    """Check malformed replies, early questions, expired receipts and unchanged inputs."""
    for scenario in ('invalid_review', 'incomplete_review', 'early_question', 'stop',
                     'invalid_glitch', 'incomplete_glitch'):
        runner, api = build_case(root / scenario, scenario=scenario)
        if scenario.endswith('glitch'):
            try:
                runner.run()
                raise AssertionError('Invalid diagnosis was accepted')
            except (ValueError, RuntimeError):
                pass
            runner, api = build_case(root / scenario, scenario=scenario)
        assert runner.run()['action'] == ('stop' if scenario == 'stop' else 'confirm')
        events = read_json(api.path)['events']
        assert sum(e['kind'] == 'execute' for e in events) == (0 if scenario == 'stop' else 2)
        if scenario == 'early_question':
            assert [e['question'] for e in events if e['kind'] == 'answer'] == ['Does this look right?']
    case = root / 'expired'
    plan = [{'point': 'after_input_sent', 'unit': '1'}]
    runner, api = build_case(case, plan)
    try:
        runner.run()
    except InjectedFailure:
        pass
    runner, api = build_case(case, plan)
    missing, original = runner.state['response_id'], api.retrieve
    api.missing_response_error = LookupError
    def retrieve(response_id):
        """Expire just the saved receipt; the original thread remains available."""
        if response_id == missing:
            raise LookupError('Expired response')
        return original(response_id)
    api.retrieve = retrieve
    assert runner.run()['action'] == 'confirm'
    requests = [r['request'] for r in read_json(api.path)['requests'] if r['role'] == 'descriptives']
    assert 'could not recover' in requests[1]['input'][1]['content']
    assert requests[0]['conversation'] == requests[1]['conversation']


def app_case(root, point='after_glitch_submit', unit='1', *, docker=False, handoff=False, question=False):
    """Resume a review through the real app, preserving the loader and user form."""
    from v_5.tests.loader_resume_check import project_fixture, client_for, wait_state
    from v_5.ingest.data_loader import functions as loader_functions
    from v_5.main_functions.session import Session
    project, directory = project_fixture(root)
    api = ReviewAPI(root / 'server', scenario='write_attempt' if docker else 'normal')
    controls = root / 'controls'
    controls.mkdir()
    faults = Faults(controls, [] if question else [{'point': point, 'unit': unit}])
    published, review_threads = [], []
    real_docker = review_functions.run_code

    def runner(*args, **kwargs):
        """Inject a failure into the production review; its workspace is supplied by the loader."""
        return DescriptivesRunner(*args, **kwargs, hook=faults.hit)
    def execute_loader(code, files, workspace, current, **options):
        """Use the already-tested loader fixture while counting unwanted loader replays."""
        api.event('loader_execute')
        return loader_execute(code, workspace, current)
    def execute_review(code, files, workspace, current, **options):
        """Check the read-only mount option and preserve the prepared file byte-for-byte."""
        assert options['read_only'] is True
        before = (workspace / 'toy.parquet').read_bytes()
        api.event('review_execute')
        result = real_docker(code, files, workspace, current, **options) if docker else local_execute(code, workspace, current)
        assert (workspace / 'toy.parquet').read_bytes() == before
        return result
    def publish(session, plan):
        """Record the real codebook-to-publication handoff without accessing a database."""
        published.append(plan)
        session.state['atlas'] = {'status': 'published', 'data': [{'folder': 'toy'}]}

    with (patch.object(loader_functions, 'LoaderCalls', return_value=api),
          patch.object(loader_functions, 'execute_code', side_effect=execute_loader),
          patch.object(review_functions, 'DescriptivesCalls', return_value=api),
          patch.object(review_functions, 'DescriptivesRunner', side_effect=runner),
          patch.object(review_functions, 'run_code', side_effect=execute_review),
          patch.object(Session, 'run_code', return_value={'exit_code': 0, 'output': 'pandas and pyarrow'}),
          patch.object(Session, 'ask_agent', side_effect=AssertionError('Old review loop was called')),
          patch('v_5.ingest.codebook_ingestion.functions.ingest_codebooks', return_value={}),
          patch('v_5.ingest.codebook_ingestion.finalize.finalize_codebook'),
          patch('v_5.ingest.publishing.publish_atlas', side_effect=publish),
          patch.object(loader_functions, 'write_final_summary', return_value='complete')):
        client, headers = client_for(root)
        assert client.post(f'/api/projects/{project}/start', headers=headers, json={'prepare_only': True}).status_code == 200
        if question:
            waiting = wait_state(client, project, ('awaiting_user', 'failed'))
            assert waiting['status'] == 'awaiting_user', waiting.get('error')
            assert client.post(f'/api/projects/{project}/stop', headers=headers, json={}).status_code == 200
        failed = wait_state(client, project)
        assert failed['status'] in ('failed', 'stopped'), failed.get('error')
        workspace = Path(failed['loader_resume']['workspace'])
        checkpoints = list(workspace.glob('review_after_turn_*/checkpoint.json'))
        assert len(checkpoints) == 1
        saved = read_json(checkpoints[0])
        old_requests = len(read_json(api.path)['requests'])
        assert not published

        client, headers = client_for(root)
        assert client.post(f'/api/projects/{project}/start', headers=headers, json={'prepare_only': True}).status_code == 200
        answers = 0
        while True:
            state = wait_state(client, project, ('awaiting_user', 'complete', 'failed'))
            assert state['status'] != 'failed', state.get('error')
            if state['status'] == 'complete':
                break
            if question and answers == 0:
                assert len(read_json(api.path)['requests']) == old_requests
            form = state['question_form']
            assert form['questions'][0]['question'] == 'Does this look right?'
            answer = 'Something is wrong; investigate it.' if handoff and answers == 0 else 'Yes, it looks right.'
            assert client.post(f'/api/projects/{project}/answer', headers=headers,
                json={'form_id': form['id'], 'answers': {'clarification': answer}}).status_code == 200
            answers += 1
            assert answers <= 2
        assert len(published) == 1 and 'loader_resume' not in state and 'codebook_resume' not in state
        assert len(list(workspace.glob('review_after_turn_*/checkpoint.json'))) == (2 if handoff else 1)
        after = read_json(checkpoints[0])
        assert after['thread_id'] == saved['thread_id']
        assert after['reply']['action'] == ('handoff' if handoff else 'confirm')
        assert not list(workspace.glob('review_after_turn_*/conversation.json'))
        events = read_json(api.path)['events']
        assert sum(e['kind'] == 'loader_execute' for e in events) == (3 if handoff else 2)
        assert sum(e['kind'] == 'review_execute' for e in events) == (4 if handoff else 2)


def main():
    """Run isolated process crashes, app retries, or the actual Docker review checks."""
    parser = argparse.ArgumentParser()
    parser.add_argument('--worker', type=Path)
    parser.add_argument('--plan', default='[]')
    parser.add_argument('--app', action='store_true')
    parser.add_argument('--docker', action='store_true')
    args = parser.parse_args()
    if args.worker:
        runner, _ = build_case(args.worker, json.loads(args.plan))
        assert runner.run()['action'] == 'confirm'
        return
    with tempfile.TemporaryDirectory(prefix='_dr_', dir=ROOT) as temp:
        root = Path(temp)
        if args.app or args.docker:
            if args.docker:
                app_case(root / 'docker', docker=True)
                print('PASS: real-Docker read-only enforcement, saved error, glitch retry and app restart.', flush=True)
                return
            for index, (point, unit) in enumerate([('after_submit', '1'), ('after_input_sent', '1'),
                ('after_execution_result', '1'), ('after_glitch_submit', '1'),
                ('after_glitch_response', '1'), ('after_reply', '3')]):
                app_case(root / str(index), point, unit)
                print('App review recovery passed:', point, flush=True)
            app_case(root / 'question', question=True)
            app_case(root / 'handoff', handoff=True)
            print('PASS: 8 actual-app review restarts, restored confirmation, loader correction and fresh re-review.', flush=True)
            return
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(kill_case, root / str(i), point, unit) for i, (point, unit) in enumerate(KILLS)]
            for i, future in enumerate(futures, 1):
                future.result()
                if i % 5 == 0:
                    print(f'{i}/{len(KILLS)} process-kill recoveries passed.', flush=True)
        branch_checks(root / 'branches')
    print(f'PASS: {len(KILLS)} process-kill recoveries and 7 failure/branch checks. No paid calls.', flush=True)


if __name__ == '__main__':
    main()
