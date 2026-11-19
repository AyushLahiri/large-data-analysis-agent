"""Check conservative master-to-codebook searches with scripted replies, not paid models."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd
from v_5.ingest.codebook_ingestion.finalize import final_table, finalize_codebook, read_csv
from v_5.ingest.codebook_ingestion.tables import COLUMNS, MASTER_FIELDS
from v_5.ingest.codebook_ingestion.sub_agents import master_to_codebook as matching
from v_5.ingest.codebook_ingestion.sub_agents.agent import MasterCodebookSearchReply, MasterCodebookUpdateReply


def definition(name, **changes):
    """Create a complete final definition; individual cases change only relevant evidence."""
    return dict(variable_name=name, variable_label=changes.get('variable_label', name),
                variable_description=changes.get('variable_description', 'Original description of ' + name),
                other_information_override=changes.get('other_information_override', ''),
                battery_root=changes.get('battery_root', ''), battery_title=changes.get('battery_title', ''),
                battery_members=changes.get('battery_members', []), categories='1 Yes\n2 No',
                theme='Household', subtheme='Housing', conflicts=[])


def roster(names):
    """Build master facts that must survive every accepted or rejected soft match."""
    return pd.DataFrame([{'variable_name': name, '% missing': 7, 'variable_type': 'int',
                          'original_variable_name': name.upper(), 'table_names': 'survey'} for name in names],
                        columns=MASTER_FIELDS)


def session():
    """Provide only the logging boundaries used by these isolated finalizer checks."""
    return SimpleNamespace(state={}, message=lambda *args: None, record=lambda *args: None)


def fixture():
    """Include an exact match, a documented battery, separator variation, no hit and ambiguity."""
    names = ['exact', 'q_1', 'q_2', 'hv_201', 'ghost', 'score_1']
    master = roster(names)
    matched = {name: definition(name) for name in names}
    unmatched = {name: definition(name) for name in ('q', 'hv201', 'q10', 'score')}
    unmatched['q'].update(variable_description='q_1 rates the bedroom and q_2 rates the kitchen on the same comfort scale.',
                          other_information_override='Not matched in the initial exact-name join. Same two-room comfort battery.')
    unmatched['hv201'].update(variable_description='Source of household drinking water.')
    unmatched['score'].update(variable_description='Household score; items unspecified.')
    entries = {'exact': [{'source_id': 'book', 'other_information': ''}]}  # Even a blank exact definition is ineligible.
    return master, matched, unmatched, entries, {'Household': ['Housing']}


def candidates(payload):
    """Return bounded whole-name searches, including an intentionally unhelpful lead."""
    patterns = {'q_1': ['^q$', '^q1$', '^q-1$'], 'q_2': ['^q$'], 'hv_201': ['^hv[_-]?201$'],
                'ghost': ['^ghost$'], 'score_1': ['^score$']}
    return {'candidates': [{'variable_name': row['variable_name'], 'patterns': patterns[row['variable_name']]}
                           for row in payload['target_rows']]}


def accepted(name, source, **changes):
    """Build a complete accepted update with a short source-to-master explanation."""
    return {'row': definition(name, **changes), 'source_variables': [source],
            'reason': 'The source documents this item under its shared root; the exact join missed the suffix.'}


def conservative_flow():
    """Verify precise targeting, full evidence, abstention, battery consistency and source partitioning."""
    master, matched, unmatched, entries, taxonomy = fixture()
    original = final_table(master, deepcopy(matched), entries).set_index('variable_name')
    stages, histories = [], []

    def reply(s, name, prompt, history, payload, contract, counter, **kwargs):
        assert name == 'master_to_codebook_reconciler' and not kwargs['allow_user_questions']
        stages.append(payload['stage'])
        histories.append(history)
        targets = [row['variable_name'] for row in payload['target_rows']]
        assert 'exact' not in targets and len(targets) <= 50
        assert set(payload['target_columns']) == set(payload['target_rows'][0])
        if payload['stage'] == 'search':
            value = candidates(payload)
        else:
            assert targets == ['q_1', 'q_2', 'hv_201', 'score_1']  # ghost is not sent back to the agent.
            returned = {item['variable_name']: [row for query in item['candidates'] for row in query['rows']]
                        for item in payload['search_results']}
            assert [row['variable_name'] for row in returned['q_1']] == ['q']  # q10 must not match q1.
            assert set(returned['q_1'][0]) == set(payload['source_columns'])
            assert 'Same two-room' in returned['q_1'][0]['other_information']
            value = {'updates': [accepted('q_1', 'q', variable_label='Bedroom comfort',
                battery_root='q', battery_title='Room comfort', battery_members=['q_2'],
                other_information_override='Same comfort scale for both rooms.'),
                accepted('q_2', 'q', variable_label='Kitchen comfort',
                    battery_root='q', battery_title='Room comfort', battery_members=['q_1']),
                accepted('hv_201', 'hv201', variable_label='Drinking water source')]}
        # Exercise the actual structured-output contracts as well as the orchestration.
        value = contract.model_validate(value).model_dump()
        history.append({'role': 'assistant', 'content': json.dumps(value)})
        return value

    with patch.object(matching, 'agent_turn', side_effect=reply):
        core, remaining, soft = matching.reconcile_master_names(session(), master, matched, unmatched, entries, taxonomy, {})
    assert stages == ['search', 'reconcile'] and histories[0] is histories[1]
    core = core.set_index('variable_name')
    for name in ('exact', 'ghost', 'score_1'):
        assert core.loc[name].to_dict() == original.loc[name].to_dict()
    assert core.loc['q_1', 'variable_label'] == 'Bedroom comfort'
    assert core.loc['q_2', 'variable_label'] == 'Kitchen comfort'
    assert core.loc['q_1', 'battery_members'] == ['q_2']
    assert core.loc['q_1', 'battery_summary'] == core.loc['q_2', 'battery_summary']
    assert core.loc['hv_201', 'variable_label'] == 'Drinking water source'
    for field in MASTER_FIELDS[1:]:
        assert core[field].tolist() == original[field].tolist()
    assert remaining.variable_name.tolist() == ['q10', 'score']
    soft = soft.set_index('variable_name')
    assert soft.loc['q', 'soft_match'] == ['q_1', 'q_2']
    assert soft.loc['hv201', 'soft_match'] == ['hv_201']
    explanations = json.loads(soft.loc['q', 'other_information'].split('Soft matches: ')[1])
    assert set(explanations) == {'q_1', 'q_2'} and all(len(text.split()) < 30 for text in explanations.values())


def no_hits_and_fresh_batches():
    """No hits end a variable's work; 51 variables create two independent conversations."""
    names = [f'v_{i}' for i in range(51)]
    master, matched = roster(names), {name: definition(name) for name in names}
    sources = {'v': definition('v')}
    batches, histories, calls = [], [], []

    def reply(s, name, prompt, history, payload, contract, counter, **kwargs):
        calls.append(payload['stage'])
        if payload['stage'] == 'search':
            assert history == []
            histories.append(history)
            batches.append(len(payload['target_rows']))
            value = {'candidates': [{'variable_name': row['variable_name'], 'patterns': ['^v$']}
                                    for row in payload['target_rows']]}
            history.append({'role': 'assistant', 'content': json.dumps(value)})
            return value
        target = payload['target_rows'][0]['variable_name']
        return {'updates': [accepted(target, 'v')]}

    with patch.object(matching, 'agent_turn', side_effect=reply):
        _, _, soft = matching.reconcile_master_names(session(), master, matched, sources, {}, {}, {})
    assert batches == [50, 1] and histories[0] is not histories[1]
    assert calls == ['search', 'reconcile', 'search', 'reconcile']
    assert soft.iloc[0]['soft_match'] == ['v_0', 'v_50']  # The same root remains searchable in later batches.

    def no_hit(s, name, prompt, history, payload, contract, counter, **kwargs):
        assert payload['stage'] == 'search'
        return {'candidates': [{'variable_name': row['variable_name'], 'patterns': ['^absent$']}
                               for row in payload['target_rows']]}

    with patch.object(matching, 'agent_turn', side_effect=no_hit) as calls:
        _, remaining, soft = matching.reconcile_master_names(session(), master, matched, sources, {}, {}, {})
        assert calls.call_count == 2 and soft.empty and remaining.variable_name.tolist() == ['v']
    with patch.object(matching, 'agent_turn', side_effect=AssertionError('No source candidates means no calls')):
        _, remaining, soft = matching.reconcile_master_names(session(), master, matched, {}, {}, {}, {})
        assert remaining.empty and soft.empty


def rejected_payloads():
    """Reject unsupported targets, uncited sources and long explanations without applying them."""
    for bad in (accepted('exact', 'q'), accepted('ghost', 'q'), accepted('q_1', 'not_returned'),
                {**accepted('q_1', 'q'), 'reason': 'word ' * 30}):
        master, matched, unmatched, entries, taxonomy = fixture()
        before = deepcopy(matched)

        def reply(s, name, prompt, history, payload, contract, counter, **kwargs):
            return candidates(payload) if payload['stage'] == 'search' else {'updates': [bad]}

        with patch.object(matching, 'agent_turn', side_effect=reply):
            try:
                matching.reconcile_master_names(session(), master, matched, unmatched, entries, taxonomy, {})
                raise AssertionError('Unsupported update was accepted.')
            except ValueError:
                pass
        assert matched == before
    rows = [{'variable_name': 'q1', 'other_information': 'Full row retained'}, {'variable_name': 'q10'}]
    found = matching.search_candidates([{'variable_name': 'q_1', 'patterns': ['^q1$', '.*', '^q[_-]?1$']}], ['q_1'], rows)
    assert found[0]['candidates'][0]['rows'] == [rows[0]]
    assert found[0]['candidates'][1]['rows'] == [] and found[0]['candidates'][1]['error']
    assert found[0]['candidates'][2]['rows'] == [rows[0]]
    assert matching.compile_candidate(r'^score\.v2$').fullmatch('SCORE.v2')
    try:
        MasterCodebookSearchReply.model_validate({'candidates': [{'variable_name': 'q', 'patterns': ['a'] * 4}]})
        raise AssertionError('Four variations were accepted.')
    except ValueError:
        pass


def finalizer_integration():
    """Exercise the finalizer's actual new call and all three exported CSV tables."""
    with tempfile.TemporaryDirectory(prefix='_master_codebook_') as temporary:
        path = Path(temporary)
        master = roster(['q_1', 'q_2', 'exact'])
        master.to_csv(path / 'combined.csv', index=False)
        pd.DataFrame({'variable_name': ['q', 'unused']}).to_csv(path / 'unmatched.csv', index=False)
        source = pd.DataFrame([dict(variable_name=name, **{field + '_src': '' for field in COLUMNS[1:]})
                               for name in ['q', 'unused', 'exact']])
        source.to_csv(path / 'source.csv', index=False)
        book = {'combined': str(path / 'combined.csv'), 'unmatched': str(path / 'unmatched.csv'),
                'source_tables': {'src': {'filename': 'book.csv', 'table': str(path / 'source.csv')}}}

        def final_reply(s, name, prompt, history, payload, contract, counter, **kwargs):
            return {'require_further_battery_rows': False,
                    'rows': [definition(row['variable_name']) for row in payload['target_rows']]}

        def soft_reply(s, name, prompt, history, payload, contract, counter, **kwargs):
            assert [row['variable_name'] for row in payload['target_rows']] == ['q_1', 'q_2']
            if payload['stage'] == 'search':
                return {'candidates': [{'variable_name': key, 'patterns': ['^q$']} for key in ['q_1', 'q_2']]}
            return {'updates': [accepted('q_1', 'q', variable_label='Confirmed item one')]}

        with patch('v_5.ingest.codebook_ingestion.finalize.planning_overview', return_value={'overview': '', 'variables_seen': 3}), \
                patch('v_5.ingest.codebook_ingestion.finalize.agent_turn', side_effect=final_reply), \
                patch.object(matching, 'agent_turn', side_effect=soft_reply):
            finalize_codebook(session(), {'files': {}}, {}, path, book)
        core = read_csv(book['final']).set_index('variable_name')
        assert core.loc['q_1', 'variable_label'] == 'Confirmed item one'
        assert core.loc['q_2', 'variable_label'] == 'q_2' and core.loc['exact', 'variable_label'] == 'exact'
        assert read_csv(book['final_unmatched']).variable_name.tolist() == ['unused']
        soft = read_csv(book['soft_matched'])
        assert soft.variable_name.tolist() == ['q'] and json.loads(soft.iloc[0]['soft_match']) == ['q_1']


def main():
    """Run four focused offline checks of the new agent's contracts and production paths."""
    for check in (conservative_flow, no_hits_and_fresh_batches, rejected_payloads, finalizer_integration):
        check()
        print('PASS', check.__name__, flush=True)
    print('4 focused master/codebook checks passed; scripted model replies, no paid calls.', flush=True)


if __name__ == '__main__':
    main()
