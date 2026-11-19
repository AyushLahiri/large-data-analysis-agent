from v_5.analyse.general import progress
from v_5.analyse.main.memory import user_message
from v_5.main_functions.agent import GLITCH_PROMPT, GlitchReply


MAX_EXECUTION_ERRORS = 5


class AnalysisExecutionLimit(RuntimeError):
    pass


def error_feedback(session, language, code, error):
    """Explain an execution error and enforce the per-question retry limit.

    When: Analysis subagent glitch_agent, after SQL validation, SQL execution or Python
        fails.
    Inputs: session: error counter/logging; language: failing operation; code:
        executed/proposed source; error: exception.
    Returns: Feedback dict with execution_error and optional error_review; raises
        AnalysisExecutionLimit on the fifth error.
    Flow: Logs the full failure, sends bounded error text and numbered code to the
        diagnostic agent, and falls back to raw feedback if review itself fails.
    """
    session.check_stop()
    session.analysis_error_count += 1
    session.record('execution_error', 'analysis_agent', {'language': language, 'code': code,
                   'error': str(error), 'count': session.analysis_error_count})
    if session.analysis_error_count >= MAX_EXECUTION_ERRORS:
        raise AnalysisExecutionLimit('Analysis stopped after five execution errors. Please try again.')
    progress(session, 'Checking an execution issue', f'Reviewing the {language} error before trying again.')
    numbered = '\n'.join(f'{index}: {line}' for index, line in enumerate(code.splitlines(), 1))
    try:
        report = session.ask_agent('glitch_agent', GLITCH_PROMPT,
                                   [user_message({'requesting_agent': 'analysis_agent', 'language': language,
                                                  'executed_code': numbered, 'error_output': str(error)[:8000]})],
                                   GlitchReply, allow_user_questions=False)
        return {'execution_error': str(error)[:2000], 'error_review': report}
    except InterruptedError:
        raise
    except Exception as review_error:
        session.record('error_review_unavailable', 'glitch_agent', {'error': str(review_error)})
        return {'execution_error': str(error)[:2000]}
