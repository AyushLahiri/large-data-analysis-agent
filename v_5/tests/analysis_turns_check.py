"""Check analysis grouping, rollover and raw archives without model calls."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import httpx
from openai import BadRequestError

from v_5.analyse.main.agent import OrchestratorReply
from v_5.analyse.main.memory import AnalysisContextLimit, AnalysisMemory, recent_variables_text, rollover_finished_contexts, user_message
from v_5.analyse.managers.turns import apply_parents, complete_turn, initialize_turns, new_turn, remember_variables, update_turn, write_transcript
from v_5.main_functions.session import Session


def check_memory(path):
    """Check analysis turn grouping, recall and context limits offline.

    Inputs: path: temporary project directory.
    Returns: None; assertions fail if memory or grouping behavior changes.
    Details: Builds synthetic history and mocks model responses/token counts, including
        oversized context and completed-turn rollover.
    """
    state = {'messages': []}
    for i in range(1, 22):
        state['messages'] += [
            {'id': f'q{i}', 'agent': 'You', 'text': f'Question {i}'},
            {'id': f'a{i}', 'agent': 'analysis_orchestrator', 'text': f'Answer {i}',
             'details': {'method': {'steps': ['Used the household records.']}, 'decimal_places': 2}},
        ]
    assert initialize_turns(state) and not initialize_turns(state)
    old_navigation = state['turns'][0]['navigation']
    old_navigation['summary'] = '**' + ' '.join(['word'] * 50) + '**'
    assert initialize_turns(state) and not initialize_turns(state)
    assert len(old_navigation['summary'].split()) == 30 and '**' not in old_navigation['summary']
    first, second, third = (turn['id'] for turn in state['turns'][:3])
    try:
        apply_parents(state['turns'], {third: first})
    except ValueError:
        assert state['turns'][2]['parent_id'] == third
    else:
        raise AssertionError('Nonconsecutive turns were grouped.')
    apply_parents(state['turns'], {second: first, third: first})
    apply_parents(state['turns'], {second: second, third: second})
    assert [turn['parent_id'] for turn in state['turns'][:3]] == [first, second, second]

    # A later clarification can revise the parent of an already completed turn.
    group = {'turns': state['turns'][:3]}
    current = group['turns'][-1]
    update_turn(group, current, {'turn_id': third, 'parent_id': first,
                                'previous_parent_updates': [{'turn_id': second, 'parent_id': first}],
                                'navigation': current['navigation'], 'rollover': current['rollover']})
    assert [(turn['id'], turn['parent_id']) for turn in group['turns']] == [(first, first), (second, first), (third, first)]

    session = Session(path, state, analysis=True)
    memory = AnalysisMemory(session)
    history = memory.seed({'question': 'Continue the comparison.'})
    initial = json.loads(history[0]['content'])['turn_memory']
    assert len(initial['recent_summaries']) == len(initial['earlier_headers']) == 10
    assert 'bullets' in initial['recent_summaries'][0] and 'bullets' not in initial['earlier_headers'][0]
    assert memory.recall('analyst', {'turn_id': 'turn_000012', 'detail': 'full'}) == {
        'turn_id': 'turn_000012', 'question': 'Question 12', 'answer': 'Answer 12'}
    assert 'outside' in memory.recall('analyst', {'turn_id': first, 'detail': 'summary'})['error']
    full = {'turn_id': third, 'detail': 'full'}
    assert 'first' in memory.recall('analyst', full)['error']
    summary = memory.recall('analyst', {**full, 'detail': 'summary'})
    assert summary['parent_id'] == first and 'bullets' in summary and 'variables' in summary
    assert 'first' in memory.recall('analysis_orchestrator', full)['error']
    memory.recall('analysis_orchestrator', {**full, 'detail': 'summary'})
    assert memory.recall('analyst', full) == {'turn_id': third, 'question': 'Question 3', 'answer': 'Answer 3'}

    history.extend(user_message({'step': i}) for i in range(9))
    memory.budgets['analysis_orchestrator'] = {'generation': 1, 'used_tokens': 799_000}
    reply = OrchestratorReply(action='answer', message='Continuing.', request='', why_user='',
                              allow_raw_preview=False, decimal_places=2, recall=None)
    raw_reply = reply.model_dump_json(), {'input_tokens': 4_000, 'output_tokens': 40}, 'completed'
    with patch.object(session, 'call_loader', return_value=raw_reply) as call:
        memory.ask('analysis_orchestrator', 'Test prompt', history, OrchestratorReply)
        memory.ask('analysis_orchestrator', 'Test prompt', history, OrchestratorReply)
    assert memory.budgets['analysis_orchestrator'] == {'generation': 1, 'used_tokens': 807_080}
    request = call.call_args.args[1]
    assert json.loads(request['input'][1]['content'])['turn_memory'] == initial
    assert json.loads(request['input'][2]['content']) == {'step': 0}
    assert memory.recall('analysis_orchestrator', full)['question'] == 'Question 3'

    long_reason = ' '.join(f'word{i}' for i in range(45))
    for action in ('clarify', 'answer'):
        response = reply.model_copy(update={'action': action, 'why_user': long_reason})
        with patch.object(session, 'call_loader', return_value=(response.model_dump_json(), raw_reply[1], 'completed')):
            result = memory.ask('analysis_orchestrator', 'Test prompt', history, OrchestratorReply)
        expected = ' '.join(long_reason.split()[:40]) if action == 'clarify' else ''
        assert result['why_user'] == expected

    provider_limit = BadRequestError('Context too long', body={'code': 'context_length_exceeded'},
                                    response=httpx.Response(400, request=httpx.Request('POST', 'https://example.test')))
    with patch.object(session, 'call_loader', side_effect=provider_limit) as call:
        try:
            memory.ask('analysis_agent', 'Test prompt', history, OrchestratorReply)
        except AnalysisContextLimit:
            assert call.call_count == 1
            assert memory.budgets['analysis_agent']['generation'] == 1
            assert memory.budgets['analysis_agent']['limit_hit'] == 'Model context limit'
        else:
            raise AssertionError('A model context failure did not end the active question.')


def check_archive(path):
    """Check that raw turn archives remain complete and reusable.

    Inputs: path: temporary project directory.
    Returns: None; writes and verifies local transcript files.
    Details: Exercises Session events, write_transcript idempotence and later AnalysisMemory
        seeding without paid model calls.
    """
    state = {'messages': [], 'turns': []}
    turn = new_turn(state['turns'], 'How many?', 'households')
    session = Session(path, state, analysis=True, turn_id=turn['id'])
    turn['question_id'] = session.message('You', 'How many?')
    code = 'if True:\n    print(42)'
    session.record('analysis_code', 'analysis_agent', {'language': 'python', 'code': code})
    stdout = session.run_path / 'stdout.txt'
    stdout.write_text('42\n', encoding='utf-8')
    session.record('analysis_python_execution', 'analysis_agent', {'full_log': str(stdout)})
    selected = {'name': 'household_id', 'folder': 'households', 'table_name': 'households', 'column_name': 'household_id',
                'description': 'Household identifier', 'categories_or_type': 'character'}
    session.message('analysis_orchestrator', 'There are 42 households.', details={'decimal_places': 2, 'selected_variables': [selected]})
    state['analysis_context'] = {'analysis_agent': {'generation': 3, 'used_tokens': 805_000}}
    words = ' '.join(['word'] * 60)
    reply = {'turn_id': turn['id'], 'parent_id': turn['id'], 'previous_parent_updates': [],
             'navigation': {'header': words, 'summary': words}, 'variables_used': [selected],
             'rollover': {'header': words, 'bullets': ['`household_id` ' + words] * 5,
                          'variables': ['household_id']}}
    with patch.object(session, 'ask_agent', return_value=reply) as organize:
        complete_turn(session, turn)
    payload = json.loads(organize.call_args.args[2][0]['content'])
    assert payload['current_turn'] == {'turn_id': turn['id'], 'question': 'How many?', 'answer': 'There are 42 households.'}
    assert payload['selected_variables'] == [selected]
    assert turn['new_variables'] == [{'folder': 'households', 'name': 'household_id'}]
    second = {'id': 'turn_000002'}
    remember_variables(state, second, [selected], [selected])
    assert second['variables_used'] == [selected] and not second['new_variables']
    more = [{**selected, 'name': f'x{i}', 'column_name': f'x{i}'} for i in range(12)]
    remember_variables(state, {'id': 'turn_000003'}, more, more)
    assert len(state['variables_used']) == 13
    context = recent_variables_text(state, {'households'}).splitlines()
    assert len(context) == 11 and context[1].startswith('x2 |') and context[-1].startswith('x11 |')
    assert not recent_variables_text(state, {'other'})
    assert len(turn['navigation']['header'].split()) == 7
    assert len(turn['navigation']['summary'].split()) == 30
    assert len(turn['rollover']['header'].split()) == 20
    assert len(turn['rollover']['bullets']) == 4 and len(turn['rollover']['bullets'][0].split()) == 31
    assert state['analysis_context']['analysis_agent']['generation'] == 3
    rollover_finished_contexts(session)
    write_transcript(path, state, turn, session.run_path)
    assert state['analysis_context']['analysis_agent'] == {'generation': 4, 'used_tokens': 0}
    raw = (path / 'analysis/transcript.txt').read_text(encoding='utf-8')
    assert raw.startswith('------------\nTURN turn_000001\n')
    assert raw.index('turn_finished') < raw.index('analysis_context_rollover')
    assert code in raw and 'RAW PYTHON OUTPUT\n42' in raw and '"decimal_places": 2' in raw
    assert (path / 'analysis/turns/turn_000001.txt').read_text(encoding='utf-8') == raw
    next_turn = AnalysisMemory(Session(path, state, analysis=True))
    assert json.loads(next_turn.seed({})[0]['content'])['turn_memory']['recent_summaries'][0]['turn_id'] == turn['id']


if __name__ == '__main__':
    with TemporaryDirectory() as directory:
        check_memory(Path(directory) / 'memory')
        check_archive(Path(directory) / 'archive')
    print('Analysis turns, rollover and raw archive checks passed.')
