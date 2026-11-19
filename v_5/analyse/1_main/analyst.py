import json
import psycopg2
import re
from pathlib import Path
from v_5.analyse.general import (
    JoinCardinalityError, MAX_COLUMNS, MAX_ROWS, ResultLimit, progress, result_table,
)
from v_5.analyse.main.agent import ANALYST, AnalystReply, SCHEMA_GUIDE, SQL_RULES
from v_5.analyse.main.execution import checked_sql, execute_sql, pull_frame
from v_5.analyse.main.memory import AnalysisMemory, recent_variables_text, user_message
from v_5.analyse.main.sub_agents.agent import SQL_REVIEW, SqlReview
from v_5.analyse.main.sub_agents.errors import error_feedback
from v_5.analyse.managers.turns import variable_key
from v_5.analyse.sql_assets.catalog import VariableSearch
from v_5.analyse.sql_assets.joins import JOIN_SKILL, candidate_identifiers, read_join_rules
from v_5.app.docker import run_code


def format_method(method):
    """Normalize the method note shown with an analysis result.

    Inputs: method: dict with steps and name/description variable records.
    Returns: Cleaned steps/variables dict; raises if more than eight nonempty steps remain.
    Details: Used when Analyst.run finishes. Removes bullet prefixes, normalizes whitespace
        and deduplicates variable descriptions.
    """
    steps = []
    for step in method['steps']:
        step = re.sub(r'^\s*(?:[-*•]|\d+[.)]|\(\d+\))\s+', '', step)
        step = ' '.join(step.split())
        if step:
            steps.append(step)
    if len(steps) > 8:
        raise ValueError('Use at most eight short methodology bullets.')
    variables = {}
    for item in method['variables']:
        name, description = item['name'].strip(), ' '.join(item['description'].split())
        if name and description:
            variables[name, description] = None
    return {'steps': steps, 'variables': [{'name': name, 'description': description} for name, description in variables]}


class Analyst:
    def __init__(self, session, catalog, original_question, memory=None):
        """Prepare the analyst's data tools and conversation memory.

        Inputs: session: chat runtime; catalog: scoped Atlas metadata; original_question: user
            text; memory: optional shared AnalysisMemory.
        Returns: None; initializes the variable picker, memory and turn counter.
        Details: Created by analyse after the orchestrator delegates a question; supplied memory
            keeps both roles on the same past-turn view.
        """
        self.session, self.catalog = session, catalog
        self.question = original_question
        self.picker = VariableSearch(catalog, session)
        self.memory = memory or AnalysisMemory(session)
        self.turns = 0

    def selected_variables(self, items):
        """Keep only reported variables that exist in the selected data.

        Inputs: items: selected-variable records from the analyst reply; uses catalog metadata.
        Returns: Deduplicated list with valid folder/table/column locations and a type fallback.
        Details: Used in the final analyst handoff before turn management records variable
            history.
        """
        selected = {}
        for item in items:
            folder = self.catalog.folders.get(item['folder'])
            if not folder or item['table_name'] not in {table['name'] for table in folder['tables']}:
                continue
            kind = self.catalog.columns[item['table_name']].get(item['column_name'])
            if not kind or not item['name'].strip():
                continue
            value_type = ('character' if kind in {'text', 'character', 'character varying'} else
                          'numeric' if kind in {'integer', 'bigint', 'smallint', 'numeric', 'decimal', 'real', 'double precision'} else '')
            selected.setdefault(variable_key(item), {**item, 'categories_or_type': item['categories_or_type'].strip() or value_type})
        return list(selected.values())

    def python(self, reply, active_weights, work):
        """Run a calculation on temporary frames from Atlas.

        When: Analysis main agent chooses action=python after scope and weight checks.
        Inputs: reply: requested frames and code; active_weights: folder selections; work:
            temporary run directory.
        Returns: Validated columns/rows plus diagnostics; raises for execution, JSON or size
            failures.
        Flow: pull_frame writes selected columns, Docker runs read-only with cancellation, then
            temporary frames are removed. Parses full output only within the existing size
            bound.
        """
        frames = {}
        try:
            for index, frame in enumerate(reply['frames'], 1):
                folder = frame['folder']
                key = f'temp_{index}'
                frames[key] = pull_frame(self.catalog, folder, frame['columns'], active_weights[folder]['variable'], work / f'{key}.parquet')
            self.session.record('analysis_python_frames', 'analysis_agent', frames)
            files = {name: {'path': str(work / info['file'])} for name, info in frames.items()}
            self.session.check_stop()
            result = run_code(reply['code'], files, work, work, read_only=True, review_output=False,
                              cancelled=self.session.cancelled)
        finally:
            for file in work.glob('temp_*.parquet'):
                file.unlink(missing_ok=True)
        self.session.record('analysis_python_execution', 'analysis_agent', result)
        if result.get('exit_code') != 0:
            raise ValueError(result.get('error') or result.get('output') or 'Python failed.')
        output = result['output']
        if result.get('truncated'):
            log = Path(result['full_log'])
            if log.stat().st_size > 2_000_000:
                raise ValueError('Python output was too long. Return only the exact shape and essential diagnostics.')
            output = log.read_text(encoding='utf-8')
        # Python must print a table payload: columns and rows, with optional shape and diagnostics.
        payload = json.loads(output)
        if 'shape' in payload and (payload['shape']['rows'] > MAX_ROWS or payload['shape']['columns'] > MAX_COLUMNS):
            raise ResultLimit(payload['shape']['rows'], payload['shape']['columns'])
        if result.get('truncated') and len(payload['rows']) <= MAX_ROWS and len(payload['columns']) <= MAX_COLUMNS:
            raise ValueError('Python output text was too long. Keep the table cells and diagnostics brief.')
        checked = result_table(payload['columns'], payload['rows'])
        return {**checked, 'diagnostics': payload.get('diagnostics', {})}

    def run(self, task, allow_preview, *, decimal_places=2):
        """Choose variables, execute calculations and assemble an evidence-based answer.

        When: Analysis main agent, when the orchestrator hands off a task.
        Inputs: task: relayed request; allow_preview: raw-preview permission; decimal_places:
            display preference; uses session/catalog/memory.
        Returns: Outcome dict with message, tables, method, selected variables and optional
            weight/join notes; may return a failure message.
        Flow: Loops over search, recall, skill and execution actions. SQL passes checked_sql and
            sql_reviewer; execution failures use glitch_agent. Successful results are validated
            before weights and join discoveries can be retained.
        """
        session, catalog = self.session, self.catalog
        active_weights = {key: dict(val) for key, val in session.state.get('weights', {}).items() if key in catalog.folders}
        pending_weights, weight_folders, results = {}, set(), []
        loaded_skills = set()
        history = self.memory.seed({'original_user_question': self.question, 'relayed_user_request': task,
                                    'scope': catalog.context(), 'saved_weights': active_weights,
                                    'raw_preview_allowed': allow_preview, 'display_decimal_places': decimal_places})
        recent = recent_variables_text(session.state, catalog.folders)
        if recent:
            history.append({'role': 'user', 'content': recent})
        prompt = '\n\n'.join([ANALYST, SCHEMA_GUIDE, SQL_RULES, (SKILL_PATH / 'catalog.md').read_text(encoding='utf-8')])

        for _ in range(28):
            self.turns += 1
            reply = self.memory.ask('analysis_agent', prompt, history, AnalystReply)
            action = reply['action']
            feedback = {}
            try:
                for choice in reply['weights']:
                    folder, variable = choice['folder'], choice['variable'].strip().casefold()
                    if folder not in catalog.folders:
                        raise ValueError('Choose a weight in an allowed folder.')
                    match = catalog.exact(folder, variable) if variable else []
                    if variable and len(match) != 1:
                        raise ValueError(f'Choose an unambiguous weight variable for {folder}.')
                    weight = {'variable': match[0]['variable_name'] if match else '',
                              'label': (match[0].get('variable_label') or variable) if match else 'Unweighted'}
                    active_weights[folder] = weight
                    if choice['persist']:
                        # A persistent weight choice is only saved at the end if that folder was actually used.
                        pending_weights[folder] = weight

                if action == 'recall_turn':
                    progress(session, 'Looking up a past turn', 'Checking the relevant earlier answer.')
                    feedback = {'turn_recall': self.memory.recall('analysis_agent', reply['recall'])}
                elif action == 'exact':
                    progress(session, 'Checking variable names', 'Looking for the requested fields and available survey weights.')
                    folders = [reply['folder']] if reply['folder'] else list(catalog.folders)
                    if not set(folders) <= catalog.folders.keys():
                        raise ValueError('That folder is outside the selected scope.')
                    matches = {folder: {name: catalog.exact(folder, name.strip().casefold())
                                        for name in reply['exact_names']} for folder in folders}
                    feedback = {'matches': matches,
                                'survey_weights': {folder: self.picker.weight_candidates(folder) for folder in folders}}
                    batteries = []
                    for folder, variables in matches.items():
                        groups = sorted({tuple(sorted({row['variable_name'], *row['battery_members']}))
                                         for rows in variables.values() for row in rows if row.get('battery_members')})
                        batteries.extend(self.picker.show_all_battery(folder, [list(group) for group in groups]))
                    if batteries:
                        feedback['batteries'] = batteries
                elif action == 'show_all_battery':
                    progress(session, 'Checking battery variables', 'Reading the individual variables in the selected batteries.')
                    feedback = {'batteries': self.picker.show_all_battery(reply['folder'], reply['surface_all_battery'])}
                elif action == 'subthemes':
                    progress(session, 'Checking subthemes', 'Looking up subthemes for the selected themes.')
                    feedback = {'subthemes': self.picker.get_subthemes(reply['themes_for'])}
                elif action == 'search':
                    progress(session, 'Finding relevant variables', 'Searching the available codebooks for variables that fit the question.')
                    feedback = {'search_results': self.picker.search(reply['search_for'])}
                elif action == 'columns':
                    progress(session, 'Checking data columns', 'Using column names to find fields in a folder without a codebook.')
                    folder = reply['folder']
                    if folder not in catalog.folders:
                        raise ValueError('Select an allowed folder before sampling column names.')
                    feedback = {'sample_columns': catalog.sample_columns(folder),
                                'survey_weights': self.picker.weight_candidates(folder)}
                elif action == 'read_skill':
                    if reply['skill'] not in SKILLS:
                        raise ValueError(f"Choose an available skill: {', '.join(SKILLS)}.")
                    if reply['skill'] == JOIN_SKILL and len(catalog.folders) < 2:
                        raise ValueError('Cross-folder joins require project scope with at least two published folders.')
                    loaded_skills.add(reply['skill'])
                    progress(session, 'Choosing a method', f"Checking the guidance for {SKILLS[reply['skill']]}.")
                    feedback = {'skill': (SKILL_PATH / (reply['skill'] + '.md')).read_text(encoding='utf-8')}
                    if reply['skill'] == JOIN_SKILL:
                        feedback.update(candidate_identifiers=candidate_identifiers(catalog),
                                        saved_join_rules=read_join_rules(catalog))
                elif action in {'sql', 'python'}:
                    if not loaded_skills:
                        raise ValueError('Read the relevant subskill before running code.')
                    cross_folder_skill = JOIN_SKILL in loaded_skills
                    join_checks = [] if cross_folder_skill else None
                    if action == 'sql':
                        try:
                            folders, query_columns = checked_sql(reply['code'], catalog, allow_preview, join_checks=join_checks)
                        except ValueError as error:
                            history.append(user_message(error_feedback(session, 'SQL validation', reply['code'], error)))
                            continue
                    else:
                        folders = {frame['folder'] for frame in reply['frames']}
                        if not folders or not folders <= catalog.folders.keys():
                            raise ValueError('Specify the allowed folder and variables for each Python frame.')
                        if len(folders) > 1 and not cross_folder_skill:
                            raise ValueError('Read the cross_folder_joins skill before pulling Python frames from multiple folders.')
                    if not folders <= active_weights.keys():
                        progress(session, 'Checking survey weights', 'Confirming which weight, if any, applies to the selected data.')
                        weight_candidates = {folder: self.picker.weight_candidates(folder) for folder in folders - active_weights.keys()}
                        feedback = {'choose_weights_first': weight_candidates,
                                    'guidance': 'Choose the documented weight or explicitly use no weight before executing.'}
                    else:
                        code = reply['code']
                        try:
                            if action == 'sql':
                                progress(session, 'Reviewing the query', 'Checking the query scope, joins, and calculations before running it.')
                                variables = [
                                    row for folder in sorted(folders)
                                    for row in catalog.review_variables(folder, {
                                        table['name']: query_columns[table['name']]
                                        for table in catalog.folders[folder]['tables'] if table['name'] in query_columns
                                    })
                                ]
                                # The structural SQL check has passed; the reviewer now receives the question, variables and weight choices.
                                review = session.ask_agent('sql_reviewer', SQL_REVIEW, [user_message({
                                    'question': self.question, 'task': task, 'sql': code, 'scope': catalog.context(),
                                    'raw_preview_allowed': allow_preview, 'weights': {key: active_weights[key] for key in folders},
                                    'query_variables': variables, 'cross_folder_skill': cross_folder_skill,
                                })], SqlReview, allow_user_questions=False)
                                if not review['approved']:
                                    progress(session, 'Revising the query', 'Adjusting the query after review.')
                                    history.append(user_message({'sql_review_blocked': review['issues']}))
                                    continue
                            session.record('analysis_code', 'analysis_agent', {'language': action, 'code': code, 'folders': sorted(folders)})
                            if action == 'sql':
                                progress(session, 'Running the query', 'Calculating the result in Atlas.')
                                outcome = execute_sql(code, session=session, join_checks=join_checks or ())
                            else:
                                progress(session, 'Running Python', 'Preparing the selected columns and running the calculation in the sandbox.')
                                work = session.run_path / f'python_{self.turns:03}'
                                work.mkdir()
                                outcome = self.python(reply, active_weights, work)
                            session.record('analysis_execution_result', 'analysis_agent', {'language': action, **outcome})
                            results.append(outcome)
                            shape = {'rows': len(outcome['rows']), 'columns': len(outcome['columns'])}
                            progress(session, 'Calculation complete',
                                     f"Received a {shape['rows']:,} × {shape['columns']:,} result.",
                                     code=code, result=shape)
                            weight_folders.update(folders)
                            feedback = {'execution_result': outcome}
                        except (InterruptedError, ResultLimit):
                            raise
                        except JoinCardinalityError as error:
                            feedback = {'join_blocked': str(error)}
                        except (ValueError, OSError, psycopg2.Error) as error:
                            feedback = error_feedback(session, action, reply['code'], error)
                else:
                    tables = reply['tables']
                    ran_analysis = reply['ran_analysis'] and bool(results)
                    method = format_method(reply['method']) if ran_analysis else None
                    if tables and not results:
                        raise ValueError('A result table needs a successful query or Python result.')
                    for table in tables:
                        result_table(table['columns'], table['rows'])
                    total_rows = sum(len(table['rows']) for table in tables)
                    if total_rows > MAX_ROWS:
                        raise ResultLimit(total_rows, max(len(table['columns']) for table in tables))
                    if ran_analysis and (not tables or not method['steps']):
                        raise ValueError('Present the computed table and an evidence-based method note.')
                    join_report = {}
                    if reply.get('join_success') == 'yes':
                        folders = sorted(set(reply['join_folders']))
                        rules = [rule.strip() for rule in reply['join_rules_discovered'] if rule.strip()]
                        if JOIN_SKILL not in loaded_skills or len(folders) < 2 or not set(folders) <= weight_folders:
                            raise ValueError('Record only a confirmed join between folders checked in this turn.')
                        if not 1 <= len(rules) <= 10 or any(len(rule.split()) > 30 for rule in rules):
                            raise ValueError('Return 1 to 10 join rule bullets of at most 30 words each.')
                        join_report = {'join_success': 'yes', 'join_folders': folders, 'join_rules_discovered': rules}
                    catalog.label_tables(tables, weight_folders)
                    previous = session.state.get('weights', {})
                    used = {folder: active_weights[folder] for folder in weight_folders}
                    continued = [f"{catalog.folders[folder]['name']} — {weight['label']}" for folder, weight in used.items()
                                 if weight['variable'] and previous.get(folder) == weight]
                    session.check_stop()
                    session.state.setdefault('weights', {}).update({key: value for key, value in pending_weights.items() if key in used})
                    return {'message': reply['message'], 'ran_analysis': ran_analysis, 'method': method, 'tables': tables,
                            'selected_variables': self.selected_variables(reply['selected_variables']),
                            'continued_weights': continued, **join_report}
            except ResultLimit:
                raise
            except (ValueError, KeyError) as error:
                feedback = {'tool_error': str(error)}
            session.record('analysis_tool_result', 'analysis_agent', {'action': action, 'result': feedback})
            history.append(user_message(feedback))
        return {'message': 'I couldn’t complete that analysis with the available information.'}


SKILLS = {'general': 'general analysis and cross-tabs', 'tests': 'hypothesis tests', JOIN_SKILL: 'cross-folder joins'}


SKILL_PATH = Path(__file__).with_name('skills')
