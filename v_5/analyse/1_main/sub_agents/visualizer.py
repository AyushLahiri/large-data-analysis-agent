"""Run the chat's visualizer against one selected table and review its rendered drafts."""
import base64
import json
from pathlib import Path

from playwright.sync_api import sync_playwright

from v_5.analyse.general import progress
from v_5.analyse.main.agent import ANALYST
from v_5.analyse.main.memory import recent_variables_text, user_message
from v_5.analyse.main.sub_agents.agent import VISUALIZATION_BRIEF, VISUALIZER, ChartView, VisualizationBrief, VisualizerStep
from v_5.analyse.main.sub_agents.errors import error_feedback
from v_5.analyse.managers.tables import selected_table, source_context, table_file
from v_5.app.docker import run_code
from v_5.main_functions.session import write_json


UI = Path(__file__).resolve().parents[3] / 'UI'
MARKER = '\n__V5_CHART_VIEWS__\n'
PREAMBLE = """import json
import platform
from importlib.metadata import version
import pandas as pd
print("Python", platform.python_version(), "|", ", ".join(p + " " + version(p) for p in ("pandas","numpy","scipy","pyarrow")))
with open('/input/table.json', encoding='utf-8') as source:
    table = json.load(source)
df = pd.DataFrame(table['rows'], columns=table['columns'])
views = []
"""


def check_options(node, row_count):
    """Check one ECharts option tree before sending it to the browser.

    Inputs: node is the option or one of its children. row_count is the number
        of rows in the selected table.
    Returns: None on success. Invalid option keys, resource references, tooltip
        render modes or source-row indexes raise ValueError.
    Details: Dataset source records contain user column names and values, so
        option-key checks skip those records.
    """
    if isinstance(node, list):
        for child in node:
            check_options(child, row_count)
    elif isinstance(node, dict):
        for key, value in node.items():
            if key in {'__proto__', 'prototype', 'constructor', 'link', 'sublink', 'image', 'href',
                       'toolbox', 'baseOption', 'media', 'timeline', 'options'}:
                raise ValueError(f'{key} is outside the local static chart contract.')
            if key == 'source':  # Dataset row/column names are data, including names such as "color".
                continue
            if key == 'renderMode' and value != 'richText':
                raise ValueError('Use native text tooltips.')
            if key == 'sourceRows' and (not isinstance(value, list) or any(type(i) is not int or not 0 <= i < row_count for i in value)):
                raise ValueError('sourceRows must reference valid zero-based selected-table rows.')
            check_options(value, row_count)
    elif isinstance(node, str) and node.lstrip().lower().startswith(('image://', 'javascript:', 'data:', 'http:', 'https:', 'file:')):
        raise ValueError('Charts must be self-contained.')


def validated_views(items, row_count):
    """Validate the chart views emitted by a Python step.

    Inputs: items is the decoded views list and row_count bounds source-row references.
    Returns: Normalized view dictionaries, or an empty list for an exploratory run.
    Flow: Check the view contract, series, axis references and option tree. Reject
        duplicate options and drafts with more than three views.
    """
    if not isinstance(items, list) or len(items) > 3:
        raise ValueError('Use up to three views.')
    views = []
    for item in items:
        view = ChartView.model_validate(item)
        option = view.option
        check_options(option, row_count)
        series = option.get('series')
        if not isinstance(series, list) or not series or any(not isinstance(s, dict) for s in series):
            raise ValueError('Provide a nonempty ECharts series array.')
        if any(not s.get('type') or s['type'] in ('pie', 'custom') for s in series):
            raise ValueError('Use a built-in ECharts series. Pie charts and executable custom series are excluded.')
        for dimension in ('xAxis', 'yAxis'):
            if dimension not in option and not any(s.get('coordinateSystem', 'cartesian2d') == 'cartesian2d'
                    and s['type'] in ('line', 'bar', 'scatter', 'effectScatter', 'boxplot', 'candlestick', 'heatmap', 'pictorialBar')
                    for s in series):
                continue
            axes = option.get(dimension)
            axes = axes if isinstance(axes, list) else [axes]
            if not axes or any(not isinstance(a, dict) or not str(a.get('name') or '').strip() for a in axes):
                raise ValueError('Name every axis and include units where relevant.')
            for s in series:
                index = s.get(dimension + 'Index', 0)
                if type(index) is not int or not 0 <= index < len(axes):
                    raise ValueError('The series references an unavailable axis.')
        views.append(view.model_dump())
    if len({json.dumps(v['option'], sort_keys=True) for v in views}) != len(views):
        raise ValueError('Each alternative must have a distinct chart design.')
    return views


def run_draft(session, code, source, work, row_count):
    """Execute one visualization script against its selected table.

    Inputs: session supplies cancellation, code is the worker's Python, source is
        the table JSON, work holds this step's files and row_count bounds references.
    Returns: A pair of validated views and the last 4,000 characters of diagnostics.
    Flow: Mount the table read-only and expose it as table and df. Split marked
        JSON from ordinary stdout and enforce the 2 MB output limit before parsing.
    """
    # Report the worker's own line numbers to glitch_agent.
    script = PREAMBLE + f"\nexec(compile({code!r}, 'visualizer.py', 'exec'))\n"
    script += f'print({MARKER!r} + json.dumps(views, allow_nan=False))\n'
    result = run_code(script, {'table': {'path': source}}, work, work, read_only=True,
                      review_output=False, cancelled=session.cancelled)
    if result.get('exit_code') != 0:
        raise ValueError(result.get('error') or result['output'][-4000:])
    stdout = Path(result['full_log'])
    if stdout.stat().st_size > 2_000_000:
        raise ValueError('The draft exceeded 2 MB. Reduce redundant data or printed output.')
    diagnostic, marker, encoded = stdout.read_text(encoding='utf-8').rpartition(MARKER)
    if not marker:
        raise ValueError('The script ended before its views could be read.')
    return validated_views(json.loads(encoded), row_count), diagnostic.strip()[-4000:]


def render_drafts(views, width):
    """Render each view for the visualizer's prepublication review.

    Inputs: views contains validated chart options. width is the card width in CSS pixels.
    Returns: Alternating text diagnostics and high-detail PNG image inputs for the worker.
    Flow: Headless Chrome mounts the browser's chart card and selects each view.
        Report sampled marks, clipped text and possible label intersections alongside
        the screenshot. A view with no rendered observations raises ValueError.
    """
    with sync_playwright() as driver:
        browser = driver.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page(viewport={'width': width, 'height': 700}, device_scale_factor=1.5)
        page.route('**/*', lambda route: route.abort())
        page.set_content('<section class="viz-card"></section>')
        for stylesheet in ('style.css', 'charts.css'):
            page.add_style_tag(path=UI / stylesheet)
        page.add_style_tag(content='body, .viz-card { margin: 0; }')
        for script in ('dom.js', 'vendor/echarts-6.0.0.min.js', 'charts.js'):
            page.add_script_tag(path=UI / script)
        page.evaluate("views => { chatCharts.mount(document.querySelector('.viz-card'), views); }", views)
        card = page.locator('.viz-card')
        feedback = []
        for i, view in enumerate(views):
            if i:
                card.get_by_role('group', name='Chart views').get_by_role('button').nth(i).click()
            diagnostics = card.locator('.viz-plot').evaluate("""node => {
                const chart = echarts.getInstanceByDom(node);
                chart.getZr().flush();
                const series = chart.getModel().getSeries().map(s => {
                    const points = s.getData().count();
                    const indexes = [...new Set([0, points - 1])].filter(i => i >= 0 && i < points);
                    const samples = indexes.map(i => {
                        const p = s.getDataParams(i);
                        return {name: p.name, value: p.value, hover: p.data?.hover, annotation: p.data?.annotation};
                    });
                    return {name: s.name, points, samples};
                });
                const plot = node.getBoundingClientRect();
                const labels = [...node.querySelectorAll('svg text')].map(el => ({
                    text: el.textContent.trim(), box: el.getBoundingClientRect(),
                })).filter(l => l.text && l.box.width > 2 && l.box.height > 2).sort((a,b) => a.box.top - b.box.top);
                const clipped = labels.filter(({box}) => box.left < plot.left - 1 || box.right > plot.right + 1
                    || box.top < plot.top - 1 || box.bottom > plot.bottom + 1).slice(0, 6).map(l => l.text);
                const overlaps = [];
                for (let a = 0; a < labels.length && overlaps.length < 6; a++) {
                    const first = labels[a];
                    for (let b = a + 1; b < labels.length && overlaps.length < 6; b++) {
                        const second = labels[b];
                        if (second.box.top >= first.box.bottom - 2) break;
                        const shared = Math.min(first.box.right, second.box.right) - Math.max(first.box.left, second.box.left);
                        const depth = Math.min(first.box.bottom, second.box.bottom) - second.box.top;
                        if (shared > 2 && depth > .35 * Math.min(first.box.height, second.box.height)
                                && first.text !== second.text) overlaps.push([first.text, second.text]);
                    }
                }
                return {rendered_series: series, plot_px: [node.clientWidth, node.clientHeight],
                        clipped_text: clipped, possible_text_overlaps: overlaps};
            }""")
            if not any(s['points'] for s in diagnostics['rendered_series']):
                raise ValueError('The renderer found no observations in this draft.')
            png = card.screenshot()
            feedback.extend([{'type': 'input_text', 'text': json.dumps({'title': view['title'], 'chat_card_width_px': width, **diagnostics})},
                             {'type': 'input_image', 'detail': 'high', 'image_url': 'data:image/png;base64,' + base64.b64encode(png).decode()}])
        return feedback


def visualize_table(session, memory, folder, question, table_id):
    """Run the chat's visualizer for a table selection or a follow-up edit.

    When: A Visualize click or an orchestrator decision identifies a source table.
    Inputs: session owns chat state, memory provides the analyst conversation,
        folder limits the scope, question preserves the user's request and table_id
        selects the source.
    Returns: None. Publishing writes a local chart asset and posts its reference in chat.
        The worker can also finish with an explanation.
    Flow: Obtain an analyst brief, reuse or create the chat's visualizer conversation,
        and send the source and any prior design. Return Python diagnostics and
        draft images to that conversation until it publishes or finishes.
    """
    directory = session.state_path.parent
    table = selected_table(directory, session.state, table_id, folder)
    summary, parent = source_context(session.state, table)
    prior = next((viz for msg in reversed(session.state['messages'])
                  if (viz := (msg.get('details') or {}).get('visualization')) and viz['table_id'] == table_id), None)
    if prior:
        path = directory / 'visualizations' / (prior['turn_id'] + '.json')
        if path.is_file():
            previous = json.loads(path.read_text(encoding='utf-8'))
            prior = {**prior, 'python': previous['python']}
    recent_folders = {folder} if folder else {item['folder'] for item in session.state.get('variables_used', [])}
    progress(session, 'Visualizing the table', 'Reading the selected table and your request.')
    agent_id = session.state.get('visualization_agent')
    analyst_input = {'turn_id': session.turn_id, 'user_request': question, 'source_turn_summary': summary,
                     'visualization_agent': agent_id or 'no viz agent active',
                     'selected_table': table, 'recent_variables': recent_variables_text(session.state, recent_folders)}
    brief = memory.ask('analysis_agent', ANALYST + '\n\n' + VISUALIZATION_BRIEF,[user_message(analyst_input)], VisualizationBrief)
    if not agent_id:
        agent_id = session.analysis_request(lambda client: client.conversations.create()).id
        with session.lock:
            session.state['visualization_agent'] = agent_id
            session.save()
    width = getattr(session, 'chart_width', 580)
    inputs = [user_message({'chat_card_width_px': width, 'user_request': question, 'analyst_guidance': brief['guidance'],
                             'parent_conversation_for_context_only': parent, 'source_turn_summary': summary,
                             'previous_visualization_for_this_table': prior, 'selected_table': table})]
    candidate = None
    for step in range(28):
        response = session.ask_agent('visualizer', VISUALIZER, inputs, VisualizerStep,
                                     allow_user_questions=False, conversation=agent_id)
        if response['action'] == 'explain':
            session.message('visualizer', response['message'] or 'This table has no suitable graphical comparison.', details={'table_id': table_id})
            return
        if response['action'] == 'publish':
            if not candidate:
                inputs = [user_message({'error': 'Run a draft and inspect its successful render before publishing.'})]
                continue
            with session.lock:
                session.check_stop()
                asset = {'table_id': table_id, 'turn_id': session.turn_id, **candidate}
                write_json(directory / 'visualizations' / (session.turn_id + '.json'), asset)
                summary = {'table_id': table_id, 'turn_id': session.turn_id,
                           'views': [{'title': v['title'], 'kind': v['kind']} for v in asset['views']]}
                session.message('visualizer', response['message'] or 'Here is your visualization.',
                                details={'table_id': table_id, 'visualization': summary})
            return
        work = session.run_path / ('viz_draft_' + str(step + 1))
        progress(session, 'Checking the visualization', 'Running the visualizer’s Python and checking its output.')
        operation = 'visualizer Python and chart output'
        try:
            views, output = run_draft(session, response['code'], table_file(directory, table_id), work, len(table['rows']))
            feedback = [{'type': 'input_text', 'text': json.dumps({'python_output': output, 'views': len(views)})}]
            # Empty views mean an exploratory check. Keep the last reviewed draft.
            if views:
                operation = 'ECharts preview'
                feedback.extend(render_drafts(views, width))
                candidate = {'python': response['code'], 'views': views}
            inputs = [{'role': 'user', 'content': feedback}]
        except InterruptedError:
            raise
        except Exception as error:
            # After a failure the worker must render a successful draft again to publish.
            candidate = None
            inputs = [user_message(error_feedback(session, operation, response['code'], error, agent='visualizer'))]
    session.message('visualizer', 'The visualizer reached its revision limit before approving a chart.', details={'table_id': table_id})
