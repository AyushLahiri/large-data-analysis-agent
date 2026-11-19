"""Check upload, immediate tagging and delayed shared questionnaires in a real browser."""

from copy import deepcopy
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server, WSGIRequestHandler
from v_5.main import create_app
from v_5.main_functions.session import write_json
from v_5.main_functions.assets import archive_project
from v_5.tests.reuse_check import project, DATA, BOOK, ANSWERS
from v_5.tests.ingestion_lifecycle_check import ready_project


class QuietRequest(WSGIRequestHandler):
    def log_request(self, *args):
        """Keep the check output to its assertions rather than HTTP polling."""


def lifecycle_controls(page, root):
    """Check folder deletion, the waiting stop ring and retry invalidation in the real UI."""
    path, state = ready_project(root, 777, folders=('first', 'second'))
    page.reload()
    page.locator('#projects').select_option(state['id'])
    page.get_by_role('button', name='Delete folder First', exact=True).click()
    page.locator('#delete-submit').click()
    expect(page.locator('#delete-dialog')).not_to_be_visible()
    expect(page.locator('#tree .folder')).to_have_count(1)
    expect(page.locator('#tree .folder-heading')).to_contain_text('Second')

    entered, release, fresh_started = threading.Event(), threading.Event(), threading.Event()
    runs = []

    def ingest(session):
        """Hold only the first agent reply so the browser can observe the stopping state."""
        runs.append(deepcopy(session.state))
        if len(runs) == 1:
            entered.set()
            assert release.wait(20)
            session.message('ingestion_agent', 'Reply finished before stopping.')
            session.check_stop()
        if len(runs) == 3:
            fresh_started.set()
        return 'blocked'

    with patch('v_5.main.run_project', ingest):
        try:
            page.locator('#ingest').click()
            page.locator('#continue-ingestion').click()
            assert entered.wait(5)
            stop = page.locator('#stop')
            expect(stop).to_be_visible()
            assert stop.evaluate("b=>getComputedStyle(b).borderRadius") == '50%'
            assert stop.evaluate("b=>getComputedStyle(b,'::before').width") == '11px'
            stop.click()
            expect(stop).to_be_disabled()
            expect(stop).to_have_class('stopping')
            expect(stop).to_have_attribute('aria-busy', 'true')
            assert stop.evaluate("b=>getComputedStyle(b,'::after').animationName") == 'stop-wait'
            # Wait for the existing 150 ms background transition before checking its alpha.
            page.wait_for_function("""()=>{const c=getComputedStyle(document.getElementById('stop')).backgroundColor;
                return c.startsWith('rgba(') && parseFloat(c.split(',').at(-1)) < .6;}""")
            expect(page.locator('#retry-ingestion')).not_to_be_visible()
            expect(page.locator('#dataset')).to_be_disabled()
        finally:
            release.set()
        expect(page.locator('#retry-ingestion')).to_be_enabled()
        expect(page.locator('#stop')).not_to_be_visible()
        expect(page.locator('#chat')).to_contain_text('Reply finished before stopping.')
        page.locator('#retry-ingestion').click()
        expect(page.locator('#status')).to_have_text('Preparation paused')
        assert len(runs) == 2 and runs[1]['messages']
        page.locator('#before-note').fill('Changed instructions after stopping')
        expect(page.locator('#retry-ingestion')).to_be_disabled()
        expect(page.locator('#retry-reason')).to_have_text('Ingestion materials have been updated. Please restart ingestion again.')
        page.locator('#save').click()
        expect(page.locator('#save')).to_be_enabled()
        page.reload()
        page.locator('#projects').select_option(state['id'])
        expect(page.locator('#retry-ingestion')).to_be_disabled()
        expect(page.locator('#retry-reason')).to_be_visible()
        page.locator('#ingest').click()
        page.locator('#continue-ingestion').click()
        assert fresh_started.wait(5), 'Fresh ingestion did not start.'
        expect(page.locator('#status')).to_have_text('Preparation paused')
        assert len(runs) == 3 and runs[2]['messages'] == []
        assert runs[2]['context']['before_ingestion'] == 'Changed instructions after stopping'
        expect(page.locator('#chat')).not_to_contain_text('Reply finished before stopping.')

    path, state = ready_project(root, 778)
    page.reload()
    page.locator('#projects').select_option(state['id'])
    page.get_by_role('button', name='Delete folder First', exact=True).click()
    page.locator('#delete-submit').click()
    expect(page.locator('#tree .folder')).to_have_count(0)
    expect(page.locator('#dataset')).to_be_enabled()
    expect(page.locator('#ingest')).to_be_disabled()
    print('PASS folder deletion; stop square/circle, translucent waiting ring; retry, edited-input block and fresh chat', flush=True)


def copy_review(page, root, received):
    """Check full-data and codebook-only copies before questions, without starting ingestion."""
    source_path, source = project(root, 99, [('source', 'data.csv', DATA, 'primary', ''),
                                            ('source', 'book.csv', BOOK, 'asset', 'codebook')], published=True)
    archive_project(source_path, source)
    started = len(received)
    for full_match in (True, False):
        page.locator('#new').click()
        page.locator('#project-name').fill('Copy choice check')
        page.locator('#name-submit').click()
        expect(page.locator('#name-dialog')).not_to_be_visible()
        expect(page.locator('#tree .folder')).to_have_count(0)
        page.locator('#dataset').click()
        raw = DATA if full_match else DATA.replace(b'red', b'RED')
        page.locator('#files').set_input_files({'name': 'data.csv', 'mimeType': 'text/csv', 'buffer': raw})
        page.locator('#upload-submit').click()
        expect(page.locator('#upload')).not_to_be_visible()
        expect(page.locator('#tree .folder')).to_have_count(1)
        page.get_by_role('button', name='＋ Add files', exact=True).click()
        page.locator('#files').set_input_files({'name': 'book.csv', 'mimeType': 'text/csv', 'buffer': BOOK})
        page.locator('#upload-submit').click()
        page.locator('#setup-role').select_option('codebook')
        page.locator('#setup-submit').click()
        expect(page.locator('#folder-setup')).not_to_be_visible()
        page.get_by_role('button', name='Complete ingestion', exact=True).click()
        expect(page.locator('#setup-title')).to_have_text('Copies of these files already exist')
        expect(page.locator('.dataset-questions')).to_have_count(0)
        expect(page.locator('[id^="setup-codebook-reuse-"]')).to_be_visible()
        if full_match:
            page.locator('#setup-dataset-reuse').select_option('0')
        else:
            expect(page.locator('#setup-dataset-reuse')).to_have_count(0)
        page.locator('#setup-submit').click()
        expect(page.locator('#setup-title')).to_have_text('Describe the data files')
        page.locator('#setup-back').click()
        expect(page.locator('#setup-title')).to_have_text('Copies of these files already exist')
        if full_match:
            expect(page.locator('#setup-dataset-reuse')).to_have_value('0')
        page.locator('#setup-submit').click()
        for index, (key, value) in enumerate(ANSWERS.items()):
            field = page.locator('.dataset-questions textarea').nth(index)
            if full_match:
                expect(field).to_have_value(value)
            else:
                field.fill(value)
        page.locator('#setup-submit').click()
        expect(page.locator('#setup-title')).to_have_text('book.csv')
        expect(page.locator('[id^="setup-codebook-reuse-"]')).to_have_count(0)
        page.locator('#setup-submit').click()
        expect(page.locator('#setup-title')).to_have_text('Review this dataset')
        expect(page.locator('#setup-body')).not_to_contain_text('No complete matching dataset')
        page.locator('#setup-submit').click()
        expect(page.locator('#folder-setup')).not_to_be_visible()
        project_id = page.locator('#projects').input_value()
        saved = page.request.get(f'{page.url.rstrip("/")}/api/projects/{project_id}').json()
        assert bool(saved['folders']['data'].get('reuse_dataset')) == full_match
        book = next(file for file in saved['files'].values() if file['asset_kind'] == 'codebook')
        assert book['reuse_codebook']['project_id'] == source['id']
        assert len(received) == started
    print('PASS data/codebook and codebook-only copy pages before questions; no automatic ingestion', flush=True)


def main(capture_directory=None, *, lifecycle_only=False):
    """Exercise the real UI and Flask routes with model/database calls replaced at the boundary."""
    received, errors = [], []
    completed = threading.Event()

    def ingest(session):
        """Capture the actual saved form handed to ingestion without spending tokens."""
        received.append(deepcopy(session.state))
        completed.set()
        return 'blocked'

    root = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix='_upload_ui_', dir=root) as temporary, \
            patch('v_5.main.rename_atlas_project'), patch('v_5.main.run_project', ingest):
        # This hidden old-format project reproduced the real upload 500.
        write_json(Path(temporary) / 'projects' / ('a' * 32) / 'project.json',
                   {'id': 'a' * 32, 'context': {'name': 'Old format'}, 'folders': [], 'files': {}})
        app = create_app(Path(temporary))
        server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=QuietRequest)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe',
                    headless=True, timeout=15000)
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.set_default_timeout(10000)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{server.server_port}')
                    if lifecycle_only:
                        lifecycle_controls(page, Path(temporary))
                        assert not errors, errors
                        return
                    page.locator('#new').click()
                    page.locator('#project-name').fill('Upload flow check')
                    page.locator('#name-submit').click()
                    page.locator('#dataset').click()
                    page.locator('#files').set_input_files({'name': 'households.csv', 'mimeType': 'text/csv',
                                                           'buffer': b'id,age\n1,30\n'})
                    expect(page.locator('#upload textarea')).to_have_count(0)
                    page.locator('#upload-submit').click()
                    expect(page.locator('#upload')).not_to_be_visible()
                    expect(page.locator('#folder-setup')).not_to_be_visible()
                    expect(page.locator('#dataset')).to_be_disabled()
                    print('PASS main upload without hidden-question validation', flush=True)

                    page.get_by_role('button', name='＋ Add files', exact=True).click()
                    page.locator('#files').set_input_files([
                        {'name': 'weights.csv', 'mimeType': 'text/csv', 'buffer': b'id,weight\n1,2\n'},
                        {'name': 'expenses.csv', 'mimeType': 'text/csv', 'buffer': b'id,amount\n1,8\n'},
                        {'name': 'book.csv', 'mimeType': 'text/csv', 'buffer': b'variable_name,label\nage,Age\n'},
                    ])
                    page.locator('#upload-submit').click()
                    for filename, role in [('weights.csv', 'columns'), ('expenses.csv', 'both'), ('book.csv', 'codebook')]:
                        expect(page.locator('#folder-setup')).to_be_visible()
                        expect(page.locator('#setup-title')).to_have_text(filename)
                        expect(page.locator('#setup-body textarea')).to_have_count(0)
                        heading = page.locator('#setup-role optgroup[label="Data"] option[disabled]')
                        expect(heading).to_have_text('　Data continuation')
                        assert heading.is_disabled()
                        page.locator('#setup-role').select_option(role)
                        if filename == 'weights.csv':
                            page.locator('#setup-submit').click()
                            expect(page.locator('#continuation-warning')).to_be_visible()
                            page.locator('#continuation-warning-back').click()
                            expect(page.locator('#setup-title')).to_have_text(filename)
                            expect(page.locator('#setup-role')).to_have_value(role)
                        page.locator('#setup-submit').click()
                        if role in {'rows', 'columns', 'both'}:
                            warning = page.locator('#continuation-warning')
                            close = page.locator('#continuation-warning-close')
                            expect(warning).to_be_visible()
                            expect(close).to_be_disabled()
                            expect(close.locator('.warning-loader')).to_have_count(1)
                            page.keyboard.press('Escape')
                            expect(warning).to_be_visible()
                            if capture_directory and filename == 'weights.csv':
                                page.screenshot(path=str(capture_directory / 'continuation-warning.png'))
                            expect(close).to_be_enabled(timeout=7000)
                            expect(close).to_have_text('×')
                            close.click()
                        else:
                            expect(page.locator('#continuation-warning')).not_to_be_visible()
                    expect(page.locator('#folder-setup')).not_to_be_visible()
                    page.get_by_role('button', name='＋ Add files', exact=True).click()
                    page.locator('#files').set_input_files({'name': 'notes.md', 'mimeType': 'text/markdown', 'buffer': b'A short report.'})
                    page.locator('#upload-submit').click()
                    expect(page.locator('#setup-title')).to_have_text('notes.md')
                    expect(page.locator('#setup-body textarea')).to_have_count(0)
                    page.locator('#setup-role').select_option('report')
                    page.locator('#setup-submit').click()
                    expect(page.locator('#folder-setup')).not_to_be_visible()
                    print('PASS nested data tags, five-second warning/back action and immediate single/batch tagging', flush=True)

                    add_files = page.get_by_role('button', name='＋ Add files', exact=True)
                    complete = page.get_by_role('button', name='Complete ingestion', exact=True)
                    expect(complete).to_have_count(1)
                    add_box, complete_box = add_files.bounding_box(), complete.bounding_box()
                    assert complete_box['y'] >= add_box['y'] + add_box['height']
                    assert complete.evaluate('element => getComputedStyle(element).backgroundColor') == 'rgb(53, 95, 76)'
                    complete.click()
                    expect(page.locator('.questionnaire-file')).to_have_count(3)
                    expect(page.locator('.dataset-questions textarea')).to_have_count(12)
                    expect(page.locator('#setup-submit')).to_have_text('Save and next: book.csv')
                    cards = page.locator('.questionnaire-file')
                    first, second, third = [cards.nth(index) for index in range(3)]
                    expect(first.locator('h3')).to_have_text('households.csv')
                    expect(first.locator('h3')).to_be_in_viewport()
                    assert first.evaluate('element => getComputedStyle(element).backgroundColor') != second.evaluate('element => getComputedStyle(element).backgroundColor')
                    question_boxes = first.locator('.dataset-question').evaluate_all('elements => elements.map(element => {const r=element.getBoundingClientRect();return {top:r.top,bottom:r.bottom,left:r.left,width:r.width};})')
                    assert all(b['top'] > a['bottom'] and b['left'] == a['left'] for a, b in zip(question_boxes, question_boxes[1:]))
                    values = ['Surveyed households', '2026, example town', 'My survey', 'One household']
                    for index, value in enumerate(values):
                        first.locator('textarea').nth(index).fill(value)
                        expect(second.locator('textarea').nth(index)).to_have_value(value)
                        expect(third.locator('textarea').nth(index)).to_have_value(value)
                    second.locator('textarea').nth(3).fill('A household weight record')
                    first.locator('textarea').nth(3).fill('One surveyed household')
                    expect(second.locator('textarea').nth(3)).to_have_value('A household weight record')
                    expect(third.locator('textarea').nth(3)).to_have_value('One surveyed household')
                    if capture_directory:
                        page.locator('#folder-setup .upload-body').evaluate('element => {element.scrollTop=0;}')
                        page.screenshot(path=str(capture_directory / 'data-questions.png'))
                    page.set_viewport_size({'width': 390, 'height': 844})
                    body_size = page.locator('#folder-setup .upload-body').evaluate('element => [element.scrollWidth, element.clientWidth]')
                    assert body_size[0] <= body_size[1], body_size
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    page.locator('#setup-submit').click()
                    for filename, note in [('book.csv', 'Variable definitions'), ('notes.md', 'Field report')]:
                        expect(page.locator('#setup-title')).to_have_text(filename)
                        expect(cards).to_have_count(1)
                        expect(cards.locator('h3')).to_have_text(filename)
                        expect(page.locator('.dataset-questions')).to_have_count(0)
                        cards.locator('textarea').fill(note)
                        if filename == 'book.csv':
                            page.locator('#setup-back').click()
                            expect(page.locator('#setup-title')).to_have_text('Describe the data files')
                            expect(page.locator('.questionnaire-file').nth(1).locator('textarea').nth(3)).to_have_value('A household weight record')
                            page.locator('#setup-submit').click()
                            expect(page.locator('#setup-title')).to_have_text(filename)
                            expect(cards.locator('textarea')).to_have_value(note)
                        page.locator('#setup-submit').click()
                    expect(page.locator('#setup-title')).to_have_text('Review this dataset')
                    page.locator('#setup-folder-description').fill('')  # Optional first-folder context stays valid when adding a second.
                    page.locator('#setup-folder-relationship').fill('Join the three data files by id.')
                    expect(page.locator('#setup-body')).not_to_contain_text('No complete matching dataset')
                    page.locator('#setup-submit').click()
                    expect(page.locator('#folder-setup')).not_to_be_visible()
                    assert not completed.is_set(), 'Saving a form must not start agents.'
                    expect(page.locator('#dataset')).to_be_enabled()
                    expect(page.locator('#dataset')).to_contain_text('Add new data and related materials')
                    expect(page.locator('#ingest')).to_be_enabled()
                    # A second folder is allowed before either folder is ingested.
                    page.locator('#dataset').click()
                    page.locator('#files').set_input_files({'name': 'locations.csv', 'mimeType': 'text/csv', 'buffer': b'id,place\n1,Town\n'})
                    page.locator('#upload-submit').click()
                    expect(page.locator('#upload')).not_to_be_visible()
                    expect(page.locator('#ingest')).to_be_disabled()
                    page.get_by_role('button', name='Complete ingestion', exact=True).click()
                    expect(page.locator('#setup-title')).to_have_text('Describe the data files')
                    single = page.locator('.dataset-questions textarea').first
                    empty_height = single.bounding_box()['height']
                    assert empty_height < 50, empty_height
                    single.fill('A long description of geographical locations. ' * 30)
                    assert single.bounding_box()['height'] > empty_height + 20
                    for index, value in enumerate(['Geographical locations', '2026', 'My survey', 'One location']):
                        page.locator('.dataset-questions textarea').nth(index).fill(value)
                    page.locator('#setup-submit').click()
                    page.locator('#setup-folder-description').fill('Locations for the households')
                    page.locator('#setup-submit').click()
                    expect(page.locator('#folder-setup')).not_to_be_visible()
                    assert not completed.is_set()
                    page.locator('#ingest').click()
                    assert completed.wait(5), 'Explicit Ingest materials never started ingestion.'
                    saved = {file['filename']: file for file in received[0]['files'].values()}
                    assert saved['weights.csv']['dataset_context']['unit'] == 'A household weight record'
                    assert saved['expenses.csv']['dataset_context']['unit'] == 'One surveyed household'
                    assert saved['households.csv']['dataset_context']['description'] == 'Surveyed households'
                    assert saved['book.csv']['asset_kind'] == 'codebook'
                    assert saved['expenses.csv']['continuation'] == ['rows', 'columns']
                    assert saved['notes.md']['note'] == 'Field report'
                    assert saved['locations.csv']['dataset_context']['description'] == 'Geographical locations'
                    assert received[0]['active_folder'] is None
                    assert not errors, errors
                    print('PASS Back/draft preservation, growing inputs, two saved folders and explicit ingestion only', flush=True)
                    copy_review(page, Path(temporary), received)
                    lifecycle_controls(page, Path(temporary))
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--lifecycle', action='store_true')
    main(lifecycle_only=parser.parse_args().lifecycle)
