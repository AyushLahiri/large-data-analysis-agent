"""Check the final codebook workflow without calling models."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pandas as pd

from v_5.ingest.codebook_ingestion.tables import COLUMNS
from v_5.ingest.codebook_ingestion.finalize import add_theme, finalize_codebook, read_csv
from v_5.main_functions.session import write_json


def run():
    """Check final-codebook batches, conflict review and taxonomy limits.

    Inputs: No arguments; creates temporary source evidence and scripted responses.
    Returns: None; prints success or raises an assertion.
    Details: Exercises phase 4 planning, 50-row batches, battery lookahead, user corrections
        and unmatched variables without model calls.
    """
    names = [f'v{i}' for i in range(110)]
    names[49], names[98] = 'q_1', 'q_2'
    extras = [f'old_field_{i}' for i in range(51)]
    extra = extras[0]
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        book = {'combined': str(root/'combined.csv'), 'unmatched': str(root/'unmatched.csv'), 'source_tables': {}}
        pd.DataFrame({'variable_name': names, '% missing': 20, 'original_variable_name': names,
                      'table_names': 'prepared'}).to_csv(book['combined'], index=False)
        pd.DataFrame({'variable_name': [*extras, extra]}).to_csv(book['unmatched'], index=False)
        for key, priority in [('emb', {'embedded_for': 'data'}), ('pri', {'primary': True})]:
            fields = {f'{col}_{key}': ['A note', ''] if col == 'other_information' else ['', ''] for col in COLUMNS[1:]}
            path = root/f'{key}.csv'
            pd.DataFrame({'variable_name': [names[0], extra], **fields}).to_csv(path, index=False)
            book['source_tables'][key] = {**priority, 'filename': path.name, 'table': str(path)}
        write_json(root/'manifest.json', {'tables': [{'columns': [{'name': name, 'type': 'numeric'} for name in names]}]})

        class Session:
            def message(self, *args):
                """Skip UI/journal output in the final-codebook test.

                Inputs: args: ignored session arguments.
                Returns: None.
                Details: Test-only stub; saved codebook artifacts are checked directly.
                """
                pass
            def record(self, *args):
                """Skip UI/journal output in the final-codebook test.

                Inputs: args: ignored session arguments.
                Returns: None.
                Details: Test-only stub; saved codebook artifacts are checked directly.
                """
                pass
            def answer(self, prompt):
                """Simulate the user resolving a codebook conflict.

                Inputs: prompt: review prompt; uses the fake session's question_form.
                Returns: Variable-to-instruction dict selecting dollars; marks the review as seen.
                Details: Asserts that phase 4 presents the specialized codebook-review form.
                """
                assert self.state['question_form']['kind'] == 'codebook_review'
                self.reviewed = True
                return {names[0]: 'Use dollars'}

        session = Session()
        session.state, session.reviewed = {}, False
        extensions, batches, histories = [], [], {}

        def response(session, name, prompt, history, payload, contract, counter, model=None, **options):
            """Supply scripted planning and final-codebook agent replies.

            Inputs: session/name/prompt/history/payload/contract/counter/model/options: agent_turn
                interface.
            Returns: Overview or final-row response matching the requested role.
            Details: Checks batch isolation and lookahead, injects a supported conflict and returns
                reviewed/unmatched definitions for phase-4 assertions.
            """
            if name == 'master_to_codebook_reconciler':
                assert payload['stage'] == 'search'
                assert names[0] not in [row['variable_name'] for row in payload['target_rows']]
                return {'candidates': [{'variable_name': row['variable_name'], 'patterns': []} for row in payload['target_rows']]}
            if name == 'codebook_overview_agent':
                assert 'sample' not in payload or len(payload['sample']) <= 50
                return {'categories': ['Energy bills'], 'note': 'A planning hint'}
            assert '110 of 110' in payload['planning_warning']
            assert len(payload['recent_rows']) <= 10
            assert payload['column_key']['sources_by_id']['emb']['priority'] == 1
            variables = [row['variable_name'] for row in payload['target_rows']]
            assert options.get('allow_rollover', False) is False
            thread = counter['thread']
            if thread not in histories:
                assert not history and counter['used'] == 0
                histories[thread] = history
            else:
                assert history is histories[thread] and history
            history.append({'role': 'assistant', 'content': 'Prior reply for this batch.'})
            review = 'user_instruction' in payload['target_rows'][0]
            if review:
                assert payload['target_rows'][0]['user_instruction'] == 'Use dollars'
            elif variables == names[:50]:
                extensions.append(len(payload['lookahead_rows']))
                if payload['can_request_more_battery_rows']:
                    return {'require_further_battery_rows': True, 'rows': []}
            elif variables == names[50:100]:
                assert [row['variable_name'] for row in payload['recent_rows']] == names[40:50]
            elif variables[0] in extras:
                assert session.reviewed and payload['taxonomy'] == {'Energy': ['Usage', 'Comfort']}
            if not review:
                batches.append(variables)
            rows = []
            for variable in variables:
                conflicts = [{'field': 'categories', 'reason': 'Units differ', 'evidence': [
                    {'source_id': 'emb', 'excerpt': 'dollars'}, {'source_id': 'pri', 'excerpt': 'cents'}]}] if variable == names[0] and not review else []
                rows.append({'variable_name': variable, 'variable_label': variable.upper(), 'variable_description': f'Description of {variable}',
                             'other_information_override': '', 'categories': 'dollars' if review else 'numeric',
                             'battery_root': 'q' if variable == 'q_1' else '', 'battery_title': 'Home comfort' if variable == 'q_1' else '',
                             'battery_members': ['q_2'] if variable == 'q_1' else [], 'theme': 'Energy',
                             'subtheme': 'Comfort' if variable == 'q_1' else 'Usage', 'conflicts': conflicts})
            return {'require_further_battery_rows': False, 'rows': rows}

        with (patch('v_5.ingest.codebook_ingestion.finalize.agent_turn', side_effect=response),
              patch('v_5.ingest.codebook_ingestion.sub_agents.master_to_codebook.agent_turn', side_effect=response),
              patch('v_5.ingest.codebook_ingestion.sub_agents.overview.agent_turn', side_effect=response)):
            finalize_codebook(session, {'files': {'data': {'filename': 'source.sav'}}},
                              {'manifest_path': '/work/manifest.json'}, root, book)
        assert extensions == [0, 10, 20, 30, 40, 50]
        assert batches == [names[:50], names[50:100], names[100:], extras[:50], extras[50:]]
        assert len(histories) == 6 and len(histories[1]) == 6
        final = read_csv(book['final']).set_index('variable_name')
        assert final.index.tolist() == names and (final['% missing'] == '20').all()
        assert (final['variable_type'] == 'numeric').all()
        assert final.loc[names[0], 'categories'] == 'dollars' and final.loc[names[0], 'other_information'] == 'A note'
        assert json.loads(final.loc['q_1', 'battery_members']) == ['q_2']
        assert json.loads(final.loc['q_2', 'battery_members']) == ['q_1']
        assert final.loc['q_1', 'battery_summary'] == final.loc['q_2', 'battery_summary']
        assert final.loc['q_1', 'subtheme'] == final.loc['q_2', 'subtheme'] == 'Comfort'
        unmatched = read_csv(book['final_unmatched'])
        assert unmatched.variable_name.tolist() == extras and (unmatched.variable_type == '').all()
        assert read_csv(book['soft_matched']).empty

    theme = {'Energy': [f'Subtheme {i}' for i in range(15)]}
    try:
        add_theme(theme, {'theme': 'energy', 'subtheme': 'New subtheme'})
    except ValueError:
        pass
    else:
        raise AssertionError('Subtheme cap was not enforced.')
    print('PASS: fresh 50-row batches, same-session lookahead, cross-batch batteries, review, and unmatched taxonomy.')


if __name__ == '__main__':
    run()
