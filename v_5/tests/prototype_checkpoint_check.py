"""Fault injection for the notebook: real process deaths offline, optional capped Luna runs."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

import nbformat
import pandas as pd
from v_5.prototype_data import DOCUMENT, PAGES
from v_5.prototype_runtime import (
    Checkpoints, FakeProvider, InjectedFailure, LAB_ROOT, NOTEBOOK, Spending,
    digest, file_lock, load_pipeline, oracle, read_json, run_case, save_json, FLAGS, LunaProvider,
)
from v_5.ingest.codebook_ingestion.tables import COLUMNS, normalize_rows

ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_FILES = [ROOT / 'v_5' / name for name in (
    'ingest/4_codebook_ingestion/sub_agents/pdf.py',
    'ingest/4_codebook_ingestion/sub_agents/agent.py',
    'ingest/4_codebook_ingestion/tables.py', 'ingest/4_codebook_ingestion/sessions.py',
    'main_functions/session.py')]


def source_hashes():
    """Fingerprint the production files that the experiment must leave unchanged.

    Inputs: Reads production PDF flow, schemas, table helpers and session helpers.
    Returns: File-to-hash mapping.
    Details: The stress suite checks it again after all work.
    """
    return {str(path.relative_to(ROOT)): digest(path.read_text(encoding='utf-8')) for path in PRODUCTION_FILES}


def original_baseline(directory, batch_size=30):
    """Compute expected toy rows/payloads independently, without recovery or API code.

    Inputs: temporary directory and final batch size. Returns: table plus expected inputs.
    This literal reference preserves the original page/lookahead and replacement sequence.
    """
    captured, blocks, notes = [], [], []
    for start in range(0, len(PAGES), 5):
        master, unfinished = {}, ''
        for index in range(start, min(start + 5, len(PAGES))):
            page = PAGES[index]
            payload = {'filename': 'toy_strings', 'page_range': [start + 1, min(start + 5, len(PAGES))],
                       'current_page': page, 'previous_page_lookbehind': PAGES[start - 1] if index == start and start else None,
                       'next_page_lookahead': PAGES[index + 1] if index + 1 < len(PAGES) else None,
                       'unfinished_entry': unfinished}
            captured.append({'role': 'reader', 'payload': payload})
            result = oracle(payload, 'reader')
            unfinished = result['unfinished_entry']
            notes.append('\n## Page ' + str(page['page']) + '\n\n' + ''.join(x + '\n\n' for x in result['general_information']))
            incoming = normalize_rows(result['rows']).to_dict('records')
            existing = [master[r['variable_name']] for r in incoming if r['variable_name'] in master]
            if existing:
                matching = {r['variable_name'] for r in existing}
                payload = {'current_page_rows': [r for r in incoming if r['variable_name'] in matching], 'existing_rows': existing}
                captured.append({'role': 'merge', 'payload': payload})
                replacements = {r['variable_name']: r for r in oracle(payload, 'merge')['rows']}
                incoming = [replacements.get(r['variable_name'], r) for r in incoming]
            master.update({r['variable_name']: r for r in incoming})
        blocks.append(list(master.values()))
    by_name, master, targets = {}, {}, []
    for number, rows in enumerate(blocks, 1):
        for row in rows:
            by_name.setdefault(row['variable_name'], []).append({'source': f'table{number}', **row})
    fields = ('variable_label', 'variable_description', 'other_information', 'values')
    for name, rows in by_name.items():
        master[name] = {field: rows[0][field] for field in COLUMNS}
        master[name]['source_location'] = '\n'.join(dict.fromkeys(r['source_location'] for r in rows))
        if len({tuple(row[field] for field in fields) for row in rows}) > 1:
            targets.append({'variable_name': name, 'source_rows': rows})
    for start in range(0, len(targets), batch_size):
        payload = {'target_rows': targets[start:start + batch_size]}
        captured.append({'role': 'reconcile', 'payload': payload})
        for row in oracle(payload, 'reconcile')['rows']:
            row['source_location'] = master[row['variable_name']]['source_location']
            master[row['variable_name']] = row
    directory.mkdir(parents=True)
    (directory / 'general_information.md').write_text(''.join(notes), encoding='utf-8')
    return normalize_rows(list(master.values())), captured


def normalized_values(frame):
    """Compare literal field contents while allowing harmless newline ordering differences.

    Inputs: frame: normalized source table.
    Returns: Variable-to-field mapping with distinct lines sorted for comparison.
    Details: Does not permit missing or invented facts, labels, locations or variables.
    """
    assert list(frame.columns) == COLUMNS
    assert not frame.variable_name.duplicated().any()
    return {row['variable_name']: {key: sorted(str(row[key]).splitlines()) for key in COLUMNS}
            for row in frame.to_dict('records')}


def fake_calls(root):
    """Read simulated-server evidence, which is never used by application recovery."""
    return [read_json(path) for path in (Path(root) / 'simulated_api').glob('resp_fake_*.json')]


def verify_case(root, expected, *, live=False, expected_payloads=None):
    """Check exact facts, notes, current flags and the absence of call/chat archives."""
    root = Path(root)
    frame = pd.read_csv(root / 'codebook' / 'normalized.csv', dtype=str, keep_default_na=False)
    assert normalized_values(frame) == normalized_values(expected), 'Final table differs from production truth'
    if not live:
        pd.testing.assert_frame_equal(frame, expected.reset_index(drop=True))
    state = read_json(root / 'codebook' / 'checkpoint.json')
    assert state['phase'] == 'complete'
    assert 'operations' not in state and 'agents' not in state
    for forbidden in ('operations', 'threads'):
        assert not (root / forbidden).exists()
    notes = (root / 'codebook' / 'general_information.md').read_text(encoding='utf-8')
    for page in range(1, 21):
        assert notes.splitlines().count(f'## Page {page}') == 1
        assert notes.splitlines().count(f'N{page:02d}') == 1
    for number in range(1, 5):
        reader = state['readers'][f'reader_{number:03d}']
        assert reader['current_pages'][0] == number * 5
        assert reader['unfinished_entry'] == ''
        assert len(reader['master_table']) == 3
        assert reader['parent_id'] == '000'
        assert reader['sub_agent']['parent_id'] == reader['agent_id']
        for agent in (reader, reader['sub_agent']):
            assert agent['thread_id'] and agent['response_id']
            flags = FLAGS if agent['parent_id'] == '000' else FLAGS[:-1]
            assert all(agent[flag] == 1 for flag in flags)
            assert agent['reserved_tokens'] == 0 and agent['used_tokens'] > 0
            if agent['parent_id'] != '000':
                assert 'merged' not in agent
            assert agent['output'] and agent['table']
            assert 'accepted_units' not in agent and 'previous_threads' not in agent
    agent = state['reconciler']
    assert agent['parent_id'] == '000' and all(agent[flag] == 1 for flag in FLAGS)
    assert normalized_values(normalize_rows(agent['master_table'])) == normalized_values(expected)
    assert sorted(path.name for path in (root / 'codebook').iterdir()) == [
        'block_tables_joined.csv', 'checkpoint.json', 'general_information.md', 'master_rows.json',
        'normalized.csv', 'run.lock', 'verification.json']
    calls = fake_calls(root)
    if expected_payloads is not None:
        captured = []
        for record in calls:
            request = record['request']
            assert request['model'] == 'gpt-6-luna' and request['reasoning']['effort'] == 'high'
            payload = json.loads(request['input'][0]['content'])
            role = 'reader' if 'current_page' in payload else 'merge' if 'existing_rows' in payload else 'reconcile'
            captured.append({'role': role, 'payload': payload})
        assert set(map(digest, captured)) == set(map(digest, expected_payloads)), 'Production payloads changed'
    return {'variables': len(frame), 'facts': 20, 'submissions': len(calls),
            'checkpoint_bytes': (root / 'codebook' / 'checkpoint.json').stat().st_size}


def child(root, plan=(), *, live=False, batch_size=30):
    """Launch the same pipeline in a fresh process; test controls are separate files."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    plan_path = root / 'fault_plan.json'
    save_json(plan_path, list(plan))
    command = [sys.executable, '-B', '-m', 'v_5.prototype_runtime', '--case', str(root),
               '--plan', str(plan_path), '--batch-size', str(batch_size)]
    if live:
        command.append('--live')
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                          encoding='utf-8', errors='replace', timeout=600 if live else 120)


def assert_cursor_resume(before, new_calls):
    """Prove restarts do not resend earlier pages or known reader responses."""
    for record in new_calls:
        payload = json.loads(record['request']['input'][0]['content'])
        if 'current_page' in payload:
            page = payload['current_page']['page']
            reader = before['readers'][f'reader_{(page - 1) // 5 + 1:03d}']
            current = reader['current_pages'][0]
            assert page >= current, ('Earlier page replayed', page, current)
            if page == current:
                assert not reader['sent'], ('Known request was resubmitted', page)


def crash_case(root, point, unit, expected, payloads, batch_size=30):
    """Kill the entire four-worker process, then resume from its saved cursors."""
    plan = [{'point': point, 'unit': unit, 'action': 'exit'}]
    failed = child(root, plan, batch_size=batch_size)
    assert failed.returncode == 86, (point, unit, failed.returncode, failed.stderr[-1800:])
    before = read_json(root / 'codebook' / 'checkpoint.json')
    calls_before = {call['response']['id'] for call in fake_calls(root)}
    resumed = child(root, plan, batch_size=batch_size)
    assert resumed.returncode == 0, (point, unit, resumed.stderr[-2500:])
    verify_case(root, expected, expected_payloads=payloads)
    new_calls = [c for c in fake_calls(root) if c['response']['id'] not in calls_before]
    assert_cursor_resume(before, new_calls)
    count = len(fake_calls(root))
    run_case(root, batch_size=batch_size)
    assert len(fake_calls(root)) == count, 'Completed restart generated new calls'
    return {'point': point, 'unit': unit, 'passed': True}


class BadReplyProvider(FakeProvider):
    """Return one realistic unsuccessful model result, then work normally."""

    def __init__(self, root, kind):
        super().__init__(root)
        self.kind = kind
        self.guard = threading.Lock()

    def retrieve(self, response_id):
        result = super().retrieve(response_id)
        if self.kind == 'wrong_variables':
            request = read_json(self.root / (response_id + '.json'))['request']
            if 'existing_rows' not in json.loads(request['input'][0]['content']):
                return result
        with self.guard:
            marker = self.root / 'failure_used.json'
            if not marker.exists():
                save_json(marker, {'kind': self.kind})
                if self.kind == 'failed':
                    result['status'] = 'failed'
                elif self.kind == 'incomplete':
                    result['status'] = 'incomplete'
                elif self.kind == 'invalid_json':
                    result['output_text'] = '{'
                elif self.kind == 'wrong_variables':
                    output = json.loads(result['output_text'])
                    output['rows'][0]['variable_name'] = 'wrong_variable'
                    result['output_text'] = json.dumps(output)
        return result


def conversation_lock_check(root):
    """Check the actual API lock error retries briefly, without duplicate reservations."""
    from types import SimpleNamespace
    from unittest.mock import Mock
    from openai import BadRequestError
    import httpx
    error = BadRequestError('Conversation busy',
        response=httpx.Response(400, request=httpx.Request('POST', 'https://api.openai.com/v1/responses')),
        body={'code': 'conversation_locked'})
    for name, side_effect in (
        ('clears', [error, SimpleNamespace(id='response_test', status='queued')]),
        ('stays_locked', error)):
        provider = LunaProvider.__new__(LunaProvider)
        provider.ledger = Spending(Path(root) / (name + '.json'))
        create = Mock(side_effect=side_effect)
        provider.client = SimpleNamespace(responses=SimpleNamespace(create=create))
        provider.session = SimpleNamespace(check_stop=lambda: None, message=lambda *a: None)
        with patch('v_5.ingest.codebook_ingestion.sub_agents.pdf_calls.time.sleep') as sleep:
            if name == 'clears':
                assert provider.submit({}, name)['id'] == 'response_test'
                assert create.call_count == 2 and sleep.call_count == 1
            else:
                try:
                    provider.submit({}, name)
                except BadRequestError:
                    pass
                else:
                    raise AssertionError('Busy conversation did not stop at the retry limit')
                assert create.call_count == 16 and sleep.call_count == 15
        assert len(read_json(provider.ledger.path)['calls']) == 1


def offline_suite():
    """Test ordinary interruption points, network/disk errors and unfinished child work."""
    hashes, started = source_hashes(), time.monotonic()
    report = {'passed': False, 'crash_cases': [], 'other_checks': []}
    nbformat.validate(nbformat.read(NOTEBOOK, as_version=4))
    with tempfile.TemporaryDirectory(prefix='v5_simple_') as tmp:
        work = Path(tmp)
        expected, payloads = original_baseline(work / 'original')
        baseline = work / 'baseline'
        run_case(baseline)
        report['baseline'] = verify_case(baseline, expected, expected_payloads=payloads)
        assert len(fake_calls(baseline)) == 29
        assert (baseline / 'codebook' / 'general_information.md').read_text(encoding='utf-8') == (work / 'original' / 'general_information.md').read_text(encoding='utf-8')
        points = ('before_submit', 'after_submit_before_save', 'after_sent_save',
                  'after_reply_before_save', 'after_output_save', 'after_table_save')
        cases = [(point, unit, 30) for point in points for unit in
                 ('read_b01_p003', 'merge_b01_p003', 'reconcile_001')]
        cases += [(point, unit, 30) for point in ('before_merge_save', 'during_merge_save', 'after_merge_save')
                  for unit in ('read_b01_p003', 'reconcile_001')]
        cases += [('after_advance_save', 'read_b01_p004', 30),
                  ('after_advance_save', 'reconcile_002', 1),
                  ('after_phase_save', 'workflow', 30), ('after_csv_export', 'workflow', 30)]
        expected_small, payloads_small = original_baseline(work / 'original_small', 1)

        def one(item):
            index, (point, unit, size) = item
            result = crash_case(work / f'crash_{index:02d}', point, unit,
                                expected_small if size == 1 else expected,
                                payloads_small if size == 1 else payloads, size)
            print(f'PASS crash {index + 1}/{len(cases)}: {point} / {unit}', flush=True)
            return result

        with ThreadPoolExecutor(max_workers=3) as pool:
            report['crash_cases'] = list(pool.map(one, enumerate(cases)))
        for name, point, action in (
            ('submission_timeout', 'before_submit', 'timeout'),
            ('reply_connection_lost', 'after_submit_before_save', 'timeout'),
            ('retrieval_timeout', 'before_retrieve', 'timeout'),
            ('disk_write_failed', 'during_merge_save', 'disk')):
            root = work / name
            plan = [{'point': point, 'unit': 'read_b01_p003', 'action': action}]
            try:
                run_case(root, plan=plan)
            except (TimeoutError, OSError):
                pass
            else:
                raise AssertionError('Fault was not reached: ' + name)
            before = read_json(root / 'codebook' / 'checkpoint.json')
            ids = {call['response']['id'] for call in fake_calls(root)}
            run_case(root, plan=plan)
            verify_case(root, expected, expected_payloads=payloads)
            assert_cursor_resume(before, [c for c in fake_calls(root) if c['response']['id'] not in ids])
            report['other_checks'].append(name)
        for kind in ('failed', 'incomplete', 'invalid_json', 'wrong_variables'):
            root = work / kind
            try:
                load_pipeline()['run_prototype'](Checkpoints(root, BadReplyProvider(root, kind)))
            except ValueError:
                pass
            else:
                raise AssertionError('Bad response was not rejected: ' + kind)
            load_pipeline()['run_prototype'](Checkpoints(root, BadReplyProvider(root, kind)))
            verify_case(root, expected, expected_payloads=payloads)
            assert len(fake_calls(root)) == 30
            report['other_checks'].append(kind)
        # A second launch is a normal notebook mistake, and must not overwrite live workers.
        with file_lock(baseline / 'codebook' / 'run.lock'):
            try:
                run_case(baseline)
            except OSError:
                pass
            else:
                raise AssertionError('Duplicate runner was allowed')
        report['other_checks'].append('duplicate_runner_blocked')
        budget = Spending(work / 'test_spending.json', limit=.000001)
        try:
            budget.reserve('request', {}, [])
        except RuntimeError:
            pass
        else:
            raise AssertionError('Budget cap was not enforced')
        report['other_checks'].append('spending_cap')
        # Complete runs re-export deliverables without model calls, including an interrupted export.
        count = len(fake_calls(baseline))
        (baseline / 'codebook' / 'normalized.csv').unlink()
        run_case(baseline)
        verify_case(baseline, expected)
        assert len(fake_calls(baseline)) == count
        report['other_checks'].append('completed_restart_no_calls')
        with patch.dict(PAGES[0], {'markdown': PAGES[0]['markdown'] + '\nChanged source'}):
            try:
                run_case(baseline)
            except ValueError as error:
                assert 'inputs or settings changed' in str(error)
            else:
                raise AssertionError('Changed source reused the old checkpoint')
        report['other_checks'].append('changed_source_rejected')
        conversation_lock_check(work / 'conversation_lock')
        report['other_checks'].append('conversation_lock_retry_bounded')
    assert source_hashes() == hashes
    report.update(passed=True, source_unchanged_during_tests=True, seconds=round(time.monotonic() - started, 1))
    save_json(LAB_ROOT / 'simple_offline_results.json', report)
    print(f"PASS: {len(report['crash_cases'])} process-kill recoveries; {len(report['other_checks'])} other checks.", flush=True)
    return report


def live_suite():
    """Repeatedly interrupt real Luna work, including an unfinished child and batch two."""
    hashes = source_hashes()
    plan = [dict(point=point, unit=unit, action='exit') for point, unit in (
        ('after_submit_before_save', 'read_b01_p001'),
        ('after_sent_save', 'read_b01_p001'),
        ('after_output_save', 'read_b01_p001'),
        ('after_table_save', 'read_b01_p001'),
        ('during_merge_save', 'read_b01_p001'),
        ('after_merge_save', 'read_b01_p001'),
        ('after_sent_save', 'merge_b01_p003'),
        ('after_output_save', 'merge_b01_p003'),
        ('after_advance_save', 'read_b01_p004'),
        ('after_sent_save', 'reconcile_001'),
        ('after_merge_save', 'reconcile_001'),
        ('after_advance_save', 'reconcile_002'),
        ('after_csv_export', 'workflow'))]
    folder = LAB_ROOT / 'app_live_recovery'
    with tempfile.TemporaryDirectory(prefix='v5_simple_truth_') as tmp:
        expected, _ = original_baseline(Path(tmp) / 'original', 1)
        for attempt in range(len(plan) + 1):
            result = child(folder, plan, live=True, batch_size=1)
            print(f'Live process {attempt + 1}: exit {result.returncode}', flush=True)
            assert result.returncode in (0, 86), result.stderr[-3500:]
            if result.returncode == 0:
                break
        else:
            raise AssertionError('Live run did not finish')
        assert len(read_json(folder / 'faults.json', [])) == len(plan)
        counts = verify_case(folder, expected, live=True)
        before = read_json(LAB_ROOT / 'spending.json')
        class NoNetwork:
            model, output_limit = 'gpt-6-luna', 4096
            def check_stop(self):
                pass
            def __getattr__(self, name):
                raise AssertionError('Completed run tried API access: ' + name)
        load_pipeline()['run_prototype'](Checkpoints(folder, NoNetwork()), batch_size=1)
        assert read_json(LAB_ROOT / 'spending.json') == before
    calls = list(before['calls'].values())
    assert source_hashes() == hashes
    report = {'passed': True, 'live_process_kills': len(plan), 'model': 'gpt-6-luna',
              'effort': 'high', 'completed_restart_calls': 0, 'source_unchanged_during_tests': True,
              'variables': counts['variables'], 'facts': counts['facts'],
              'estimated_known_total_usd': sum(c['estimated_usd'] or 0 for c in calls),
              'conservative_total_usd': sum(c['budget_charge_usd'] for c in calls)}
    assert report['conservative_total_usd'] <= 5
    save_json(LAB_ROOT / 'simple_live_results.json', report)
    print(json.dumps(report, indent=2), flush=True)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args()
    live_suite() if args.live else offline_suite()
