"""Check project deletion with temporary projects only; never call a model."""

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
from unittest.mock import patch
from uuid import uuid4

from psycopg2 import OperationalError, sql

from v_5.main import create_app
from v_5.main_functions.assets import archive_project, matching_files
from v_5.main_functions.session import write_json
from v_5.main_functions.sql import delete_atlas_project
from v_5.tests.reuse_check import BOOK, DATA, ROOT, project


def fixture(root, *, published=False, status='draft'):
    """Create originals, notes, run outputs and a local chat owned by one random project."""
    path, state = project(root, int(uuid4().hex, 16), [
        ('source', 'data.csv', DATA, 'primary', ''), ('source', 'book.csv', BOOK, 'asset', 'codebook')],
        published=published)
    state['status'] = status
    write_json(path / 'project.json', state)
    archive = archive_project(path, state).parent
    write_json(path / 'runs' / 'check' / 'checkpoint.json', {'complete': True})
    write_json(path / 'analysis' / 'runs' / 'check' / 'output.json', {'rows': [1]})
    return path, state, archive


def client_for(root):
    """Return real routes plus their write token and active job maps for controlled tests."""
    app = create_app(root)
    app.testing = True
    client = app.test_client()
    headers = {'X-V5-Token': client.get('/api/status').json['token']}
    handler = app.view_functions['delete_project']
    jobs = dict(zip(handler.__code__.co_freevars, (cell.cell_contents for cell in handler.__closure__)))
    return client, headers, jobs


def remove(client, headers, state):
    """Request explicit project deletion through the same endpoint as the button."""
    return client.post(f'/api/projects/{state["id"]}/delete', json={'confirmation': 'delete'}, headers=headers)


def offline(root):
    """Check ingestion/analysis deletion, active workers, failures and retry with SQL mocked."""
    other_path, other, other_archive = fixture(root, published=True)
    client, headers, jobs = client_for(root)
    with patch('v_5.main.delete_atlas_project') as delete_sql:
        for status, published in [('draft', False), ('failed', False), ('stopped', False),
                                  ('running', False), ('complete', True)]:
            path, state, archive = fixture(root, status=status, published=published)
            response = remove(client, headers, state)
            assert response.status_code == 200, response.json
            delete_sql.assert_called_with(state['id'])
            assert not path.exists() and not archive.exists()
            assert not path.with_name('.deleting-' + state['id']).exists()
            assert other_path.exists() and other_archive.exists()
        print('PASS draft, failed, stopped, interrupted and published project cleanup', flush=True)

        path, state, archive = fixture(root)
        state['folders']['source']['setup_reviewed'] = True
        write_json(path / 'project.json', state)
        base = f'/api/projects/{state["id"]}'
        delete_sql.reset_mock()
        assert client.post(base + '/delete', json={}, headers=headers).status_code == 400
        assert client.post(base + '/delete', json={'confirmation': 'delete'}).status_code == 403
        delete_sql.assert_not_called()
        for invalid_id in ('public', '../projects', 'g' * 32):
            try:
                delete_atlas_project(invalid_id)
            except ValueError:
                pass
            else:
                raise AssertionError('Unsafe schema accepted')

        # Use the actual ingestion start/stop lifecycle, with only model work replaced.
        started, finish = threading.Event(), threading.Event()

        def ingest(session):
            """Keep a real ingestion session active until cancellation and simulated cleanup finish."""
            started.set()
            assert session.cancelled.wait(10)
            assert finish.wait(10)
            return 'stopped'

        with patch('v_5.main.run_project', ingest):
            assert client.post(base + '/start', json={}, headers=headers).status_code == 200
            assert started.wait(5)
            try:
                assert remove(client, headers, state).status_code == 400
                assert client.post(base + '/stop', json={}, headers=headers).status_code == 200
                assert remove(client, headers, state).status_code == 400  # Stop is not yet complete.
                delete_sql.assert_not_called()
            finally:
                finish.set()
            deadline = time.monotonic() + 5
            while state['id'] in jobs['sessions'] and time.monotonic() < deadline:
                time.sleep(.02)
            assert state['id'] not in jobs['sessions']

        jobs['analysis_jobs'][(state['id'], uuid4().hex)] = object()
        assert remove(client, headers, state).status_code == 400
        jobs['analysis_jobs'].clear()
        release = threading.Event()
        worker = threading.Thread(target=release.wait, daemon=True)
        worker.start()
        jobs['extraction_jobs'][state['id']] = [worker]
        try:
            assert remove(client, headers, state).status_code == 400
            delete_sql.assert_not_called()
        finally:
            release.set()
            worker.join(5)
        assert remove(client, headers, state).status_code == 200
        assert not path.exists() and not archive.exists()
        print('PASS confirmation, ID safety, running ingestion/stop, analysis and extraction guards', flush=True)

        # A locked original or output must leave a selectable, cleanup-only project.
        for locked_area in ('originals', 'outputs'):
            path, state, archive = fixture(root, published=True)
            base = f'/api/projects/{state["id"]}'
            staged = path.with_name('.deleting-' + state['id'])
            locked = archive if locked_area == 'originals' else staged / 'runs'
            rmtree = shutil.rmtree

            def blocked(target, *args, **kwargs):
                """Fail only the selected test directory, as an open Windows file would."""
                if Path(target) == locked:
                    raise PermissionError('Intentional locked test file')
                return rmtree(target, *args, **kwargs)

            with patch('shutil.rmtree', blocked):
                assert remove(client, headers, state).status_code == 409
            assert not path.exists() and (staged / 'project.json').exists()
            # A new app instance proves the retry does not depend on in-memory state.
            retry, retry_headers, _ = client_for(root)
            assert retry.get(base).json['status'] == 'deleting'
            assert any(p['id'] == state['id'] for p in retry.get('/api/projects').json)
            assert retry.post(base + '/start', json={}, headers=retry_headers).status_code == 409
            assert all(match['project_id'] != state['id'] for matches in
                       matching_files(other_path, other).values() for match in matches)
            assert remove(retry, retry_headers, state).status_code == 200
            assert not staged.exists() and not archive.exists()
            assert remove(retry, retry_headers, state).status_code == 200
        print('PASS locked originals/outputs, reload, retry and removal from duplicate matching', flush=True)


def sql_checks(root):
    """Verify real schema and registry cascades, transactional rollback and project isolation."""
    from v_5.analyse.managers.chats import chat_transaction
    from v_5.analyse.sql_assets.joins import save_join_rules
    from v_5.main_functions.sql import PROJECT_SUMMARY_SQL, atlas_transaction, rename_atlas_project
    from v_5.tests.reuse_sql_check import seed

    owners = []
    try:
        for _ in range(2):
            path, state, archive = fixture(root)
            owners.append((path, state, archive))
            rename_atlas_project(state['id'], state['context']['name'])
            seed(path, state)
            save_join_rules(state['id'], ['source'], ['Join on id'])
            with chat_transaction() as cursor:
                cursor.execute(PROJECT_SUMMARY_SQL)
                cursor.execute('INSERT INTO public.atlas_project_summaries VALUES (%s,%s)', (state['id'], 'Toy summary'))
                cursor.execute('INSERT INTO public.atlas_chats (chat_id,project_id) VALUES (%s,%s)', (state['id'], state['id']))
                cursor.execute("INSERT INTO public.atlas_chat_turns (chat_id,turn_id,turn_number,question,answer) VALUES (%s,'turn',1,'{}','{}')", (state['id'],))
                cursor.execute("INSERT INTO public.atlas_chat_sessions (chat_id,turn_id,parent_id,navigation,rollover_notes) VALUES (%s,'turn','parent','{}','{}')", (state['id'],))

        def counts(state):
            """Count this project's schema objects and rows, including chat grandchildren."""
            with atlas_transaction() as cursor:
                cursor.execute('SELECT count(*) FROM pg_namespace WHERE nspname=%s', (state['id'],))
                result = [cursor.fetchone()[0]]
                cursor.execute('SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=%s', (state['id'],))
                result.append(cursor.fetchone()[0])
                for table in ('atlas_projects', 'atlas_project_summaries', 'atlas_join_rules',
                              'atlas_chats', 'atlas_chat_turns', 'atlas_chat_sessions'):
                    key = 'chat_id' if table in {'atlas_chat_turns', 'atlas_chat_sessions'} else 'project_id'
                    cursor.execute(sql.SQL('SELECT count(*) FROM public.{} WHERE {}=%s').format(
                        sql.Identifier(table), sql.Identifier(key)), (state['id'],))
                    result.append(cursor.fetchone()[0])
                return result

        path, state, archive = owners[0]
        before, other_before = counts(state), counts(owners[1][1])
        assert all(before) and all(other_before)
        client, headers, _ = client_for(root)

        @contextmanager
        def failing_transaction():
            """Raise after the real DROP SCHEMA, while still inside its database transaction."""
            with atlas_transaction() as cursor:
                class FailingCursor:
                    def execute(self, statement, *args):
                        if isinstance(statement, str) and statement.startswith('DELETE FROM public.atlas_projects'):
                            raise OperationalError('Intentional failure before registry deletion')
                        return cursor.execute(statement, *args)
                yield FailingCursor()

        with patch('v_5.main_functions.sql.atlas_transaction', failing_transaction):
            assert remove(client, headers, state).status_code == 503
        assert path.exists() and archive.exists() and counts(state) == before
        assert remove(client, headers, state).status_code == 200
        assert counts(state) == [0] * len(before)
        assert not path.exists() and not archive.exists()
        assert counts(owners[1][1]) == other_before
        print('PASS real SQL schema/codebooks, all registry/chat cascades, rollback and other-project isolation', flush=True)

        # A project still in ingestion has a registry row but no SQL schema yet.
        path, state, archive = fixture(root)
        owners.append((path, state, archive))
        rename_atlas_project(state['id'], state['context']['name'])
        assert counts(state)[0] == 0 and counts(state)[2] == 1
        assert remove(client, headers, state).status_code == 200
        assert counts(state) == [0] * len(before) and not path.exists() and not archive.exists()
        print('PASS real SQL deletion during ingestion before a schema exists', flush=True)
    finally:
        for _, state, _ in owners:
            delete_atlas_project(state['id'])


def browser_checks(root):
    """Click the real delete button in both workspaces, with SQL/model boundaries mocked."""
    from playwright.sync_api import expect, sync_playwright
    from werkzeug.serving import make_server
    from v_5.tests.upload_ui_check import QuietRequest

    errors = []
    with patch('v_5.main.delete_atlas_project'), patch('v_5.analyse.managers.routes.list_chats', return_value=[]), \
            patch('v_5.analyse.managers.routes.AtlasCatalog.artifact_tree', return_value={}):
        server = make_server('127.0.0.1', 0, create_app(root), threaded=True, request_handler=QuietRequest)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe', headless=True)
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.set_default_timeout(10000)
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    for published in (False, True):
                        path, state, archive = fixture(root, published=published, status='complete' if published else 'draft')
                        page.goto(f'http://127.0.0.1:{server.server_port}')
                        page.locator('#projects').select_option(state['id'])
                        expect(page.locator('#analyse-workspace' if published else '#ingest-workspace')).to_be_visible()
                        expect(page.locator('#delete-project')).to_be_visible()
                        # Project deletion shares the ingestion file/folder button colours.
                        style = page.locator('#delete-project').evaluate('element => { const s = getComputedStyle(element); return [s.backgroundColor, s.borderTopColor, s.color]; }')
                        assert style == ['rgb(250, 239, 237)', 'rgb(236, 209, 204)', 'rgb(183, 97, 85)'], style
                        page.locator('#delete-project').click()
                        expect(page.locator('#delete-description')).to_contain_text('SQL schema')
                        page.locator('#delete-dialog').get_by_role('button', name='Cancel', exact=True).click()
                        assert path.exists() and archive.exists()
                        page.locator('#delete-project').click()
                        page.locator('#delete-submit').click()
                        expect(page.locator('#delete-dialog')).not_to_be_visible()
                        expect(page.locator('#delete-project')).not_to_be_visible()
                        assert not path.exists() and not archive.exists()
                    assert not errors, errors
                    print('PASS browser deletion and cancellation in ingestion and analysis', flush=True)
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            worker.join(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sql', action='store_true')
    parser.add_argument('--browser', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='_delete_check_', dir=ROOT) as temporary:
        root = Path(temporary)
        offline(root)
        if args.sql:
            sql_checks(root)
        if args.browser:
            browser_checks(root)
