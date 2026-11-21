"""Small browser check using an isolated project and simulated agent messages."""
import json
import re
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright, expect
from v_5.analyse.main.sub_agents.errors import error_feedback
from v_5.analyse.general import progress
from v_5.analyse.managers.chats import chat_title
from v_5.main import create_app
from v_5.ingest.data_loader.agent import LoaderPhases

SCREENSHOTS = Path(__file__).resolve().parents[1] / 'app/runtime/ui_check'
SCREENSHOTS.mkdir(parents=True, exist_ok=True)
received = []
finish_analysis = threading.Event()


def conversation(session):
    """Simulate ingestion messages, forms and a failed publication for the browser test.

    Inputs: session: real temporary Session driven by the browser.
    Returns: failed; saves fake preparation state and a retry publication plan.
    Details: Replaces run_project so UI checks can cover questions, codebook review,
        progress and retry without model/Docker calls.
    """
    session.message('agent_house_csv_001 - households.csv (file_001) - finding',
                    'The file contains household records with a shared household identifier.',
                    'thinking', {'observations': ['The preview contains identifiers and energy values.']})
    session.message('ingestion_agent', 'The files look related. Two short answers will help organise them.')
    session.state['question_form'] = {'id': 'form-test', 'questions': [
        {'id': 'order', 'question': 'Do the building rows follow the household file order?',
         'help_text': 'The building file has no household ID.', 'options': ['Yes, the same order', 'No']},
        {'id': 'book', 'question': 'Which codebook should guide preparation?',
         'help_text': '', 'options': []}]}
    session.save()
    received.append(session.answer('Please answer these two questions.'))
    for file in session.state['files'].values():
        if file['filename']=='notes.md':
            file.update(folder='Other',ignore=True,placement_note='The relationship could not be established. Set aside for review.')
    session.state['folders']['Other']={'description':'Materials with an unresolved relationship','summary':'Set aside until their relationship can be established.'}
    session.save()
    session.message('data_loader_agent', 'The household keys match. Next, I will preserve postal codes and prepare the table.', 'thinking')
    session.message('execution', 'The code checked the keys and assembled 24 rows.', 'thinking',
                    {'code': 'assert households.household_id.is_unique', 'result': {'rows': 24, 'unmatched_keys': 0}})
    session.message('data_loader_agent', 'Your table is prepared: 24 households, eight columns. The original postal codes are preserved.')
    session.state['preparations'] = {'Household energy': {'phases': {name: {'completed': True,
        'details': 'Checks completed against the saved data.', 'observations': ['24 records retained.'],
        'evidence_used': ['Saved execution results'], 'decisions_taken': ['Preserve postal codes as text'],
        'key_findings': ['All records retained']} for name in LoaderPhases.model_fields}}}
    session.state['question_form'] = {'id': 'codebook-test', 'kind': 'codebook_review',
        'questions': [{'id': 'bill', 'question': 'bill'}],
        'rows': [{'variable_name': 'bill', 'variable_label': 'Electricity bill', 'categories': 'numeric',
                  'conflicts': [{'field': 'variable_description', 'reason': 'Currency units differ.',
                                 'evidence': [{'source': 'book.csv', 'excerpt': 'Dollars'},
                                              {'source': 'layout.dct', 'excerpt': 'Cents'}]}]}]}
    received.append(session.answer('Review the conflicting definitions.'))
    handoff = session.run_path / 'atlas_handoff.json'
    handoff.write_text(json.dumps({'release': 'example', 'datasets': []}))
    session.state.update(atlas_handoff_path=str(handoff), atlas={'status': 'failed'})
    return 'failed'


def publish(session, plan):
    """Simulate successful folder publication for the browser test.

    Inputs: session: temporary project; plan: expected example release.
    Returns: complete; updates fake Atlas state and emits folder-completion messages.
    Details: Replaces publish_atlas without writing to the database.
    """
    assert plan['release'] == 'example'
    session.state['atlas'] = {'status': 'published', 'database': 'atlas', 'schema': session.state['id'], 'release': 'example',
        'data': [{'dataset_id': 'dataset_001', 'dataset_name': 'Household energy', 'folder': 'households', 'name': 'households', 'row_count': 24}],
        'codebook': {'codebook_core': 8, 'codebook_unmatched': 2},
        'information': {'general_information': 1, 'ingestion_guide': 1}, 'embeddings': 8, 'dimensions': 2000}
    session.message('atlas', 'Ingestion is complete for Household energy files. Beginning with Field survey.',
                    'folder_complete', {'folder': 'households', 'release': 'example'})
    session.message('ingestion_agent', 'Reviewing the field survey materials.')
    return 'complete'


def final_summary(session):
    """Create a small downloadable summary for the browser test.

    Inputs: session: temporary project directory/state.
    Returns: complete; writes final_summary.md and sets summary metadata.
    Details: Replaces the summary model/SQL work while exercising the real download route.
    """
    (session.path / 'final_summary.md').write_bytes(b'# Household energy\n')
    session.state.update(final_summary_path='final_summary.md', final_summary_release='example')
    return 'complete'


def analysis_chat(session, project, folder, text):
    """Simulate analysis answers, errors and cancellation for browser checks.

    Inputs: session: temporary chat; project: project snapshot; folder: expected scope;
        text: scenario trigger.
    Returns: None; emits scripted messages/tables/methods or waits for cancellation.
    Details: Replaces analyse; covers formatting, chat switching, stop handling and the
        execution-error limit without paid calculations.
    """
    if not text:
        session.message('analysis_orchestrator', 'Let’s explore household energy by region or usage.')
        return
    assert folder == 'households'
    if text == 'Run another calculation.':
        progress(session, 'Running the query', 'Calculating the result in Atlas.')
        assert session.cancelled.wait(timeout=15)
        session.check_stop()
    if text == 'Check the error limit.':
        for language in ('SQL validation', 'python', 'sql', 'python', 'sql'):
            error_feedback(session, language, 'Invalid input', ValueError('Example execution error'))
        raise AssertionError('The fifth error did not stop the analysis.')
    if text == 'Show the average to four decimal places.':
        session.message('formatter', 'Here is the average.', details={
            'tables': [{'title': 'Average usage', 'columns': ['Average', 'Valid records', 'Weighted total'],
                        'rows': [[5.123456, 59, 12], [6.0, 60, 25]], 'unweighted_sample_columns': ['Valid records']}],
            'decimal_places': 4, 'ran_analysis': True,
        })
        return
    if text == 'What does the household identifier mean?':
        session.message('analysis_orchestrator',
                        'The codebook describes the identifier.\n\n'
                        '| Variable | What it measures |\n'
                        '| :--- | ---: |\n'
                        '| **`household_id`** | **Identifier** for each household |\n\n'
                        '### Which would be most useful?\n\n'
                        '- Compare *regions*.\n'
                        '  - Include the household count.\n\n'
                        '> Each row is one household.\n\n'
                        '```sql\nSELECT count(*) FROM households\n```\n\n'
                        '[Reference](https://example.com) [unsafe](javascript:alert%281%29) '
                        '<img src="x" onerror="alert(1)">',
                        details={'ran_analysis': False, 'method': {'steps': ['Looked up the identifier.'],
                                 'variables': [{'name': 'household_id', 'description': 'Identifies each household.'}]}})
        return
    progress(session, 'Running the query', 'Calculating the result in Atlas.')
    assert finish_analysis.wait(timeout=15)
    progress(session, 'Calculation complete', 'Received a 1 × 1 result.', code='SELECT count(*)', result={'rows': 1, 'columns': 1})
    session.state['analysis_context'] = {'analysis_agent': {'generation': 1, 'used_tokens': 804_000}}
    session.message('analysis_orchestrator', 'There are 24 household records.',
                    details={'ran_analysis': True,
                             'tables': [{'title': 'Households', 'columns': ['Unweighted count', 'Average usage'],
                                         'rows': [[24, 5.123456]]}],
                             'method': {'steps': ['Included eligible household records.', 'Counted households without weights.'],
                                        'variables': [{'name': 'household_id', 'description': 'Identifies each household.'}]}})


def organize_turn(session, name, prompt, history, contract, *, allow_user_questions):
    """Return predictable turn-manager notes for browser navigation checks.

    Inputs: session/name/prompt/history/contract/allow_user_questions: agent-call interface.
    Returns: Validated session-manager reply, or an empty glitch-agent reply.
    Details: Reads the real current-turn payload and returns controlled parent groups and
        summaries without model calls.
    """
    if name == 'glitch_agent':
        return {}
    assert name == 'session_management_agent'
    payload = json.loads(history[0]['content'])
    turn = payload['current_turn']
    assert turn['answer']
    earlier = payload['chronological_index']
    count_summary = ('There are **24 household records** in the published folder. The count includes all eligible '
                     'households and uses the household identifier for each record. This simple total uses no survey weights at all.')
    return contract.model_validate({
        'turn_id': turn['turn_id'],
        'parent_id': earlier[0]['turn_id'] if earlier and 'identifier mean' not in turn['question'] else turn['turn_id'],
        'previous_parent_updates': [],
        'navigation': {'header': 'Household average' if earlier else '**Household count**',
                       'summary': turn['answer'] if earlier else count_summary},
        'rollover': {'header': turn['question'], 'bullets': [turn['answer']], 'variables': []},
        'variables_used': payload['selected_variables'],
    }).model_dump()


def run():
    """Exercise the application's browser workflows using a temporary project.

    Inputs: No arguments; requires Playwright and local Chrome.
    Returns: None; saves screenshots and prints success or raises an assertion.
    Details: Starts an isolated Flask server with fake agents/SQL storage, checks ingestion
        and analysis interactions and responsive layouts, then closes the browser/server.
    """
    chats = {}

    def create_chat(project, chat):
        """Register a browser-test chat in the in-memory archive.

        Inputs: project/chat: ownership keys.
        Returns: None; inserts empty message/turn/weight state.
        Details: Replaces SQL create_chat during the browser check.
        """
        chats[project, chat] = {'messages': [], 'turns': [], 'weights': {}, 'status': 'idle'}

    def archive_chat(project, chat, state, *, register=False):
        """Checkpoint browser-test chat state in memory.

        Inputs: project/chat: keys; state: current chat; register: accepted for API
            compatibility.
        Returns: Number of saved turns; stores a deep copy of state.
        Details: Replaces SQL archive_chat so later restores cannot mutate the captured
            snapshot.
        """
        chats[project, chat] = deepcopy(state)
        return len(state['turns'])

    def save_feedback(project, chat, turn_id, feedback):
        """Save browser-test feedback onto the selected in-memory turn.

        Inputs: project/chat/turn_id: keys; feedback: rating payload.
        Returns: None; updates that turn's feedback.
        Details: Replaces SQL save_turn_feedback while the real feedback route is exercised.
        """
        turn = next(turn for turn in chats[project, chat]['turns'] if turn['id'] == turn_id)
        turn['feedback'] = feedback

    with (tempfile.TemporaryDirectory() as temporary, patch('v_5.main.run_project', conversation),
          patch('v_5.main.publish_atlas', publish), patch('v_5.main.rename_atlas_project') as registry,
          patch('v_5.main.write_final_summary', final_summary), patch('v_5.analyse.managers.routes.analyse', analysis_chat),
          patch('v_5.main.delete_atlas_project') as delete_project, patch('v_5.analyse.managers.routes.AtlasCatalog') as catalog,
          patch.multiple('v_5.analyse.managers.routes', create_chat=create_chat, archive_chat=archive_chat,
                         save_turn_feedback=save_feedback,
                         list_chats=lambda project: [{'id': chat, 'title': chat_title(state)}
                                                     for (owner, chat), state in chats.items() if owner == project],
                         restore_chat=lambda project, chat: deepcopy(chats.get((project, chat))),
                         delete_chat=lambda project, chat: chats.pop((project, chat), None)),
          patch('v_5.main_functions.session.Session.ask_agent', organize_turn)):
        catalog.return_value.artifact_tree.return_value = {'households': {
            'row_count': 24, 'column_count': 8, 'codebook': True,
            'unmatched': True, 'information': True, 'guide': True,
        }}
        server = make_server('127.0.0.1', 0, create_app(temporary), threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        origin = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                page = browser.new_page(viewport={'width':1440,'height':1050}, device_scale_factor=1)
                errors=[]; dialogs=[]
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('dialog', lambda dialog: (dialogs.append(dialog.type), dialog.dismiss()))
                page.goto(origin)
                expect(page.locator('#ingest')).to_be_disabled()
                page.get_by_role('button',name='New project',exact=False).click()
                page.locator('#project-name').fill('Household energy study')
                page.locator('#name-submit').click()
                expect(page.locator('#title')).to_have_text('Household energy study')
                page.locator('#dataset').click()
                page.locator('#files').set_input_files({'name':'households.csv','mimeType':'text/csv','buffer':b'household_id,postal_code\nH01,00101\nH02,00102\n'})
                expect(page.locator('#upload-help')).to_contain_text('primary or anchor')
                for key,value in {'description':'Household energy survey, all sampled households','time_geography':'October 2025, Kenya','source':'self','unit':'One household per row'}.items():
                    page.locator(f'#upload-0-{key}').fill(value)
                page.locator('#upload-submit').click()
                expect(page.locator('.file-name')).to_have_text('households.csv')
                expect(page.locator('.folder-heading')).to_contain_text('households')
                page.locator('#ingest').click()
                expect(page.locator('#missing-materials li')).to_have_count(2)
                expect(page.locator('#missing-materials')).to_contain_text('Add a codebook')
                expect(page.locator('#missing-materials')).to_contain_text('Add a report or presentation')
                page.locator('#add-ingestion-details').click()
                expect(page.locator('#before-note')).to_be_focused()
                page.locator('.tree-actions').get_by_role('button',name='Add files',exact=False).click()
                expect(page.locator('#files')).to_have_attribute('accept', re.compile(r'\.map(?:,|$)'))
                page.locator('#files').set_input_files([
                    {'name':'electricity.csv','mimeType':'text/csv','buffer':b'household_id,kwh\nH02,120\nH01,80\n'},
                    {'name':'codebook.csv','mimeType':'text/csv','buffer':b'variable,meaning\nhousehold_id,Unique household\nkwh,Electricity use\n'},
                    {'name':'notes.md','mimeType':'text/markdown','buffer':b'# Household survey\nOctober 2025.'},
                    {'name':'reader.MAP','mimeType':'text/plain','buffer':b'Linker script and memory map\n.text 0x00001000 0x10 reader.o\n'}])
                page.locator('#role-0').select_option('columns')
                expect(page.locator('#upload-0-description')).to_have_value('Household energy survey, all sampled households')
                expect(page.locator('#upload-0-time_geography')).to_have_value('October 2025, Kenya')
                expect(page.locator('#upload-0-source')).to_have_value('self')
                expect(page.locator('#upload-0-unit')).to_have_value('')
                expect(page.locator('.continuation-warning')).to_be_visible()
                for key,value in {'description':'Electricity columns for the same households','time_geography':'October 2025, Kenya','source':'self','unit':'One household per row'}.items():
                    page.locator(f'#upload-0-{key}').fill(value)
                page.locator('#role-1').select_option('codebook')
                page.locator('#role-2').select_option('related')
                expect(page.locator('#role-3 optgroup')).to_have_count(0)
                page.locator('#role-3').select_option('related')
                page.locator('#upload-note-1').fill('Primary codebook.')
                expect(page.locator('#upload-submit')).to_be_in_viewport()
                page.screenshot(path=str(SCREENSHOTS/'upload_dialog.png'), full_page=True)
                page.locator('#upload-submit').click()
                expect(page.locator('.file-row')).to_have_count(5)
                expect(page.locator('.file-name').filter(has_text='reader.MAP')).to_be_visible()
                expect(page.locator('.file-name').first).to_have_text('households.csv')
                expect(page.locator('[id^="note-folder:"]')).to_have_count(0)
                page.locator('#ingest').click()
                expect(page.locator('#ingest-error')).to_contain_text('fit together')
                page.locator('[id^="note-relationship:"]').fill('One household dataset with electricity columns and supporting documentation.')
                page.locator('#before-note').fill('Preserve postal code zeros.')
                page.locator('#ingest').click()
                expect(page.locator('#missing-materials li')).to_have_count(1)
                expect(page.locator('#missing-materials')).to_contain_text('Add a report or presentation')
                page.locator('#continue-ingestion').click()
                expect(page.locator('.question-card')).to_have_count(2, timeout=15000)
                expect(page.locator('#chat')).not_to_contain_text('agent_house')
                page.locator('#rename').click()
                page.locator('#project-name').fill('Household energy · October')
                page.locator('#name-submit').click()
                expect(page.locator('#title')).to_have_text('Household energy · October')
                page.get_by_role('button',name='Rename folder households',exact=True).click()
                page.locator('#project-name').fill('Household energy files')
                page.locator('#name-submit').click()
                expect(page.locator('.folder-heading')).to_contain_text('Household energy files')
                page.locator('#answer-order').select_option('Yes, the same order')
                page.locator('#answer-book').fill('Use the CSV codebook.')
                page.locator('#reply').fill('Preserve the postal codes.')
                # Polling must not reset an answer that is being written.
                page.wait_for_timeout(2200)
                expect(page.locator('#answer-book')).to_have_value('Use the CSV codebook.')
                page.screenshot(path=str(SCREENSHOTS/'desktop_questions.png'), full_page=True)
                page.locator('#send').click()
                expect(page.locator('.codebook-review mark')).to_have_text(['Dollars', 'Cents'], timeout=15000)
                assert page.locator('.codebook-review-scroll').evaluate('node => node.scrollWidth > node.clientWidth')
                page.get_by_role('textbox', name='Instruction for bill').fill('Use dollars.')
                page.get_by_role('button', name='Send review instructions').click()
                expect(page.locator('#status')).to_have_text('Needs attention', timeout=15000)
                page.get_by_role('button', name='Retry Atlas upload').click()
                expect(page.locator('#status')).to_have_text('Published to Atlas', timeout=15000)
                expect(page.locator('#analyse-workspace')).to_be_visible()
                expect(page.locator('#ingest-workspace')).to_be_hidden()
                expect(page.get_by_role('radio', name='Entire project')).to_be_checked()
                assert page.get_by_role('radio', name='Entire project').evaluate('input => input.getBoundingClientRect().width') <= 16
                page.locator('#analysis-new-chat').click()
                expect(page.locator('#analysis-chat')).to_contain_text('Let’s explore household energy')
                original = next(iter(chats))
                folder_choice = page.get_by_role('radio', name='Household energy files', exact=True)
                expect(folder_choice).to_be_visible()
                tree = page.locator('#analysis-tree')
                expect(tree.get_by_text('(24 × 8)', exact=True)).to_be_visible()
                expect(tree.get_by_text('raw_data', exact=True)).to_be_visible()
                expect(tree.get_by_text('Codebook', exact=True)).to_be_visible()
                expect(tree.get_by_text('households', exact=True)).to_have_count(0)
                color = folder_choice.evaluate('input => getComputedStyle(input.parentElement).backgroundColor')
                folder_choice.check()
                assert color != folder_choice.evaluate('input => getComputedStyle(input.parentElement).backgroundColor')
                tree.get_by_role('button', name='Collapse Household energy files').click()
                expect(tree.get_by_text('raw_data', exact=True)).to_be_hidden()
                expect(folder_choice).to_be_checked()
                tree.get_by_role('button', name='Expand Household energy files').click()
                expect(tree.get_by_text('raw_data', exact=True)).to_be_visible()
                page.locator('#analysis-question').fill('How many households are there?')
                page.locator('#analysis-send').click()
                expect(page.locator('#analysis-stage')).to_have_text('Running the query')
                thinking = page.locator('.analysis-thinking')
                thinking.locator('summary').first.click()
                expect(thinking.get_by_text('Calculating the result in Atlas.')).to_be_visible()
                finish_analysis.set()
                expect(page.locator('.analysis-table')).to_contain_text('24')
                expect(page.locator('.analysis-table td').last).to_have_text('5.12')
                expect(page.locator('.analysis-small-sample')).to_have_text('24')
                expect(thinking.get_by_text('Calculation complete')).to_be_visible()
                expect(page.locator('#analysis-activity')).to_be_hidden()
                expect(page.locator('.analysis-method')).not_to_have_attribute('open', '')
                page.locator('.analysis-method > summary').click()
                expect(page.locator('.method-steps li')).to_have_text(
                    ['Included eligible household records.', 'Counted households without weights.'])
                expect(page.locator('.method-variables dt')).to_be_hidden()
                page.get_by_text('Variables used', exact=True).click()
                expect(page.locator('.method-variables dt')).to_have_text('household_id')
                expect(page.locator('.method-variables dd')).to_have_text('Identifies each household.')
                page.locator('#analysis-question').fill('Show the average to four decimal places.')
                page.locator('#analysis-send').click()
                expect(page.get_by_role('cell', name='5.1235', exact=True)).to_be_visible()
                average = page.locator('article.message').filter(has_text='Here is the average.')
                expect(average.locator('.analysis-small-sample')).to_have_text('59')
                note = average.locator('.analysis-small-sample-note')
                expect(note).to_have_text('Sample size is less than 60. Interpret results cautiously. Statistical interpretation may be unreliable.')
                expect(note.locator('.analysis-small-sample-key')).to_be_visible()
                expect(page.locator('.analysis-turn-markers')).to_have_count(1)
                expect(page.locator('.analysis-turn-markers button')).to_have_count(2)
                page.set_viewport_size({'width': 1440, 'height': 670})
                first_turn = page.get_by_role('button', name='Household count')
                first_turn.hover()
                preview = page.locator('#analysis-turn-preview')
                expect(preview.locator('strong').last).to_have_text('24 household records')
                expect(preview).not_to_contain_text('**')
                assert len(preview.locator('.analysis-turn-summary').inner_text().split()) == 30
                first_turn.click()
                expect(page.locator('#analysis-turn-' + first_turn.get_attribute('data-turn-id'))).to_be_in_viewport()
                latest = page.get_by_role('button', name='Back to latest message')
                expect(latest).to_be_visible()
                expect(first_turn).to_have_attribute('aria-current', 'true')
                latest.click()
                expect(latest).to_be_hidden()
                expect(first_turn).to_have_attribute('aria-current', 'false')
                page.set_viewport_size({'width': 1440, 'height': 1050})
                transcript = page.get_by_role('link', name='Transcript')
                expect(page.locator('#analysis-activity')).to_be_hidden()
                raw = page.request.get(origin + transcript.get_attribute('href')).text()
                assert raw.count('------------\nTURN turn_') == 2
                assert raw.index('turn_finished') < raw.index('analysis_context_rollover')
                assert 'How many households are there?' in raw and 'Here is the average.' in raw and 'SELECT count(*)' in raw
                page.locator('#analysis-question').fill('What does the household identifier mean?')
                page.locator('#analysis-send').click()
                reference = page.locator('article.message').filter(has_text='The codebook describes the identifier.')
                expect(reference.get_by_role('columnheader', name='What it measures')).to_be_visible()
                expect(reference.locator('td strong code')).to_have_text('household_id')
                expect(reference.locator('td strong').last).to_have_text('Identifier')
                expect(reference.locator('td.markdown-align-right')).to_have_count(1)
                expect(reference.get_by_role('heading', name='Which would be most useful?', level=3)).to_be_visible()
                expect(reference.locator('li em')).to_have_text('regions')
                expect(reference.locator('ul ul li')).to_have_text('Include the household count.')
                expect(reference.locator('blockquote')).to_have_text('Each row is one household.')
                expect(reference.locator('pre code')).to_contain_text('SELECT count(*) FROM households')
                expect(reference.get_by_role('link', name='Reference')).to_have_attribute('href', 'https://example.com')
                expect(reference.locator('img, a[href^="javascript:"]')).to_have_count(0)
                expect(reference.locator('.analysis-method, .method-variables')).to_have_count(0)
                expect(reference.locator('.analysis-small-sample-note')).to_have_count(0)
                expect(page.locator('#analysis-activity')).to_be_hidden()
                positive = reference.get_by_role('button', name='Thumbs up')
                positive.click()
                expect(positive).to_have_attribute('aria-pressed', 'true')
                assert chats[original]['turns'][2]['feedback'] == {'rating': 'up'}
                expect(page.locator('#analysis-feedback-dialog')).to_be_hidden()
                negative = reference.get_by_role('button', name='Thumbs down')
                negative.click()
                feedback_dialog = page.locator('#analysis-feedback-dialog')
                feedback_dialog.get_by_role('textbox').fill('   ')
                feedback_dialog.get_by_role('button', name='Send feedback').click()
                expect(feedback_dialog.get_by_role('alert')).to_have_text('Please tell us what went wrong.')
                feedback_dialog.get_by_role('textbox').fill('The population should be clearer.')
                feedback_dialog.get_by_role('button', name='Send feedback').click()
                expect(feedback_dialog).to_be_hidden()
                expect(negative).to_have_attribute('aria-pressed', 'true')
                assert chats[original]['turns'][2]['feedback'] == {'rating': 'down', 'comment': 'The population should be clearer.'}
                question = page.locator('#analysis-question')
                question.press('ArrowUp')
                expect(page.get_by_role('button', name='Household count')).to_have_attribute('aria-current', 'true')
                question.press('Shift+Enter')
                expect(question).to_have_value('\n')
                question.fill('Run another calculation.')
                question.press('Enter')
                expect(page.locator('#analysis-stage')).to_have_text('Running the query')
                page.locator('#analysis-stop').click()
                expect(page.locator('#analysis-chat')).to_contain_text('Analysis stopped.')
                expect(page.locator('#analysis-stop')).to_be_hidden()
                expect(page.locator('#analysis-send')).to_be_enabled()
                assert len(chats[original]['turns']) == 3
                question.fill('Check the error limit.')
                question.press('Enter')
                expect(page.locator('#analysis-chat')).to_contain_text('Analysis stopped after five execution errors. Please try again.')
                expect(page.locator('#analysis-send')).to_be_enabled()
                assert len(chats[original]['turns']) == 3
                page.locator('#analysis-new-chat').click()
                expect(page.locator('#analysis-chat')).to_contain_text('Let’s explore household energy')
                expect(page.locator('#analysis-chat')).not_to_contain_text('There are 24 household records.')
                expect(page.locator('#analysis-chat-list li')).to_have_count(2)
                page.locator('#analysis-chat-list').get_by_role('button', name='How many households are there?', exact=True).click()
                expect(page.locator('#analysis-chat')).to_contain_text('There are 24 household records.')
                expect(page.locator('.analysis-method')).to_have_count(1)
                expect(reference.get_by_role('button', name='Thumbs down')).to_have_attribute('aria-pressed', 'true')
                page.get_by_role('button', name='Delete How many households are there?', exact=True).click()
                page.locator('#analysis-delete-dialog').get_by_role('button', name='Delete chat', exact=True).click()
                expect(page.locator('#analysis-chat-list li')).to_have_count(1)
                expect(page.locator('#analysis-chat')).to_contain_text('Let’s explore household energy')
                assert original not in chats
                assert not (Path(temporary) / 'projects' / original[0] / 'analysis_chats' / original[1]).exists()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('#view-ingest').click()
                expect(page.locator('#ingest-workspace')).to_be_visible()
                expect(page.locator('#atlas-result')).to_contain_text('codebook_core')
                expect(page.locator('#atlas-result')).to_contain_text('2,000 dimensions')
                download = page.get_by_role('link', name='Download project data summary')
                expect(download).to_be_visible()
                assert page.request.get(origin + download.get_attribute('href')).text() == '# Household energy\n'
                archived = page.locator('.chat-folder')
                expect(archived.locator('summary').first).to_have_text('Household energy files')
                expect(archived.get_by_text('Your table is prepared:', exact=False)).to_be_hidden()
                expect(page.get_by_text('Ingestion is complete for Household energy files. Beginning with Field survey.')).to_be_visible()
                expect(page.get_by_text('Reviewing the field survey materials.')).to_be_visible()
                archived.locator('summary').first.click()
                expect(archived.get_by_text('Your table is prepared:', exact=False)).to_be_visible()
                expect(page.locator('.folder-heading').last).to_contain_text('Other')
                expect(page.locator('[id^="note-folder:"]')).to_have_count(1)
                assert 'Use the CSV codebook.' in received[0] and 'Preserve the postal codes.' in received[0]
                assert received[1] == {'bill': 'Use dollars.'}
                expect(page.locator('#chat .message.user')).to_have_count(2)
                page.locator('#chat .thinking-group').last.locator('summary').first.click()
                expect(page.locator('#chat .thinking-content').last).to_contain_text('24 rows')
                page.locator('.run-details > summary').click()
                expect(page.locator('#phases .phase')).to_have_count(6)
                expect(page.locator('#phases .phase').first).to_contain_text('Reading instructions')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(SCREENSHOTS/'desktop_complete.png'), full_page=True)
                page.set_viewport_size({'width':390,'height':844})
                page.screenshot(path=str(SCREENSHOTS/'mobile.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile overflow'
                # Deletion is explicit; file notes leave the active project too.
                page.set_viewport_size({'width':1440,'height':1050})
                page.get_by_role('button',name='Delete notes.md',exact=True).click()
                expect(page.locator('#delete-dialog')).to_be_visible()
                page.locator('#delete-submit').click()
                expect(page.locator('.file-row')).to_have_count(4)
                expect(page.get_by_role('button',name='Delete folder Household energy files',exact=True)).to_be_hidden()
                expect(page.locator('#delete-project')).to_be_hidden()
                expect(page.get_by_role('button',name='Add a separate, related dataset')).to_be_enabled()
                published_id = registry.call_args_list[0].args[0]
                page.get_by_role('button',name='New project',exact=False).click()
                page.locator('#project-name').fill('Draft deletion check')
                page.locator('#name-submit').click()
                expect(page.locator('#title')).to_have_text('Draft deletion check')
                page.locator('#delete-project').click()
                expect(page.locator('#delete-description')).to_contain_text('cannot be undone')
                project_id = registry.call_args_list[-1].args[0]
                project_dir = Path(temporary) / 'projects' / project_id
                open_csv = project_dir / 'open.csv'
                open_csv.write_text('id\n1\n', encoding='utf-8')
                with open_csv.open('rb'):
                    page.locator('#delete-submit').click()
                    expect(page.locator('#delete-error')).to_contain_text('A project file is in use')
                    assert (project_dir / 'project.json').is_file()
                    delete_project.assert_not_called()
                page.locator('#delete-submit').click()
                expect(page.locator('#projects option')).to_have_count(2)
                assert not project_dir.exists()
                registry.assert_any_call(published_id, 'Household energy · October')
                delete_project.assert_called_once_with(project_id)
                # No project selected must not leave an actionable stale project view.
                page.locator('#projects').select_option('')
                expect(page.locator('#ingest')).to_be_disabled()
                expect(page.locator('.file-row')).to_have_count(0)
                assert not errors, errors
                assert not dialogs, dialogs
                browser.close()
                print('PASS: in-app dialogs, automatic folder, mixed batch upload, live rename, structured answers, polling preservation, thinking/results, phase reports, empty selection and desktop/mobile overflow. No paid calls or real project edits.')
        finally:
            finish_analysis.set()
            server.shutdown()


if __name__ == '__main__':
    run()
