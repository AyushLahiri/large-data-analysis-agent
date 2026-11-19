"""Check phase boundaries, file-review context and handoff persistence offline."""
import importlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from v_5.ingest.initial_ingestion.agent import IngestionReply
from v_5.ingest.initial_ingestion.sub_agents.agent import ContextReply, FileReply
from v_5.ingest.workflow import run_project
from v_5.main_functions.session import Session


def check_packages():
    """Check numbered-package aliases, contract identity and skill paths.

    Inputs: No arguments; imports the live V5 packages.
    Returns: None; assertions identify missing paths or duplicate contract classes.
    Details: Offline layout regression check shared with the phase-boundary test.
    """
    groups = {
        'ingest': {'initial_ingestion': '1_initial_ingestion', 'data_loader': '2_data_loader',
                   'descriptives_check': '3_descriptives_check', 'codebook_ingestion': '4_codebook_ingestion'},
        'analyse': {'main': '1_main', 'managers': '2_managers', 'sql_assets': '3_manageSQLassets'},
    }
    for group, phases in groups.items():
        for alias, folder in phases.items():
            module = importlib.import_module(f'v_5.{group}.{alias}')
            assert Path(module.__file__).parent.name == folder
    from v_5.ingest.prototype.agents import IngestionReply as notebook_reply
    from v_5.ingest.data_loader.sub_agents.agent import GlitchReply as loader_glitch
    from v_5.analyse.main.sub_agents.agent import GlitchReply as analyst_glitch
    from v_5.main_functions.agent import GlitchReply
    assert notebook_reply is IngestionReply
    assert loader_glitch is analyst_glitch is GlitchReply
    from v_5.analyse.main.analyst import SKILL_PATH, SKILLS
    assert SKILL_PATH.is_dir()
    assert all((SKILL_PATH / (name + '.md')).is_file() for name in SKILLS)


def check_initial_ingestion(root):
    """Check file-review conversations and handoff persistence across phases.

    Inputs: root: temporary project directory.
    Returns: None; writes test uploads/state and asserts the saved handoff and loader
        reports.
    Details: Exercises phase-1 follow-ups, current context, protected folders and phase-2
        delegation with fake model replies.
    """
    uploads = root / 'uploads'
    uploads.mkdir()
    files = {}
    for file_id, role, agent, folder in [
        ('first', 'primary', 'data_files', 'Survey'),
        ('second', 'continuation', 'data_files', 'Survey'),
        ('notes', 'asset', 'supporting_notes', 'Survey'),
        ('published', 'primary', 'published_data', 'Locked'),
    ]:
        path = uploads / (file_id + '.csv')
        path.write_text('id,value\n1,2\n2,3\n', encoding='utf-8')
        files[file_id] = dict(file_id=file_id, filename=path.name, path=str(path), format='csv',
                              folder=folder, role=role, agent=agent, ignore=False, note='Uploaded context')
    state = dict(context={'name': 'Boundary check'}, folders={'Survey': {}, 'Locked': {}},
                 files=files, messages=[], atlas={'data': [{'folder': 'Locked'}]},
                 folder_notes={'Locked': {'summary': 'Existing publication'}},
                 findings={'published': {'summary': 'Previously reviewed'}})
    calls, threads, contexts = [], {}, {}
    actions = iter(['follow_up', 'clarify', 'inspect', 'finish'])
    checker_calls = 0
    ready = []

    class OfflineSession(Session):
        def ask_agent(self, name, prompt, history, contract, **options):
            """Drive the initial-ingestion check with scripted agent decisions.

            Inputs: name/prompt/history/contract/options: real Session agent-call interface.
            Returns: File, context-checker or ingestion reply dict; appends an assistant message.
            Details: Validates shared versus separate file conversations and updated context during
                phase-1 follow-ups without paid calls.
            """
            nonlocal checker_calls
            payload = json.loads(history[-1]['content']) if contract is FileReply else None
            if contract is FileReply:
                file_id = payload.get('current_file_id', payload.get('target_file_id'))
                assert file_id != 'published'
                calls.append(file_id)
                threads.setdefault(file_id, history)
                assert threads[file_id] is history
                contexts[file_id] = payload
                if 'target_file_id' in payload:
                    assert payload['folder_context']['ingestion_notes']['summary'] == 'Current folder decision'
                    assert payload['user_clarifications'] == ['Context confirmation']
                if file_id == 'second' and calls.count(file_id) == 2:
                    assert payload['context']['user_clarifications'] == ['Context confirmation', 'Later clarification']
                reply = {'summary': 'Read ' + file_id, 'classification': 'data'}
            elif contract is ContextReply:
                checker_calls += 1
                reply = {'accepted': checker_calls > 1, 'questions': ['Confirm context'] if checker_calls == 1 else []}
            else:
                assert contract is IngestionReply
                action = next(actions)
                reply = IngestionReply(action=action, message='Review', file_ids=['first'] if action == 'follow_up' else ['second'],
                    question='Check this file', user_questions=[], project_notes='Project decision',
                    folder_notes=[{'folder': 'Survey', 'summary': 'Current folder decision', 'primary_codebook': 'unresolved'}],
                    file_updates=[]).model_dump()
            history.append({'role': 'assistant', 'content': json.dumps(reply)})
            return reply

        def answer(self, question, ready=False):
            """Supply deterministic clarification answers for the phase test.

            Inputs: question: prompt; ready: whether preparation is waiting to start.
            Returns: None for ready, otherwise a context or later-clarification string.
            Details: Test-only answer method lets the handoff continue immediately.
            """
            if ready:
                return None
            return 'Context confirmation' if checker_calls == 1 else 'Later clarification'

    session = OfflineSession(root, state)
    def prepare(current, **context):
        """Capture the context handed from review to preparation.

        Inputs: current: expected session; context: named phase-1 handoff fields.
        Returns: complete; appends context for later comparisons.
        Details: Replaces prepare_data so the boundary can be checked without running the
            loader.
        """
        assert current is session
        ready.append(context)
        return 'complete'

    with patch('v_5.ingest.workflow.prepare_data', side_effect=prepare):
        assert run_project(session) == 'complete'
        assert threads['first'] is threads['second']
        assert threads['notes'] is not threads['first']
        assert sorted(calls) == ['first', 'first', 'notes', 'second', 'second']
        assert checker_calls == 2
        handoff = json.loads(Path(state['handoff_path']).read_text(encoding='utf-8'))
        assert handoff['folder_notes']['Locked']['summary'] == 'Existing publication'
        assert handoff['findings']['published']['summary'] == 'Previously reviewed'
        assert handoff['user_clarifications'] == ['Context confirmation', 'Later clarification']
        assert ready[0]['files'] == handoff['files']
        assert ready[0]['findings'] == handoff['findings']
        session.handoff = handoff
        assert run_project(session) == 'complete'
        assert len(calls) == 5
        assert ready[1]['files'] == handoff['files']
    session.progress('Survey', 2, 1234)
    session.phase_report('Survey', {'summary': 'Prepared'})
    saved = json.loads((root / 'project.json').read_text(encoding='utf-8'))
    assert saved['loader_budget'] == {'folder': 'Survey', 'session': 2, 'used': 1234, 'remaining': 248766}
    assert saved['preparations']['Survey']['summary'] == 'Prepared'


def run():
    """Run the offline layout and phase-boundary checks.

    Inputs: No arguments; allocates a temporary project directory.
    Returns: None; prints success after assertions pass.
    Details: Calls check_packages and check_initial_ingestion without model calls.
    """
    check_packages()
    with TemporaryDirectory() as directory:
        check_initial_ingestion(Path(directory))
    print('PASS: numbered packages, shared contracts, skill paths, file-review conversations, current follow-up context, protected folders, saved/reused handoffs and phase state. No paid calls.')


if __name__ == '__main__':
    run()
