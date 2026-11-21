"""Check analysis grouping, rollover and raw archives without model calls."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from openai import BadRequestError

from v_5.analyse.main.agent import OrchestratorReply
from v_5.analyse.main.memory import AnalysisMemory, recent_variables_text, rollover_finished_contexts, user_message
from v_5.analyse.main.workflow import analyse, format_answer
from v_5.analyse.managers.turns import apply_parents, complete_turn, initialize_turns, new_turn, remember_variables, update_turn, write_transcript
from v_5.main_functions.session import Session, write_json
from v_5.tests.delete_project_check import client_for


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
    reply = OrchestratorReply(action='answer', message='Continuing.', request='', why_user='',
                              allow_raw_preview=False, decimal_places=2, recall=None)
    usage = {'input_tokens': 4_000, 'output_tokens': 40}
    response = reply.model_dump_json(), usage, 'completed'
    with patch.object(session, 'analysis_request', side_effect=[SimpleNamespace(id=f'thread-{i}') for i in range(8)]), \
         patch.object(session, 'call_loader', return_value=response) as call:
        memory = AnalysisMemory(session)
        # Migrate this existing local chat with raw Q&A. Only the newly submitted
        # messages are sent to the same API thread on subsequent calls or questions.
        history = [user_message({'question': 'Continue the comparison.'})]
        memory.ask('analysis_orchestrator', 'Test prompt', history, OrchestratorReply)
        first_request = call.call_args.args[1]
        assert first_request['conversation'] == 'thread-0' and 'Question 1' in str(first_request['input'])
        assert 'turn_memory' not in str(first_request['input'])
        history.append(user_message({'turn_recall': 'specific evidence'}))
        memory.ask('analysis_orchestrator', 'Test prompt', history, OrchestratorReply)
        assert call.call_args.args[1]['input'] == [user_message({'turn_recall': 'specific evidence'})]
        assert memory.budgets['analysis_orchestrator']['used_tokens'] == 4040

        # Each group keeps all its inputs and outputs, even when it has a recall round trip.
        for i in range(16, 22):
            session.turn_id = f'turn_{i:06d}'
            sequence = [user_message({'question': f'Q{i}'}), {'role': 'assistant', 'content': 'request recall'},
                        user_message({'turn_recall': f'evidence {i}'}), {'role': 'assistant', 'content': 'delegate'}]
            memory.remember_orchestrator(sequence)
        expected_sets = memory.budgets['analysis_orchestrator']['recent_sets']
        assert len(expected_sets) == 5 and expected_sets[0]['turn_id'] == 'turn_000017'
        session.turn_id = 'turn_000022'
        current = [user_message({'question': 'Next question'})]
        memory = AnalysisMemory(session)
        memory.ask('analysis_orchestrator', 'Orchestrate', current, OrchestratorReply)
        assert call.call_args.args[1]['conversation'] == 'thread-0'
        assert call.call_args.args[1]['input'] == current[:1]

        analyst = [user_message({'question': 'Analyse this', 'recent_variables': 'households/id'})]
        memory.ask('analysis_agent', 'Analyse', analyst, OrchestratorReply)
        assert call.call_args.args[1]['conversation'] == 'thread-1'
        assert 'recent_variables' in str(call.call_args.args[1]['input']) and 'turn_memory' not in str(call.call_args.args[1]['input'])
        # A fresh AnalysisMemory reflects a new question, including after a restart.
        memory = AnalysisMemory(session)
        followup = [user_message({'question': 'And by region?', 'recent_variables': 'households/region'})]
        memory.ask('analysis_agent', 'Analyse', followup, OrchestratorReply)
        assert call.call_args.args[1]['conversation'] == 'thread-1' and call.call_args.args[1]['input'] == followup[:1]

        # A single oversized analyst request rolls immediately without abandoning the current work.
        provider_limit = BadRequestError('Context too long', body={'code': 'context_length_exceeded'},
            response=httpx.Response(400, request=httpx.Request('POST', 'https://example.test')))
        followup.append(user_message({'execution_result': '42'}))
        with patch.object(session, 'call_loader', side_effect=[provider_limit, response]) as retry:
            memory.ask('analysis_agent', 'Analyse', followup, OrchestratorReply)
        rebuilt = retry.call_args.args[1]
        assert rebuilt['conversation'] == 'thread-2' and 'recall_turn' in rebuilt['instructions']
        seed = json.loads(rebuilt['input'][0]['content'])['turn_memory']
        assert [entry['turn_id'] for entry in seed['recent_summaries']] == list(memory.previous)[-10:]
        assert [entry['turn_id'] for entry in seed['earlier_headers']] == list(memory.previous)[-20:-10]
        assert 'bullets' in seed['recent_summaries'][0] and 'bullets' not in seed['earlier_headers'][0]
        assert rebuilt['input'][1:] == followup[:-1]
        assert memory.recall('analysis_agent', {'turn_id': 'turn_000012', 'detail': 'full'})['answer'] == 'Answer 12'
        full = {'turn_id': third, 'detail': 'full'}
        assert 'first' in memory.recall('analysis_agent', full)['error']
        assert memory.recall('analysis_agent', {**full, 'detail': 'summary'})['parent_id'] == first
        assert memory.recall('analysis_agent', full)['question'] == 'Question 3'
        assert 'error' in memory.recall('analysis_agent', {**full, 'turn_id': 'turn_000001'})
        further = [user_message({'question': 'More detail'})]
        AnalysisMemory(session).ask('analysis_agent', 'Analyse', further, OrchestratorReply)
        assert call.call_args.args[1]['input'] == further[:1] and call.call_args.args[1]['conversation'] == 'thread-2'

        # The orchestrator reaches the limit independently. Count the combined window,
        # replay exactly five entire role exchanges, and retain the current question.
        memory.budgets['analysis_orchestrator']['used_tokens'] = 795_000
        question = [user_message({'question': 'One more question'})]
        with patch.object(session, 'count_input', return_value=799_999) as count:
            AnalysisMemory(session).ask('analysis_orchestrator', 'Orchestrate', question, OrchestratorReply)
        assert count.call_args.args[0]['conversation'] == 'thread-0'
        rebuilt = call.call_args.args[1]
        assert rebuilt['conversation'] == 'thread-3' and 'turn_memory' not in str(rebuilt['input'])
        assert rebuilt['input'] == [msg for entry in expected_sets for msg in entry['messages']] + question[:1]
        assert memory.budgets['analysis_agent']['thread_id'] == 'thread-2'
        long_reason = ' '.join(f'word{i}' for i in range(45))
        for action in ('clarify', 'answer'):
            result = reply.model_copy(update={'action': action, 'why_user': long_reason})
            with patch.object(session, 'call_loader', return_value=(result.model_dump_json(), usage, 'completed')):
                parsed = AnalysisMemory(session).ask('analysis_orchestrator', 'Test', [user_message({})], OrchestratorReply)
            assert parsed['why_user'] == (' '.join(long_reason.split()[:40]) if action == 'clarify' else '')


def check_formatter_workflow(root):
    """Exercise two delegated questions, a direct answer, and formatter failure with scripted calls."""
    state = {'messages': [], 'turns': [], 'variables_used': [
        {'folder': 'survey', 'table_name': 'data', 'column_name': f'v{i}', 'name': f'v{i}',
         'description': 'Documented variable', 'categories_or_type': 'numeric'} for i in range(12)]}
    project = {'id': 'a' * 32, 'context': {'name': 'Survey'}, 'atlas': {'data': []}}
    path = root / 'projects' / project['id']
    chat_id = 'b' * 32
    chat_dir = path / 'analysis_chats' / chat_id
    write_json(path / 'project.json', project)
    catalog = Mock()
    catalog.folders = {'survey': {'name': 'Survey', 'tables': []}}
    catalog.context.return_value = {'folders': catalog.folders}
    finish = dict(action='finish', message='The codebook gives the unit as years.', exact_names=[],
                  surface_all_battery=[], themes_for=[], search_for=[], folder='', skill='', weights=[],
                  code='', frames=[], tables=[], ran_analysis=False, method={'steps': [], 'variables': []},
                  join_success='no', join_folders=[], join_rules_discovered=[], recall=None)
    requests = []

    def respond(role, request, _tokens, *, allow_user_questions):
        assert not allow_user_questions
        requests.append((role, request))
        if role == 'analysis_orchestrator':
            question = json.loads(request['input'][-1]['content'])['question']
            direct = question == 'Hello'
            reply = dict(action='answer' if direct else 'analyse', request=question, message='Hello!',
                         why_user='', decimal_places=4, allow_raw_preview=False, recall=None)
        elif role == 'analysis_agent':
            payload = json.loads(request['input'][-1]['content'])
            entries = payload['recent_variables'].splitlines()[1:]
            assert len(entries) == 10 and entries[0].startswith('v2 |') and entries[-1].startswith('v11 |')
            reply = finish
        else:
            assert role == 'formatter'
            assert request['model'] == 'gpt-6-luna' and request['reasoning'] == {'effort': 'xhigh'}
            assert 'conversation' not in request and len(request['input']) == 2
            material = json.loads(request['input'][1]['content'])
            assert material['display_decimal_places'] == 4 and material['analyst_response']['message'] == finish['message']
            assert 'join_success' not in material['analyst_response']
            reply = {'message': 'The unit is years.'}
        if role != 'formatter':
            assert request['model'] == 'gpt-6.2-sol'
        return json.dumps(reply), {'input_tokens': 100, 'output_tokens': 20}, 'completed'

    with patch('v_5.analyse.main.workflow.published_scope', return_value={}), \
         patch('v_5.analyse.main.workflow.AtlasCatalog', return_value=catalog), \
         patch.object(Session, 'analysis_request', side_effect=[SimpleNamespace(id='orch'), SimpleNamespace(id='analyst')]), \
         patch.object(Session, 'call_loader', side_effect=respond):
        for question in ('What is the unit?', 'For the same measure?', 'Hello'):
            turn = new_turn(state['turns'], question, 'survey')
            session = Session(path, state, analysis=True, turn_id=turn['id'], analysis_directory=chat_dir)
            turn['question_id'] = session.message('You', question)
            analyse(session, project, 'survey', question)
            answer = state['messages'][-1]
            turn['answer_id'] = answer['id']
            if question != 'Hello':
                assert answer['agent'] == 'formatter' and answer['text'] == 'The unit is years.'
                assert answer['details']['decimal_places'] == 4
            session.save()
            state = json.loads(session.state_path.read_text(encoding='utf-8'))
        assert [role for role, _ in requests] == ['analysis_orchestrator', 'analysis_agent', 'formatter'] * 2 + ['analysis_orchestrator']
        assert [request.get('conversation') for _, request in requests] == ['orch', 'analyst', None] * 2 + ['orch']
        assert all('turn_memory' not in str(request['input']) for _, request in requests)
    with patch.object(session, 'call_loader', return_value=('{}', {}, 'incomplete')):
        assert format_answer(session, 'Question?', finish, None) == ('analysis_agent', finish['message'])
    state.update(status='idle', atlas_archived_through=3)
    write_json(session.state_path, state)
    client, headers, _ = client_for(root)
    base = f"/api/projects/{project['id']}/analysis/chats/{chat_id}"
    with patch('v_5.analyse.managers.routes.save_turn_feedback') as saved:
        result = client.post(base + '/turns/turn_000001/feedback', json={'rating': 'up'}, headers=headers)
        assert result.status_code == 200 and result.json == {'rating': 'up'}
        saved.assert_called_once_with(project['id'], chat_id, 'turn_000001', {'rating': 'up'})


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
    seed = next_turn.starting_messages('analysis_agent', next_turn.budgets['analysis_agent'])
    assert json.loads(seed[0]['content'])['turn_memory']['recent_summaries'][0]['turn_id'] == turn['id']


if __name__ == '__main__':
    with TemporaryDirectory(prefix='_analysis_', dir=Path(__file__).resolve().parents[2]) as directory:
        check_memory(Path(directory) / 'memory')
        check_formatter_workflow(Path(directory) / 'formatter')
        check_archive(Path(directory) / 'archive')
    print('Analysis threads, rollover, formatter routing and raw archive checks passed.')
