"""Exercise table identity, visualizer reuse, iterative Python, and browser controls in an isolated chat."""
import base64
import json
import struct
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

from v_5.analyse.main.sub_agents.errors import AnalysisExecutionLimit, error_feedback
from v_5.analyse.main.sub_agents.visualizer import validated_views
from v_5.analyse.managers.tables import save_tables, selected_table, table_file
from v_5.analyse.managers.turns import new_turn
from v_5.main import create_app
from v_5.main_functions.session import Session, write_json
from v_5.tests.reuse_check import project
from v_5.tests.ui_check import organize_turn, publish


DRAFT = """
assert df.columns.tolist() == ['Month', 'Count']
print('Source total', df['Count'].sum())
views = []
for name, factor in [('Original', 1), ('In hundreds', .01)]:
    values = [{'value': y*factor, 'sourceRows': [i], 'annotation': 'Revised count' if i == 1 else ''}
              for i, y in enumerate(df['Count'])]
    views.append(dict(title=name, kind='Line chart', option={
        'xAxis': {'type': 'category', 'name': 'Month', 'data': df['Month'].tolist()},
        'yAxis': {'type': 'value', 'name': 'Count' if factor == 1 else 'Hundreds'},
        'series': [{'type': 'line', 'name': 'Observations', 'data': values}]}))
"""


def run():
    """Exercise the table-to-chart workflow with scripted agent responses.

    Inputs: A local Chrome installation, Playwright and the v5 Docker sandbox image.
    Returns: None. Assertions identify failures in table identity, agent routing,
        draft review, source linkage or browser interaction.
    Flow: Seed a temporary chat with two tables, click Visualize and request a
        revision. Use real Python execution and Chrome rendering for both turns.
    """
    with TemporaryDirectory() as temp:
        path, proj = project(Path(temp), 97, [('households', 'source.csv', b'x\n1\n', 'primary', 'data')])
        publish(SimpleNamespace(state=proj, message=lambda *a: None), {'release': 'example'})
        proj['status'] = 'complete'
        write_json(path / 'project.json', proj)
        chat_id = 'b' * 32
        directory = path / 'analysis_chats' / chat_id
        state = {'status': 'idle', 'messages': [], 'turns': [], 'variables_used': []}
        def add(question, text, tables=()):
            turn = new_turn(state['turns'], question, 'households')
            session = Session(path, state, analysis=True, turn_id=turn['id'], analysis_directory=directory)
            turn['question_id'] = session.message('You', question)
            turn['answer_id'] = session.message('formatter', text, details={'tables': list(tables)})
            return turn
        t1 = add('Context for this series', 'Monthly observations use the same population.')
        t2 = add('Show both tables', '| Area | Amount |\n|---|---|\n| North | 550 |',
                 [{'title': 'Monthly counts', 'columns': ['Month', 'Count'], 'rows': [['Jan', 10], ['Feb', 25]]}])
        add('Separate topic', 'Unrelated archive entry.')
        # Saved UTC message times also drive the timer when reopening a chat.
        for message, stamp in zip(state['messages'][:4],
                                  ['08:00:00', '08:00:59', '08:02:00', '08:03:05']):
            message['time'] = '2026-01-01T' + stamp + '+00:00'
        save_tables(directory, chat_id, state)
        refs = state['messages'][3]['details']['table_assets']
        expected_id = f"{chat_id}_{t2['id']}_{t2['id']}_table_002"
        assert refs[1]['table_id'] == expected_id and refs[0]['table_id'] != expected_id
        t2['parent_id'] = t1['id']
        assert not save_tables(directory, chat_id, state)
        del state['messages'][3]['details']['table_assets']
        assert save_tables(directory, chat_id, state, restore=True)
        assert state['messages'][3]['details']['table_assets'][1]['table_id'] == expected_id
        table_file(directory, expected_id).unlink()
        assert selected_table(directory, state, expected_id)['rows'][1][1] == 25
        for bad, scope in [(expected_id.replace(chat_id, 'a' * 32), ''), (expected_id, 'other')]:
            try:
                selected_table(directory, state, bad, scope)
            except ValueError:
                pass
            else:
                raise AssertionError('Unavailable table resolved')
        write_json(directory / 'session.json', state)
        calls, briefings, creations, glitch_reports = [], [], [], []
        active_id = 'conv_visualizer_test'
        def agent_request(self, callback):
            def create():
                creations.append(1)
                return SimpleNamespace(id=active_id)
            return callback(SimpleNamespace(conversations=SimpleNamespace(create=create)))
        def memory_ask(self, role, prompt, history, contract):
            payload = json.loads(history[-1]['content'])
            if role == 'analysis_orchestrator':
                self.budgets.setdefault(role, {'generation': 1, 'used_tokens': 0})
                assert payload['recent_visualizations'][-1]['table_id'] == expected_id
                return {'action': 'visualize', 'decimal_places': 2, 'request': expected_id}
            assert role == 'analysis_agent'
            assert payload['selected_table']['table_id'] == expected_id and 'parent_conversation_for_context_only' not in payload
            assert payload['source_turn_summary']['turn_id'] == t2['id']
            briefings.append(payload)
            return {'guidance': ''}
        reviewed_plots = []
        def model(self, name, request, *args, **kwargs):
            assert name == 'visualizer' and request['conversation'] == active_id
            assert request['model'] == 'gpt-6.2-sol' and request['reasoning']['effort'] == 'high'
            content = request['input'][0]['content']
            if isinstance(content, str):
                payload = json.loads(content)
                if 'selected_table' in payload:
                    assert payload['selected_table']['rows'] == [['Jan', 10], ['Feb', 25]]
                    context = json.dumps(payload['parent_conversation_for_context_only'])
                    assert 'Monthly observations' in context and 'North' in context and 'Unrelated archive entry' not in context
                    if len(briefings) == 2:
                        assert DRAFT in payload['previous_visualization_for_this_table']['python']
                        assert payload['user_request'] == 'Keep the chart, label the February revision.'
                    action = 'python'
                    code = "print(df['missing field'])" if len(briefings) == 1 else "print('rows', len(df))"
                elif 'error_review' in payload:
                    assert payload['error_review']['error_kind'] == 'KeyError' and 'missing field' in payload['execution_error']
                    action, code = 'python', "print('rows', len(df))"
                else:
                    raise AssertionError(payload)
            else:
                info = json.loads(content[0]['text'])
                if info['views'] == 0:
                    assert 'rows 2' in info['python_output'] and 'pandas 2.2.3' in info['python_output']
                    action, code = 'python', DRAFT
                else:
                    assert info['views'] == 2 and 'Source total 35' in info['python_output']
                    for diagnostic, attachment in zip(content[1::2], content[2::2], strict=True):
                        report = json.loads(diagnostic['text'])
                        assert report['chat_card_width_px'] == measured_width
                        assert 'clipped_text' in report and 'possible_text_overlaps' in report
                        assert attachment['type'] == 'input_image' and attachment['detail'] == 'high'
                        png = base64.b64decode(attachment['image_url'].split(',', 1)[1])
                        assert abs(struct.unpack('>I', png[16:20])[0] - measured_width * 1.5) <= 1
                        reviewed_plots.append(report['plot_px'])
                    if len(briefings) == 1 and 'revised draft' not in info['python_output']:
                        action, code = 'python', DRAFT + "\nprint('revised draft')"
                    else:
                        action, code = 'publish', ''
            calls.append(action)
            return json.dumps({'action': action, 'message': 'Monthly counts.', 'code': code}), {}, 'completed'
        ask_agent = Session.ask_agent
        def supporting_agent(session, name, prompt, history, contract, *, allow_user_questions, conversation=None):
            if name == 'visualizer':
                return ask_agent(session, name, prompt, history, contract,
                                 allow_user_questions=allow_user_questions, conversation=conversation)
            if name != 'glitch_agent':
                return organize_turn(session, name, prompt, history, contract, allow_user_questions=allow_user_questions)
            report = json.loads(history[0]['content'])
            assert report['requesting_agent'] == 'visualizer' and '1: print' in report['executed_code']
            assert 'missing field' in report['error_output']
            glitch_reports.append(report)
            return {'error_kind': 'KeyError', 'actual_error': 'missing field', 'failing_lines': ["1: print(df['missing field'])"],
                    'explanation': 'This field does not occur in the selected table.'}
        route_mod = 'v_5.analyse.managers.routes'
        with (patch(route_mod + '.list_chats', return_value=[{'id': chat_id, 'title': 'Test'}]),
              patch(route_mod + '.archive_chat', side_effect=lambda *args, **kwargs: len(args[2]['turns'])),
              patch(route_mod + '.AtlasCatalog') as catalog,
              patch('v_5.analyse.main.workflow.published_scope', return_value={}),
              patch('v_5.analyse.main.memory.AnalysisMemory.ask', memory_ask),
              patch.object(Session, 'analysis_request', agent_request), patch.object(Session, 'call_loader', model),
              patch.object(Session, 'ask_agent', supporting_agent)):
            catalog.return_value.artifact_tree.return_value = {}
            app = create_app(temp)
            server = make_server('127.0.0.1', 0, app, threaded=True)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                origin = f'http://127.0.0.1:{server.server_port}'
                with sync_playwright() as pw:
                    browser = pw.chromium.launch(channel='chrome', headless=True)
                    page = browser.new_page(viewport={'width': 1320, 'height': 960})
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    def open_chat():
                        page.goto(origin)
                        page.locator('#projects').select_option(proj['id'])
                        expect(page.locator('#analysis-chat table[id]')).to_have_count(2)
                        saved_times = page.get_by_role('timer', name='Response time').locator('time')
                        expect(saved_times.nth(0)).to_have_text('59 s')
                        expect(saved_times.nth(1)).to_have_text('1 min 5 s')
                    open_chat()
                    original = page.locator('#analysis-chat table').nth(1)
                    assert original.get_attribute('id') == expected_id
                    measured_width = original.evaluate('el => Math.round(el.closest(".message").getBoundingClientRect().width)')
                    page.get_by_role('button', name='Visualize Monthly counts').click()
                    ticking = page.get_by_role('timer', name='Time awaiting response').locator('time')
                    expect(ticking).to_be_visible()
                    first_tick = ticking.inner_text()
                    expect(ticking).not_to_have_text(first_tick)
                    card = page.locator('.viz-card').first
                    expect(card.locator('svg')).to_be_visible(timeout=120000)
                    expect(ticking).to_have_count(0)
                    reply_timer = page.get_by_role('timer', name='Response time').nth(3)
                    expect(reply_timer.locator('.response-time-icon')).to_be_visible()
                    completed_time = reply_timer.locator('time').inner_text()
                    live_plot = card.locator('.viz-plot').bounding_box()
                    assert abs(live_plot['width'] - reviewed_plots[0][0]) <= 1
                    assert live_plot['height'] == reviewed_plots[0][1]
                    assert card.get_attribute('data-table-id') == expected_id
                    view2 = card.get_by_role('button', name='Line chart 2')
                    card.get_by_role('button', name='Woodland').click()
                    view2.click()
                    expect(card.locator('h3')).to_have_text('In hundreds')
                    assert card.locator('path[stroke="#276345"]').count() > 0
                    card.get_by_text('View associated table', exact=True).click()
                    expect(card.locator('tbody tr').last).to_have_text('Feb25')
                    assert card.locator('table').get_attribute('data-table-id') == expected_id
                    chart = card.locator('.viz-plot')
                    chart.evaluate("""el => echarts.getInstanceByDom(el).dispatchAction({type:'showTip',seriesIndex:0,dataIndex:1})""")
                    expect(chart).to_contain_text('Revised count')
                    assert len(briefings) == 1
                    expect(page.locator('#analysis-send')).to_be_enabled(timeout=10000)
                    page.locator('#analysis-question').fill('Keep the chart, label the February revision.')
                    page.locator('#analysis-send').click()
                    expect(page.locator('.viz-card').nth(1).locator('svg')).to_be_visible(timeout=120000)
                    assert [b['visualization_agent'] for b in briefings] == ['no viz agent active', active_id]
                    assert len(glitch_reports) == 1 and creations == [1]
                    assert calls == ['python','python','python','python','publish','python','python','publish']
                    open_chat()
                    expect(page.locator('.viz-card svg')).to_have_count(2)
                    expect(reply_timer.locator('time')).to_have_text(completed_time)
                    assert not errors, errors
                    assert len(list((directory / 'visualizations').glob('*.json'))) == 2
                    assert json.loads((directory / 'session.json').read_text(encoding='utf-8'))['visualization_agent'] == active_id
                    browser.close()
            finally:
                server.shutdown()
        try:
            validated_views([{'title':'Test', 'kind':'Pie','option':{'series':[{'type':'pie'}]}}], 2)
        except ValueError:
            pass
        else:
            raise AssertionError('Pie chart passed validation')
        reference = {'title':'Operating range', 'kind':'Interval bar', 'note':'Interval from 70 to 95 units.',
                     'option': {'xAxis':{'type':'category','name':'Group'},'yAxis':{'type':'value','name':'Units','min':65},
                                'series':[{'type':'bar','data':[70], 'stack':'interval','itemStyle':{'color':'transparent'}},
                                          {'type':'bar','data':[25], 'stack':'interval'}]}}
        assert validated_views([reference], 2)[0]['option']['yAxis']['min'] == 65
        session = Session(path, state, analysis=True, analysis_directory=directory)
        session.analysis_error_count = 4
        try:
            error_feedback(session, 'visualizer Python', 'test', ValueError('fifth failure'), agent='visualizer')
        except AnalysisExecutionLimit:
            pass
        else:
            raise AssertionError('Visualizer did not use the common five-error limit')
    print('PASS: stable IDs, scoped handoff, visualizer reuse, real Docker Python, shared glitch recovery, revised Chrome previews and interactive controls.')


if __name__ == '__main__':
    run()
