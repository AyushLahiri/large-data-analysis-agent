import psycopg2
import re
from openai import OpenAIError
from v_5.analyse.general import ResultLimit, progress
from v_5.analyse.main.agent import FORMATTER, ORCHESTRATOR, FormatterReply, OrchestratorReply
from v_5.analyse.main.analyst import Analyst
from v_5.analyse.main.memory import AnalysisMemory, user_message
from v_5.analyse.sql_assets.catalog import AtlasCatalog, codebook_themes, read_atlas
from v_5.analyse.sql_assets.joins import save_join_rules
from v_5.analyse.managers.tables import table_index
from v_5.analyse.main.sub_agents.visualizer import visualize_table
from v_5.app.config import FORMATTER_MODEL
from v_5.ingest.summary import SUMMARY_FILE
from v_5.main_functions.session import loader_request


def project_markdown(path):
    """Read the project overview without full variable listings.

    Inputs: path: project directory containing the final summary.
    Returns: Trimmed Markdown overview, or empty text if no summary exists.
    Details: Used to orient the orchestrator. Removes detailed theme/column sections because
        scope and variable tools provide those separately.
    """
    target = path / SUMMARY_FILE
    if not target.is_file():
        return ''
    markdown = target.read_text(encoding='utf-8')
    # The orchestrator gets the overview here and top-level themes from Atlas.
    return re.sub(r'^### (?:Themes and subthemes[^\n]*|Columns)\n.*?(?=^#{1,3} |^---------- Folder \d+ ----------|\Z)',
                  '', markdown, flags=re.M | re.S).strip()


def published_scope(session, project, selected_folder):
    """Describe the published datasets and top-level themes in scope.

    Inputs: session: logging/cancellation; project: publication state; selected_folder:
        folder key or blank for all.
    Returns: Dict with dataset_count, theme_count and folder records; theme_count is None if
        the SQL lookup fails.
    Details: Used by the orchestrator before delegating. Reads themes from Atlas and records
        unavailable metadata without inventing a count.
    """
    datasets = {table['folder']: table for table in project['atlas']['data']
                if not selected_folder or table['folder'] == selected_folder}
    try:
        with read_atlas(session=session) as (_, cursor):
            themes = codebook_themes(cursor, project['id'], [table['dataset_id'] for table in datasets.values()])
    except psycopg2.Error as error:
        session.record('analysis_themes_unavailable', 'analysis_orchestrator', {'error': str(error)})
        themes = None
    folders = [{'folder': folder, 'name': table['dataset_name'],
                'themes': (themes or {}).get(table['dataset_id'], [])} for folder, table in datasets.items()]
    distinct = {label.strip().casefold() for folder in folders for label in folder['themes']}
    return {'dataset_count': len(folders), 'theme_count': len(distinct) if themes is not None else None, 'folders': folders}


def answer_text(message, outcome):
    """Remove duplicate table and method text from an answer.

    Inputs: message: answer text; outcome: analyst handoff with optional structured
        tables.
    Returns: Narrative text, or a short fallback when only structured content remains.
    Details: Used before publishing the final message because the UI renders outcome tables
        and method notes separately.
    """
    if not outcome.get('tables'):
        return message
    lines = []
    for line in message.splitlines():
        if re.match(r'^\s*(?:#+\s*)?(?:\*\*)?method(?:ology)?(?=\s*(?::|$|\*\*))', line, flags=re.I):
            break
        if not (line.strip().startswith('|') and line.count('|') >= 2):
            lines.append(line)
    return '\n'.join(lines).strip() or 'Here are the results.'


def format_answer(session, question, outcome, decimal_places):
    """Format an analyst's completed handoff for display in chat.

    Inputs: session supplies the model caller, question is the user's wording,
        outcome contains the answer materials and decimal_places is the display precision.
    Returns: The author and answer text to publish.
    Flow: Send the materials to a fresh Luna formatter at xhigh effort. On an
        empty or failed reply, record the fallback and use the analyst's message.
    """
    materials = {key: value for key, value in outcome.items() if not key.startswith('join_') and key != 'continued_weights'}
    request = loader_request(FORMATTER, [user_message({'question': question, 'analyst_response': materials,
                                                      'display_decimal_places': decimal_places})], FormatterReply)
    request.update(model=FORMATTER_MODEL, reasoning={'effort': 'xhigh'})
    try:
        text, _, status = session.call_loader('formatter', request, 8000, allow_user_questions=False)
        if status == 'completed':
            message = FormatterReply.model_validate_json(text).message.strip()
            if message:
                return 'formatter', message
        reason = f'No formatted answer returned ({status}).'
    except (OpenAIError, ValueError) as error:
        reason = str(error)
    session.check_stop()
    session.record('formatting_fallback', 'formatter', {'reason': reason})
    return 'analysis_agent', outcome['message']


def analyse(session, project, folder, question, *, table_id=None):
    """Coordinate one user-facing analysis answer.

    When: The chat route starts an opening message or a new question.
    Inputs: session: chat runtime; project: published project; folder: optional scope;
        question: user text, empty for opening. table_id selects a saved table
        when the user clicks Visualize.
    Returns: None; saves the answer, structured tables, method and display metadata.
    Flow: A table click goes directly to the analyst's visualization handoff.
        For typed questions the orchestrator can answer, select a table for the
        same handoff or delegate an analysis whose results go to the formatter.
    """
    memory = AnalysisMemory(session)
    if table_id:
        visualize_table(session, memory, folder, question, table_id)
        return
    progress(session, 'Understanding your question' if question else 'Getting oriented',
             'Identifying what the question needs.' if question else 'Reading the project summary and available topics.')
    markdown = project_markdown(session.path)
    available = table_index(session.state, folder)
    allowed_ids = {item['table_id'] for item in available}
    charts = [{'turn_id': msg.get('turn_id'), 'table_id': viz['table_id'], 'titles': [view['title'] for view in viz['views']]}
              for msg in session.state.get('messages', []) if (viz := (msg.get('details') or {}).get('visualization'))
              and viz['table_id'] in allowed_ids]
    history = [user_message({'turn_id': session.turn_id, 'project_markdown': markdown, 'project_name': project['context']['name'],
                           'selected_folder': folder or None, 'published_scope': published_scope(session, project, folder),
                           'saved_weights': session.state.get('weights', {}),
                           'available_tables': available, 'recent_visualizations': charts[-10:],
                           'question': question, 'opening_turn': not bool(question)})]
    outcome = {}
    author = 'analysis_orchestrator'
    decimal_places = 2
    for turn in range(8):
        reply = memory.ask('analysis_orchestrator', ORCHESTRATOR, history, OrchestratorReply)
        if turn == 0:
            precision = reply['decimal_places']
            decimal_places = None if precision is None else max(0, precision)
        if reply['action'] == 'recall_turn' and question:
            progress(session, 'Looking up a past turn', 'Checking the relevant earlier answer.')
            recalled = memory.recall('analysis_orchestrator', reply['recall'])
            session.record('analysis_recall', 'analysis_orchestrator', recalled)
            history.append(user_message({'turn_recall': recalled}))
            continue
        if reply['action'] == 'visualize' and question:
            if reply['request'] not in allowed_ids:
                history.append(user_message({'error': 'Choose an exact available table ID, or clarify which table is intended.'}))
                continue
            memory.remember_orchestrator(history)
            visualize_table(session, memory, folder, question, reply['request'])
            return
        if reply['action'] != 'analyse' or not question:
            memory.remember_orchestrator(history)
            message = reply['message'] or 'What would you like to explore in this project?'
            break
        memory.remember_orchestrator(history)
        try:
            progress(session, 'Checking the selected data', 'Reading the published folders and table structure.')
            analyst = Analyst(session, AtlasCatalog(project, folder, session=session), question, memory=memory)
            outcome = analyst.run(reply['request'] or question, reply['allow_raw_preview'], decimal_places=decimal_places)
        except ResultLimit as error:
            session.record('analysis_result_limit', 'analysis_agent', error.shape)
            message, outcome = str(error), {}
            break
        session.record('analysis_handoff', 'analysis_agent', outcome)
        progress(session, 'Preparing your answer', 'Putting the findings together.')
        author, message = format_answer(session, question, outcome, decimal_places)
        break
    else:
        memory.remember_orchestrator(history)
        message = 'Try narrowing the question to a particular figure or comparison.'
    message = answer_text(message, outcome)
    if outcome.get('continued_weights'):
        message += '\n\nContinuing with the same weight for ' + '; '.join(outcome['continued_weights']) + '.'
    with session.lock:
        session.check_stop()
        if outcome.get('join_success') == 'yes':
            save_join_rules(project['id'], outcome['join_folders'], outcome['join_rules_discovered'])
            session.record('analysis_join_rules_saved', 'analysis_agent', {
                'folders': outcome['join_folders'], 'rules': outcome['join_rules_discovered'],
            })
        if not outcome and reply['action'] == 'clarify':
            session.record('analysis_user_handoff', 'analysis_orchestrator', {'why_user': reply['why_user']})
        session.message(author, message, details={'ran_analysis': outcome.get('ran_analysis', False),
                        'selected_variables': outcome.get('selected_variables', []),
                        'method': outcome.get('method'), 'tables': outcome.get('tables', []), 'decimal_places': decimal_places})
