import json
from v_5.app.docker import run_code
from v_5.ingest.descriptives_check.agent import DESCRIPTIVES_PROMPT
from v_5.ingest.descriptives_check.checkpoint import DescriptivesCalls, DescriptivesRunner


def review_prepared_data(session, context, prepared, files, workspace, review_dir, environment):
    """Resume the prepared-data review and return confirm, handoff or stop.

    When: Phase 3, after a loader finish. Inputs: session services, folder/loader payloads,
        source files, prepared workspace, stable review directory and installed packages.
    Returns: The existing DescriptivesReply, including any concern and evidence paths.
    Details: One checkpoint holds the current turn and its flags; API threads hold the
        conversation. Raw execution is saved before glitch diagnosis. Docker reads prepared
        files without changing them and writes review evidence only under /review.
    """
    initial_input = json.loads(json.dumps({
        'folder_context': context, 'loader_report': prepared,
        'instruction': 'Inspect these saved outputs, then ask the user to confirm the review.'}, default=str))
    prompt = DESCRIPTIVES_PROMPT + '\nInstalled Docker environment:\n' + environment

    def execute(code, current):
        """Run a check without the synchronous glitch wrapper; the runner saves its result first."""
        session.check_stop()
        return run_code(code, files, workspace, current, read_only=True, cancelled=session.cancelled)

    def observe(event, state):
        """Restore progress and question forms from saved replies as well as new replies."""
        if event == 'reply':
            reply = state['reply']
            session.state.setdefault('descriptive_reviews', {})[context['folder']] = reply
            session.record('descriptives_review', 'descriptives_agent', {'folder': context['folder'], **reply})
            session.present_reply('descriptives_agent', json.dumps(reply),
                                  allow_user_questions=state['checked_outputs'])
        elif event == 'execution':
            result = state['result']
            session.message('execution', 'Code finished successfully.' if result.get('exit_code') == 0
                            else 'The code encountered an error.', 'thinking',
                            {'code': state['reply']['code'], 'result': result})
        elif event == 'glitch':
            session.present_reply('glitch_agent', json.dumps(state['glitch']['response']))

    return DescriptivesRunner(review_dir, workspace, initial_input, prompt,
        DescriptivesCalls(session), execute, session.answer, observer=observe,
        check_stop=session.check_stop).run()
