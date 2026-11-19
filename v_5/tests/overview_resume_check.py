"""Verify the cheap Luna overview retries fully and saves only a completed result."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

from v_5.ingest.codebook_ingestion.sub_agents import overview
from v_5.ingest.codebook_ingestion.finalize import finalize_codebook
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json
from v_5.ingest.codebook_ingestion.tables import MASTER_FIELDS

ROOT = Path(__file__).resolve().parents[2]


def run():
    """Interrupt early/late samples, consolidation and saving; then reuse a finished overview."""
    names = [f'v{i}' for i in range(400)]
    session = SimpleNamespace(message=lambda *a, **k: None)
    with tempfile.TemporaryDirectory(prefix='_ov_', dir=ROOT) as temporary:
        root = Path(temporary)
        for fail_at in (1, 4, 7, 10):
            destination = root / str(fail_at) / 'planning_overview.json'
            calls = []
            def response(session, name, prompt, history, payload, contract, counter, model, **options):
                """Fail one real planning call; remember payloads to prove the full loop restarts."""
                calls.append(payload)
                assert name == 'codebook_overview_agent' and model == 'gpt-6-luna'
                assert options['output_limit'] == 1500 and not options['allow_user_questions']
                assert not history
                if len(calls) in (1, fail_at + 1):
                    assert counter['used'] == 0 and payload['sample'][0]['variable_name'] == 'v0'
                counter['used'] += 10
                if len(calls) == fail_at:
                    raise TimeoutError('Overview call interrupted')
                return {'categories': ['Toy variables'], 'note': 'Sampled planning context.'}
            with patch.object(overview, 'agent_turn', side_effect=response):
                try:
                    overview.planning_overview(session, names, {}, {}, {}, destination=destination)
                    raise AssertionError('Expected overview failure')
                except TimeoutError:
                    pass
                assert not destination.exists()
                result = overview.planning_overview(session, names, {}, {}, {}, destination=destination)
                assert len(calls) == fail_at + 10
                assert result['variables_seen'] == 360 and result['variables_total'] == 400
                assert read_json(destination) == result
                assert overview.planning_overview(session, names, {}, {}, {}, destination=destination) == result
                assert len(calls) == fail_at + 10
            print('Overview full retry passed: call', fail_at, flush=True)

        destination = root / 'disk' / 'planning_overview.json'
        with patch.object(overview, 'agent_turn', return_value={'categories': [], 'note': 'Complete'} ) as calls:
            with patch('v_5.ingest.codebook_ingestion.sub_agents.pdf_state.os.replace', side_effect=OSError('Disk unavailable')):
                try:
                    overview.planning_overview(session, names, {}, {}, {}, destination=destination)
                    raise AssertionError('Expected final-save failure')
                except OSError:
                    pass
            assert not destination.exists() and calls.call_count == 10
            result = overview.planning_overview(session, names, {}, {}, {}, destination=destination)
            assert read_json(destination) == result and calls.call_count == 20
            assert not list(destination.parent.glob('.tmp-*'))

        # Exercise the actual finalizer twice: later failure must not rerun a finished overview.
        directory = root / 'finalizer'
        directory.mkdir()
        frame = pd.DataFrame({field: [''] for field in MASTER_FIELDS})
        frame['variable_name'], frame['variable_type'] = ['v0'], ['int']
        frame.to_csv(directory / 'combined.csv', index=False)
        pd.DataFrame({'variable_name': []}).to_csv(directory / 'unmatched.csv', index=False)
        book = {'combined': str(directory / 'combined.csv'), 'unmatched': str(directory / 'unmatched.csv'), 'source_tables': {}}
        with (patch.object(overview, 'agent_turn', return_value={'categories': [], 'note': 'Complete'}) as calls,
              patch('v_5.ingest.codebook_ingestion.finalize.agent_turn', side_effect=TimeoutError('Later finalizer failed'))):
            for attempt in range(2):
                try:
                    finalize_codebook(session, {'files': {}}, {}, directory, book)
                    raise AssertionError('Expected later finalizer failure')
                except TimeoutError:
                    pass
                assert (directory / 'planning_overview.json').is_file()
                assert calls.call_count == 2  # One sample and consolidation, only on the first run.
    print('PASS: 4 full overview retries, failed atomic save, and finalizer reuse; sampling/model settings preserved. No paid calls.')


if __name__ == '__main__':
    run()
