"""Toy replies and a fake remote API for the isolated loader experiment.

These server records and counters are test instrumentation, not checkpoint fields.
Generated fixture scripts run in fresh processes; only the Docker variant uses the
app's sandbox. No real model-generated script is executed on the host.
"""
import json
from pathlib import Path
import subprocess
import sys
from textwrap import dedent

from v_5.loader_prototype import LoaderPrototype, SESSION_TOKENS, docker_execute
from v_5.ingest.data_loader.agent import LoaderReply, LoaderPhases
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json, save_json
from v_5.prototype_runtime import Faults

DATA = 'id,colour,score\n1,red,10\n2,blue,20\n3,red,30\n'
BAD_CODE = "import pandas as pd\nfrom io import StringIO\ndf = pd.read_csv(StringIO(" + repr(DATA) + "))\nprint(df['missing'])\n"
GOOD_CODE = dedent('''\
    import json
    from pathlib import Path
    from io import StringIO
    import pandas as pd
    root = Path('/work')
    df = pd.read_csv(StringIO(DATA_LITERAL))
    assert df['id'].is_unique and len(df) == 3
    df.to_parquet(root / 'toy.parquet', index=False)
    columns = [dict(name=n, original_name=n, type=t, sql_definition=s) for n,t,s in
               [('id','int','BIGINT'), ('colour','character','TEXT'), ('score','int','BIGINT')]]
    manifest = {'tables': [dict(name='toy', path='/work/toy.parquet', row_count=3,
                               primary_key=['id'], columns=columns)]}
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    (root / 'load.py').write_text("def load(connection, rows):\\n    # Expects a psycopg-compatible connection; database execution is untested.\\n    with connection.cursor() as cursor:\\n        cursor.executemany('INSERT INTO toy (id, colour, score) VALUES (%s, %s, %s)', rows)\\n", encoding='utf-8')
    (root / 'preparation_notes.md').write_text('Three rows; unique id; no missing values. SQL execution untested.', encoding='utf-8')
    print(json.dumps({'rows':len(df), 'unique_id':bool(df['id'].is_unique),
                      'manifest_path':'/work/manifest.json'}))
''').replace('DATA_LITERAL', repr(DATA))


def reply(action, code='', complete=False):
    """Return the real LoaderReply schema with all six phase reports."""
    phase = dict(completed=complete, details='Toy preparation verified.' if complete else 'Investigation continues.',
                 observations=['Three source rows.'], evidence_used=['/work/checkpoint.json'] if complete else [],
                 decisions_taken=[], key_findings=[])
    value = dict(action=action, code=code, question='Keep the source colours?' if action == 'clarify' else '',
                 user_questions=[], phases={key: dict(phase) for key in LoaderPhases.model_fields},
                 output_tables=['toy'], manifest_path='/work/manifest.json',
                 loading_function_path='/work/load.py', preparation_notes_path='/work/preparation_notes.md',
                 summary='Toy loader continuation.')
    return LoaderReply.model_validate(value).model_dump()


def context():
    """Supply one tiny ingestion result with the production loader's context keys."""
    return dict(initial_file_context={'toy': {'shape': {'row_count': 3, 'column_count': 3}}},
                existing_tables=['survey', 'survey_part1', 'respondents'],
                sql_type_mapping={'character': 'TEXT', 'int': 'BIGINT', 'numeric': 'NUMERIC'},
                project={'name': 'Loader failure experiment'}, project_notes='Use the supplied three-row string.',
                folder='toy', folder_description={}, ingestion_notes={},
                codebook_references={'toy': {'embedded_file_id': None, 'primary_external_file_id': None}},
                user_clarifications=[], data_file_ids=['toy'],
                files={'toy': {'file_id': 'toy', 'format': 'csv', 'role': 'primary', 'folder': 'toy',
                               'path': '/input/toy.csv', 'finding': {}}}, toy_data=DATA)


class FakeAPI:
    """Persist simulated remote threads across actual worker-process crashes."""

    def __init__(self, root, scenario='normal', force_rollover=False):
        """Open the test server independently of the application's checkpoint."""
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'server.json'
        self.scenario, self.force_rollover = scenario, force_rollover
        if not self.path.exists():
            save_json(self.path, {'threads': {}, 'responses': {}, 'requests': [], 'events': [], 'once': []})

    def event(self, kind, **values):
        """Record observable external work so tests can detect unintended repetition."""
        server = read_json(self.path)
        server['events'].append({'kind': kind, **values})
        save_json(self.path, server)

    def new_thread(self, role):
        """Allocate a fake API conversation ID; the app only stores that ID."""
        server = read_json(self.path)
        name = f'thread_{len(server["threads"]) + 1}'
        server['threads'][name] = {'role': role, 'messages': []}
        save_json(self.path, server)
        return name

    def count_input(self, request):
        """Force one budget rollover after the failed execution, or return a small count."""
        server = read_json(self.path)
        loaders = [key for key, value in server['threads'].items() if value['role'] == 'loader']
        if self.scenario == 'oversized_handoff' and len(loaders) > 1:
            return SESSION_TOKENS
        history = server['threads'][request['conversation']]['messages']
        if self.force_rollover and request['conversation'] == loaders[0] and history:
            return SESSION_TOKENS
        return 400

    def messages(self, thread):
        """Fetch thread messages as the real rollover caller does, without saving a local chat."""
        return read_json(self.path)['threads'][thread]['messages']

    def loader_reply(self, payload, server):
        """Drive the toy through failure, repair, validation and optional user/review branches."""
        if 'pending_input' in payload:
            payload = payload['pending_input']
        if 'incomplete or invalid' in payload.get('instruction', ''):
            # This scenario truncates the first reply: its code has never run.
            return reply('run', BAD_CODE)
        if 'initial_file_context' in payload:
            if self.scenario == 'stop':
                return reply('stop')
            if self.scenario == 'early_clarify' and 'early_clarify' not in server['once']:
                server['once'].append('early_clarify')
                return reply('clarify')
            return reply('run', BAD_CODE)
        if 'execution_result' in payload:
            if payload['execution_result'].get('exit_code') != 0:
                code = GOOD_CODE
                if self.scenario == 'bad_manifest' and 'bad_manifest' not in server['once']:
                    server['once'].append('bad_manifest')
                    code = code.replace('row_count=3', 'row_count=4')
                return reply('run', code)
            if self.scenario == 'clarify' and 'clarify' not in server['once']:
                server['once'].append('clarify')
                return reply('clarify')
            if self.scenario == 'unfinished' and 'unfinished' not in server['once']:
                server['once'].append('unfinished')
                return reply('finish', complete=False)
            return reply('finish', complete=True)
        if 'manifest_error' in payload or 'descriptives_handoff' in payload:
            return reply('run', GOOD_CODE)
        if 'user_answer' in payload:
            return reply('finish', complete=True)
        if 'Investigate the available' in payload.get('instruction', ''):
            return reply('run', BAD_CODE)
        return reply('finish', complete=True)

    def submit(self, role, request):
        """Accept a request and store its reply; failures outside here can lose the receipt."""
        server = read_json(self.path)
        thread = request.get('conversation')
        incoming = [m for m in request['input'] if m.get('role') != 'system']
        if thread:
            server['threads'][thread]['messages'].extend(incoming)
        server['requests'].append({'role': role, 'request': request})
        status = 'completed'
        if role == 'loader':
            payload = None
            for message in reversed(server['threads'][thread]['messages']):
                if message['role'] == 'user':
                    try:
                        candidate = json.loads(message['content'])
                    except (ValueError, TypeError):
                        continue
                    if isinstance(candidate, dict) and 'incomplete or invalid' not in candidate.get('instruction', ''):
                        payload = candidate
                        break
            text = json.dumps(self.loader_reply(payload, server))
            if self.scenario in ('incomplete_loader', 'invalid_loader') and self.scenario not in server['once']:
                server['once'].append(self.scenario)
                if self.scenario == 'incomplete_loader':
                    status = 'incomplete'
                else:
                    text = '{broken'
        elif role == 'glitch':
            text = json.dumps(dict(error_kind='KeyError', actual_error="KeyError: 'missing'",
                                   failing_lines=["4: print(df['missing'])"],
                                   explanation='The requested column is absent; the loader must decide the correction.'))
            if self.scenario in ('incomplete_glitch', 'invalid_glitch') and self.scenario not in server['once']:
                server['once'].append(self.scenario)
                if self.scenario == 'incomplete_glitch':
                    status = 'incomplete'
                else:
                    text = '{}'
        else:
            text = 'The first script failed on the absent missing column. Preserve the saved error and glitch report; prepare and validate the three original rows next.'
        response_id = f'response_{len(server["responses"]) + 1}'
        server['responses'][response_id] = dict(id=response_id, status=status, output_text=text,
                                               usage={'input_tokens': 400, 'output_tokens': 200})
        if thread:
            server['threads'][thread]['messages'].append({'role': 'assistant', 'content': text})
        save_json(self.path, server)
        return {'id': response_id, 'status': status}

    def retrieve(self, response_id):
        """Return a completed fake API response independently of the worker process."""
        return read_json(self.path)['responses'][response_id]


def local_execute(code, workspace, current):
    """Run trusted toy code in a fresh Python process; this is not the Docker security test."""
    script = current / 'code.py'
    # Only these two checked-in fixtures can use the lightweight host adapter.
    if code not in (BAD_CODE, GOOD_CODE, GOOD_CODE.replace('row_count=3', 'row_count=4')):
        raise ValueError('The local adapter accepts only the fixed toy scripts.')
    script.write_text(code.replace("Path('/work')", 'Path(' + repr(str(workspace)) + ')'), encoding='utf-8')
    result = subprocess.run([sys.executable, '-B', str(script)], capture_output=True, text=True, timeout=30)
    output = result.stdout + result.stderr
    log = current / 'stdout.txt'
    log.write_text(output, encoding='utf-8')
    return dict(exit_code=result.returncode, output=output[:24000], truncated=len(output) > 24000, full_log=str(log))


def build_case(root, scenario='normal', plan=(), rollover=True, docker=False):
    """Connect the prototype to real execution and deterministic API/user/phase doubles."""
    root = Path(root)
    api = FakeAPI(root / 'test_server', scenario, rollover)
    faults = Faults(root / 'test_controls', plan)
    (root / 'test_controls').mkdir(exist_ok=True)

    def ingest():
        """Represent cheap context ingestion and its subagents as one restartable boundary."""
        api.event('ingestion')
        return context()

    def execute(code, workspace, current):
        """Count real script launches, then call the chosen fresh-process runner."""
        api.event('execute', failed_script=code == BAD_CODE)
        return (docker_execute if docker else local_execute)(code, workspace, current)

    def answer(question):
        """Return a toy user answer; test counters show whether an unsaved answer is repeated."""
        api.event('answer', question=question)
        return 'Yes, keep the original colours.'

    def review(latest):
        """Stop at the real review boundary, or request one correction in the same thread."""
        events = read_json(api.path)['events']
        count = sum(event['kind'] == 'review' for event in events)
        api.event('review')
        if scenario == 'review_handoff' and count == 0:
            return {'action': 'handoff', 'concern': 'Verify the saved colour values once more.'}
        return {'action': 'stop' if scenario == 'review_stop' else 'confirm'}

    return LoaderPrototype(root / 'work', api, execute, ingest, review, answer, faults.hit), api
