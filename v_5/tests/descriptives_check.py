"""Exercise review → correction → confirmation without paid model calls."""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from v_5.ingest.descriptives_check.agent import DescriptivesReply
from v_5.ingest.data_loader.agent import LoaderPhases, LoaderReply
from v_5.ingest.workflow import run_project


def review(action, **fields):
    """Build a descriptives reply with convenient test overrides.

    Inputs: action: review action; fields: values replacing the empty defaults.
    Returns: DescriptivesReply-shaped dict.
    Details: Used to script correction, question and confirmation paths in run.
    """
    return DescriptivesReply(action=action, code='', message='Review update', question='',
                            user_questions=[], issue='', evidence=[], requested_correction='').model_copy(update=fields).model_dump()


def run():
    """Check loader review, correction and folder-by-folder publication offline.

    Inputs: No arguments; builds temporary data and scripted conversations.
    Returns: None; prints success or raises an assertion.
    Details: Exercises phases 2-4, premature-question handling, manifest feedback and loader
        rollover without model/Docker/SQL calls.
    """
    with tempfile.TemporaryDirectory(prefix='_dc_', dir=Path(__file__).resolve().parents[2]) as temporary:
        root = Path(temporary)
        source = root / 'data.csv'
        source.write_text('id,value\n1,2\n2,3\n', encoding='utf-8')
        file = dict(file_id='file_1', filename='data.csv', path=str(source),
                    format='csv', folder='Survey', role='primary', ignore=False)
        files = {
            'file_1': file,
            'file_2': {**file, 'file_id': 'file_2', 'filename': 'building.sav', 'role': 'continuation'},
            'file_3': {**file, 'file_id': 'file_3', 'filename': 'building_embedded.csv', 'role': 'asset', 'embedded_for': 'file_2'},
            'file_4': {**file, 'file_id': 'file_4', 'filename': 'primary.csv', 'role': 'asset'},
            'file_5': {**file, 'file_id': 'file_5', 'filename': 'followup.csv', 'folder': 'Follow-up'},
        }
        phases = {name: dict(completed=True, details='Saved', observations=[], evidence_used=['/work/result.json'],
                            decisions_taken=['Preserved records'], key_findings=[]) for name in LoaderPhases.model_fields}
        finished = LoaderReply(action='finish', code='', question='', user_questions=[], phases=phases,
                               output_tables=['/work/survey.parquet'], manifest_path='/work/manifest.json',
                               loading_function_path='/work/load.py', preparation_notes_path='/work/notes.txt', summary='Prepared').model_dump()
        review_replies = iter([
            review('clarify', question='Premature review question'),
            review('run', code="print('shape and null counts')"),
            review('clarify', question='Does this look right?'),
            review('handoff', issue='User reports missing values were wrongly converted.',
                   evidence=['value: 2 unexpected nulls'], requested_correction='Check the conversion.'),
            review('run', code="print('recheck corrected values')"),
            review('clarify', question='Does the corrected result look right?'),
            review('confirm', message='Confirmed with the user.'),
            review('run', code="print('check next folder')"), review('confirm')])
        loader_replies = iter([{**finished, 'action': 'clarify', 'question': 'Premature loader question'},
                               finished, finished, {**finished, 'action': 'run', 'code': "print('correct conversion')"},
                               finished, finished])
        published = []

        class Session:
            def begin_phase(self, *args):
                """Ignore chat phase timing in this test."""

            def close_phase(self):
                """Ignore chat phase timing in this test."""

            def __init__(self):
                """Prepare the scripted loader session and its saved handoff.

                Inputs: Captures temporary root, fixture files and planned replies.
                Returns: None; initializes state, answer iterator and call/event collections.
                Details: Test-only Session replacement that begins after phase-1 review.
                """
                self.path = root
                self.cancelled = None
                self.run_path = root / 'run'
                self.state = {'context': {'name': 'Survey'}, 'folders': {'Survey': {}, 'Follow-up': {}},
                              'files': files, 'handoff_path': 'saved.json'}
                self.handoff = dict(project_notes='Survey notes', folder_notes={'Survey': {'summary': 'One dataset', 'primary_codebook': 'primary.csv'}},
                                    files=self.state['files'], findings={}, previews={}, user_clarifications=[])
                self.requests, self.executions, self.events = [], [], []
                self.rollover_requests = []
                self.answers = iter(['Some values should not be null.', 'Yes, that looks right.'])

            def call_loader(self, name, request, limit=None, *, allow_user_questions):
                """Return the next scripted loader or rollover response.

                Inputs: name/request/limit/allow_user_questions: real call interface.
                Returns: (text, usage, status): scripted reply, fixed token counts and completed.
                Details: Checks phase-2 summarizer settings and question permissions, while ensuring the
                    next folder waits for publication.
                """
                if name == 'loader_summarizer_agent':
                    assert request['model'] == 'gpt-6-luna' and request['reasoning'] == {'effort': 'high'}
                    assert limit is None and 'max_output_tokens' not in request and not allow_user_questions
                    assert 'Premature loader question' in request['history'][0]['content']
                    self.rollover_requests.append(request)
                    return 'Continuation note with the saved loading work.', {'input_tokens': 10, 'output_tokens': 10}, 'completed'
                if 'Follow-up' in name:
                    assert published == ['Survey']
                assert allow_user_questions == (len(self.requests) == 4)
                self.requests.append(request)
                return json.dumps(next(loader_replies)), {'input_tokens': 10, 'output_tokens': 10}, 'completed'

            def ask_agent(self, name, prompt, history, contract, *, allow_user_questions):
                """Return the next scripted descriptives-review action.

                Inputs: name/prompt/history/contract/allow_user_questions: real agent interface.
                Returns: Review dict; appends its assistant message to history.
                Details: Checks phase-3 folder evidence, codebook references and question gating before
                    advancing the scripted replies.
                """
                assert name == 'descriptives_agent'
                context = json.loads(history[0]['content'])
                assert context['loader_report']['phases'] == phases
                if context['folder_context']['folder'] == 'Survey':
                    assert context['folder_context']['files']['file_1']['filename'] == 'data.csv'
                    assert context['folder_context']['ingestion_notes']['summary'] == 'One dataset'
                    assert context['folder_context']['codebook_references'] == {
                        'file_1': {'embedded_file_id': None, 'primary_external_file_id': 'file_4'},
                        'file_2': {'embedded_file_id': 'file_3', 'primary_external_file_id': 'file_4'},
                    }
                else:
                    assert list(context['folder_context']['files']) == ['file_5']
                payload = next(review_replies)
                assert allow_user_questions == (payload['action'] not in {'run'} and payload['question'] != 'Premature review question')
                history.append({'role': 'assistant', 'content': json.dumps(payload)})
                return payload

            def run_code(self, code, files, workspace, turn_dir, **options):
                """Record a requested code run and simulate success.

                Inputs: code/files/workspace/turn_dir/options: sandbox-call arguments.
                Returns: Dict with exit_code=0 and empty JSON output.
                Details: Captures code and read-only options for assertions without launching Docker.
                """
                self.executions.append((code, options))
                return {'exit_code': 0, 'output': '{}'}

            def answer(self, question):
                """Return the next prepared user answer.

                Inputs: question: waiting prompt, unused by the stub.
                Returns: Next answer string from the test iterator.
                Details: Drives the descriptives correction and confirmation conversation without
                    waiting for a browser.
                """
                return next(self.answers)
            def record(self, *args):
                """Capture a workflow event for assertions.

                Inputs: args: event kind, source and payload.
                Returns: None; appends the argument tuple to events.
                Details: Test replacement for the permanent journal.
                """
                self.events.append(args)
            def count_input(self, request):
                """Force exactly one loader rollover during the check.

                Inputs: request: pending payload; uses recorded call counts.
                Returns: 245,000 tokens at the rollover point, otherwise 10.
                Details: Test-only budget simulation for phase-2 summarization.
                """
                return 245_000 if len(self.requests) == 1 and not self.rollover_requests else 10
            def save(self):
                """Accept the confirmed-workspace pointer in this offline session."""
                pass

            def progress(self, *args):
                """Ignore display/persistence output in the scripted phase check.

                Inputs: args: ignored Session-compatible arguments.
                Returns: None.
                Details: Test-only stub; run verifies the workflow decisions and captured evidence
                    instead.
                """
                pass
            def phase_report(self, *args):
                """Ignore display/persistence output in the scripted phase check.

                Inputs: args: ignored Session-compatible arguments.
                Returns: None.
                Details: Test-only stub; run verifies the workflow decisions and captured evidence
                    instead.
                """
                pass
            def show(self, *args):
                """Ignore display/persistence output in the scripted phase check.

                Inputs: args: ignored Session-compatible arguments.
                Returns: None.
                Details: Test-only stub; run verifies the workflow decisions and captured evidence
                    instead.
                """
                pass
            def print(self, *args):
                """Ignore display/persistence output in the scripted phase check.

                Inputs: args: ignored Session-compatible arguments.
                Returns: None.
                Details: Test-only stub; run verifies the workflow decisions and captured evidence
                    instead.
                """
                pass

        class ScriptedCalls:
            """Drive the existing scenario through the new remote-thread call boundary."""
            def __init__(self, session):
                self.session, self.threads, self.responses = session, {}, {}
                self.attempted, self.folder = False, ''

            def new_thread(self, role):
                key = str(len(self.threads) + 1)
                self.threads[key] = []
                return key

            def count_input(self, request):
                return self.session.count_input(request)

            def messages(self, thread):
                return self.threads[thread]

            def submit(self, role, request):
                if role == 'rollover':
                    output = self.session.call_loader('loader_summarizer_agent', request, allow_user_questions=False)
                else:
                    history = self.threads[request['conversation']]
                    incoming = [m for m in request['input'] if m['role'] != 'system']
                    payload = json.loads(incoming[0]['content'])
                    self.folder = payload.get('folder', self.folder)
                    history.extend(incoming)
                    output = self.session.call_loader('data_loader_agent — ' + self.folder,
                        {'history': list(history)}, 8000, allow_user_questions=self.attempted)
                    history.append({'role': 'assistant', 'content': output[0]})
                    self.attempted |= json.loads(output[0])['action'] == 'run'
                key = str(len(self.responses) + 1)
                self.responses[key] = dict(output_text=output[0], usage=output[1], status=output[2])
                return {'id': key}

            def retrieve(self, key):
                return self.responses[key]

        class ScriptedReviewCalls(ScriptedCalls):
            """Keep the existing review scenario behind the new thread/receipt boundary."""
            def submit(self, role, request):
                assert role == 'descriptives'
                history = self.threads[request['conversation']]
                incoming = [m for m in request['input'] if m['role'] != 'system']
                payload = json.loads(incoming[-1]['content'])
                if payload.get('execution_result', {}).get('exit_code') == 0:
                    self.attempted = True
                history.extend(incoming)
                reply = self.session.ask_agent('descriptives_agent', '', history,
                    DescriptivesReply, allow_user_questions=self.attempted)
                key = str(len(self.responses) + 1)
                self.responses[key] = {'output_text': json.dumps(reply), 'status': 'completed', 'usage': None}
                return {'id': key}

        Session.check_stop = lambda self: None
        Session.present_reply = lambda self, *a, **k: None
        Session.message = lambda self, *a, **k: None
        session = Session()
        def publish_folder(session, plan):
            """Simulate publication of one confirmed folder.

            Inputs: session: fake state; plan: single-dataset publication bundle.
            Returns: None; appends the folder and updates the fake Atlas publication.
            Details: Asserts publication order and next_folder handoff without accessing SQL.
            """
            assert len(plan['datasets']) == 1
            published.append(plan['datasets'][0]['folder'])
            assert plan['next_folder'] == ('Follow-up' if len(published) == 1 else None)
            session.state['atlas'] = {'status': 'published', 'data': [{'folder': folder} for folder in published]}

        def summarize_project(session):
            """Confirm all test folders are published before completion.

            Inputs: session: fake workflow session.
            Returns: complete after asserting the expected publication order.
            Details: Replaces write_final_summary in this offline phase-flow check.
            """
            assert published == ['Survey', 'Follow-up']
            return 'complete'

        with (patch('v_5.ingest.data_loader.functions.LoaderCalls', ScriptedCalls),
              patch('v_5.ingest.descriptives_check.functions.DescriptivesCalls', ScriptedReviewCalls),
              patch('v_5.ingest.descriptives_check.functions.run_code', lambda code, files, workspace, current, **kw: session.run_code(code, files, workspace, current, agent='descriptives_agent', read_only=kw['read_only'])),
              patch('v_5.ingest.data_loader.functions.execute_code', lambda code, files, workspace, current, **kw: session.run_code(code, files, workspace, current)),
              patch('v_5.ingest.data_loader.sub_agents.rollover.loader_request', lambda prompt, history, contract=None: {'history': list(history)}),
              patch('v_5.ingest.data_loader.functions.validate_prepared_manifest'),
              patch('v_5.ingest.data_loader.checkpoint.validate_prepared_manifest', side_effect=[
                  ValueError('Manifest tables.0.columns.0.name — Field required.'), None, None, None]),
              patch('v_5.ingest.codebook_ingestion.functions.ingest_codebooks') as codebooks, patch('v_5.ingest.codebook_ingestion.finalize.finalize_codebook') as final,
              patch('v_5.ingest.publishing.publish_atlas', side_effect=publish_folder),
              patch('v_5.ingest.data_loader.functions.write_final_summary', side_effect=summarize_project) as summary):
            assert run_project(session) == 'complete'
            assert codebooks.call_count == final.call_count == 2
            assert published == ['Survey', 'Follow-up']
            summary.assert_called_once_with(session)
        assert len(session.requests) == 6
        assert len(session.rollover_requests) == 1
        assert any('Continuation note with the saved loading work.' in m['content'] for m in session.requests[1]['history'])
        format_feedback = json.loads(session.requests[2]['history'][-2]['content'])
        assert 'tables.0.columns.0.name' in format_feedback['manifest_error']
        assert any(kind == 'manifest_validation_failed' for kind, *_ in session.events)
        correction_history = session.requests[3]['history']
        assert any('descriptives_handoff' in m['content'] for m in correction_history)
        assert any(m['role'] == 'assistant' and 'Prepared' in m['content'] for m in correction_history)
        reviews = [options for _, options in session.executions if options.get('agent') == 'descriptives_agent']
        assert len(reviews) == 3 and all(o['read_only'] for o in reviews)
        assert session.state['descriptive_reviews']['Survey']['action'] == 'confirm'
        assert len(list(root.glob('run/loader/folder_001/review_*/checkpoint.json'))) == 2
        assert not list(root.glob('run/loader/folder_001/review_*/conversation.json'))
        print('PASS: loader handoff, review and correction, and publication before preparing the next folder. No paid calls.')


if __name__ == '__main__':
    run()
