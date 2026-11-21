"""Interrupt real reconciliation batches; all model replies are scripted and local."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import pandas as pd

from v_5.ingest.codebook_ingestion import finalize
from v_5.ingest.codebook_ingestion.checkpoint import ReconciliationCheckpoint
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json
from v_5.ingest.codebook_ingestion.tables import COLUMNS
from v_5.tests.master_codebook_check import definition, roster, accepted


class Failure(RuntimeError):
    """A simulated failure at an actual save or request boundary."""


class Session:
    """Supply app interactions while counting user reviews and honoring Stop."""

    def begin_phase(self, *args):
        """Ignore chat phase timing in this test."""

    def close_phase(self):
        """Ignore chat phase timing in this test."""

    def __init__(self):
        """Start one isolated user-review and cancellation state."""
        self.state, self.answers, self.stopped = {}, 0, False

    def message(self, *args):
        """Suppress UI messages; saved codebook outputs are asserted directly."""
        pass

    def record(self, *args):
        """Avoid permanent journals in these temporary workflow checks."""
        pass

    def check_stop(self):
        """Honor Stop at the production runner's normal cancellation boundaries."""
        if self.stopped:
            raise InterruptedError('Stopped')

    def answer(self, prompt):
        """Resolve the fixture's one real source conflict and count repeated questions."""
        self.answers += 1
        return {'exact': 'Use dollars'}


class API:
    """Act as remote threads, retaining responses when the local finalizer is recreated."""

    missing_response_error = LookupError

    def __init__(self):
        """Start remote-thread fixtures that survive local finalizer recreation."""
        self.threads, self.requests, self.responses = [], [], {}
        self.bad = None

    def new_thread(self):
        """Give each fresh 50-variable agent a distinct thread ID."""
        key = f'thread_{len(self.threads) + 1}'
        self.threads.append(key)
        return key

    def count_input(self, request, name):
        """Use a fixed small count while preserving production budget accounting."""
        return 100

    def submit(self, request, name):
        """Exercise all five lookaheads, conflict review and search/decision payloads."""
        payload = next(json.loads(item['content']) for item in request['input']
                       if item.get('role') == 'user' and item['content'].startswith('{'))
        self.requests.append((name, deepcopy(payload), deepcopy(request)))
        targets = [row['variable_name'] for row in payload['target_rows']]
        if name == 'master_to_codebook_reconciler':
            if payload['stage'] == 'search':
                result = {'candidates': [{'variable_name': target, 'patterns': ['^absent$' if target == 'q_100' else '^q$']}
                                         for target in targets]}
            else:
                result = {'updates': [accepted(targets[0], 'q', variable_label='Supported ' + targets[0])]}
        elif targets[0] == 'exact' and 'user_instruction' not in payload['target_rows'][0] and payload['can_request_more_battery_rows']:
            result = {'require_further_battery_rows': True, 'rows': []}
        else:
            rows = [definition(target) for target in targets]
            if 'user_instruction' in payload['target_rows'][0]:
                rows[0]['categories'] = 'dollars'
            elif targets[0] == 'exact':
                rows[0]['conflicts'] = [{'field': 'categories', 'reason': 'Units differ', 'evidence': [
                    {'source_id': 'book', 'excerpt': 'dollars'}, {'source_id': 'other', 'excerpt': 'cents'}]}]
            result = {'require_further_battery_rows': False, 'rows': rows}
        if self.bad == 'soft' and payload.get('stage') == 'reconcile':
            result['updates'][0]['source_variables'] = ['not_returned']
            self.bad = None
        text = json.dumps(result)
        if self.bad == 'syntax':
            text, self.bad = '{invalid', None
        key = f'response_{len(self.requests)}'
        self.responses[key] = {'id': key, 'status': 'completed', 'output_text': text,
                               'usage': {'input_tokens': 100, 'output_tokens': 100}}
        return {'id': key, 'status': 'completed'}

    def retrieve(self, response_id):
        """Retrieve an existing output without another submission."""
        return self.responses[response_id]


def fixture(root):
    """Use 102 master rows, 51 unmatched source rows and one user-reviewed conflict."""
    root.mkdir(parents=True)
    names = ['exact', *(f'q_{i}' for i in range(101))]
    extras = ['q', *(f'old_{i}' for i in range(50))]
    master = roster(names)
    book = dict(combined=str(root / 'combined.csv'), unmatched=str(root / 'unmatched.csv'), source_tables={})
    master.to_csv(book['combined'], index=False)
    pd.DataFrame({'variable_name': extras}).to_csv(book['unmatched'], index=False)
    for key, variables in [('book', ['exact', *extras]), ('other', ['exact'])]:
        path = root / f'{key}.csv'
        frame = pd.DataFrame({'variable_name': variables,
            **{f'{field}_{key}': ['Source note'] * len(variables) for field in COLUMNS[1:]}})
        frame.to_csv(path, index=False)
        book['source_tables'][key] = {'table': str(path), 'filename': path.name, 'primary': key == 'book'}
    return book, Session(), API()


def run_app(root, book, session, api, hook=None):
    """Call the production finalizer with only model transport and planning substituted."""
    def checkpoint(directory, owner):
        """Use real disk state with the simulated remote API and selected interruption."""
        return ReconciliationCheckpoint(directory, owner, provider=api, hook=hook)
    with patch.object(finalize, 'ReconciliationCheckpoint', side_effect=checkpoint), \
            patch.object(finalize, 'planning_overview', return_value={'overview': {}, 'variables_seen': 102}):
        finalize.finalize_codebook(session, {'files': {}}, {}, root, book)


def output(root):
    """Compare actual published-table inputs byte for byte, including reasons and taxonomy."""
    return {name: (root / name).read_bytes() for name in
            ('final_codebook.csv', 'final_unmatched_codebook.csv', 'soft_matched_codebook.csv', 'taxonomy.json')}


def requests(api):
    """Identify each logical call without depending on generated thread or receipt IDs."""
    return Counter((name, payload['target_rows'][0]['variable_name'], payload.get('stage', ''),
                    len(payload.get('lookahead_rows', [])), 'user_instruction' in payload['target_rows'][0])
                   for name, payload, _ in api.requests)


def failure_checks(root):
    """Recover stage boundaries, preserving fresh batches, lookahead and prior user answers."""
    baseline = root / 'baseline'
    book, session, api = fixture(baseline)
    run_app(baseline, book, session, api)
    expected, expected_calls = output(baseline), requests(api)
    assert len(api.threads) == 9 and len(api.requests) == 16 and session.answers == 1
    cases = [
        ('after_submit', 'matched', 0, 0), ('after_input_sent', 'matched', 50, None),
        ('after_output', 'matched', 0, 20), ('after_next_input', 'matched', 0, 30),
        ('before_batch', 'matched', 50, None), ('after_batch', 'matched', 50, None),
        ('after_review_answers', 'review', 0, None), ('after_output', 'review', 0, None),
        ('after_review_batch', 'review', 50, None), ('after_output', 'unmatched', 0, None),
        ('after_output', 'soft_match', 0, 'search'), ('after_next_input', 'soft_match', 0, 'reconcile'),
        ('after_input_sent', 'soft_match', 50, 'reconcile'),
        ('before_soft_batch', 'soft_match', 50, None), ('after_soft_batch', 'soft_match', 50, None),
        ('after_soft_batch', 'soft_match', 150, None), ('after_complete', 'complete', 0, None),
    ]
    for index, (point, phase, start, detail) in enumerate(cases):
        case = root / f'fault_{index}'
        book, session, api = fixture(case)
        fired = []
        def hook(actual, state):
            """Interrupt just the selected stage/loop iteration, including a lookahead turn."""
            payload = (state['agent'] or {}).get('input', {})
            current = payload.get('stage') if isinstance(detail, str) else len(payload.get('lookahead_rows', []))
            if (not fired and actual == point and state['phase'] == phase and state['start'] == start
                    and (detail is None or current == detail)):
                fired.append(True)
                raise Failure(point)
        try:
            run_app(case, book, session, api, hook)
        except Failure:
            pass
        else:
            raise AssertionError(('Fault not reached', cases[index]))
        run_app(case, book, session, api)
        assert output(case) == expected, cases[index]
        actual_calls = requests(api)
        if point == 'after_submit':
            key = ('final_codebook_creator', 'exact', '', 0, False)
            actual_calls[key] -= 1
            repeated = api.requests[1][2]
            assert 'could not recover' in repeated['input'][1]['content']
            assert repeated['conversation'] == api.requests[0][2]['conversation']
        assert actual_calls == expected_calls, (cases[index], actual_calls - expected_calls)
        assert session.answers == 1 and len(api.threads) == 9
        assert read_json(case / 'reconciliation_checkpoint.json')['phase'] == 'complete'
        assert not list(case.glob('*_draft.json')) and not list(case.glob('*conversation*'))
        before = len(api.requests)
        run_app(case, book, session, api)
        assert len(api.requests) == before and output(case) == expected
    print(f'PASS {len(cases)} interruption points; identical outputs and no completed-batch repeats')

    case = root / 'export'
    book, session, api = fixture(case)
    real_replace = Path.replace
    def fail_export(path, target):
        """Interrupt after the first final CSV is replaced; retry must rewrite every export."""
        result = real_replace(path, target)
        if Path(target).name == 'final_codebook.csv':
            raise Failure('export')
        return result
    with patch.object(Path, 'replace', fail_export):
        try:
            run_app(case, book, session, api)
        except Failure:
            pass
        else:
            raise AssertionError('Export fault not reached')
    assert read_json(case / 'reconciliation_checkpoint.json')['phase'] == 'export'
    (case / 'final_codebook.csv').write_text('partial export', encoding='utf-8')
    run_app(case, book, session, api)
    assert output(case) == expected and requests(api) == expected_calls
    print('PASS failed final export rewrites assets without agent calls')


def retry_branches(root):
    """Bad output, expired receipts and Stop must leave useful pending input/output flags."""
    for problem in ('syntax', 'soft', 'expired', 'stop'):
        case = root / problem
        book, session, api = fixture(case)
        api.bad = problem
        def hook(point, state):
            """Interrupt a known receipt, or request Stop while its response is being saved."""
            if problem == 'expired' and point == 'after_input_sent':
                raise Failure('lost local process')
            if problem == 'stop' and point == 'after_output':
                session.stopped = True
        try:
            run_app(case, book, session, api, hook)
            stopped = False
        except (Failure, ValueError, InterruptedError):
            stopped = True
        # Rejected replies are corrected in the same thread; only interruptions stop the run.
        assert stopped == (problem in ('expired', 'stop')), problem
        saved = read_json(case / 'reconciliation_checkpoint.json')
        if problem == 'stop':
            assert saved['agent']['output_received'] and not saved['matched']
            session.stopped = False
        if problem == 'expired':
            # An expired response is sent again with the recovery note instead of stopping.
            del api.responses[saved['agent']['response_id']]
        run_app(case, book, session, api)
        assert read_json(case / 'reconciliation_checkpoint.json')['phase'] == 'complete'
        assert session.answers == 1
        feedback = [payload.get('feedback', '') for _, payload, _ in api.requests]
        if problem == 'syntax':
            assert any(text.startswith('Your last response was rejected') for text in feedback)
        if problem == 'soft':
            decisions = [payload for _, payload, _ in api.requests if payload.get('stage') == 'reconcile']
            assert len(decisions) == 2 and 'own searches' in decisions[1]['feedback']
            assert sum(payload.get('stage') == 'search' for _, payload, _ in api.requests) == 1
        if problem == 'stop':
            assert len(api.requests) == 16
        assert all(request['model'] == 'gpt-6.2-sol' and request['reasoning']['effort'] == 'medium'
                   for _, _, request in api.requests)
    print('PASS malformed output, unsupported soft match, expired receipt and Stop/retry')


def run():
    """Run production orchestration against temporary assets, without paid calls or SQL."""
    with tempfile.TemporaryDirectory(prefix='reconciliation_resume_') as temporary:
        root = Path(temporary)
        failure_checks(root / 'faults')
        retry_branches(root / 'branches')
    print('PASS reconciliation checkpoints; no paid calls')


if __name__ == '__main__':
    run()
