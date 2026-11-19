import json
import shutil
from pathlib import Path
from v_5.analyse.managers.agent import SESSION_MANAGER, SessionManagerReply


def limit_words(text, maximum, exempt=()):
    """Shorten text while optionally exempting variable names from the limit.

    Inputs: text: source; maximum: counted words; exempt: exact words excluded after
        punctuation stripping.
    Returns: Whitespace-normalized text ending at the word limit.
    Details: Used for navigation and rollover notes so technical variable names can survive
        short summaries.
    """
    words = []
    counted = 0
    for word in text.split():
        if word.strip('.,;:!?()[]{}"\'\x60*') not in exempt:
            if counted == maximum:
                break
            counted += 1
        words.append(word)
    return ' '.join(words)


def hover_summary(text):
    """Make a short navigation preview with balanced formatting markers.

    Inputs: text: question or answer text.
    Returns: At most thirty words, with an unmatched bold/code marker removed.
    Details: Used by turn creation, migration and session-manager updates.
    """
    summary = limit_words(text, 30)
    # A word limit can cut through a bold or code span.
    for marker in ('**', '`'):
        if summary.count(marker) % 2:
            before, _, after = summary.rpartition(marker)
            summary = before + after
    return summary


def new_turn(turns, question, folder):
    """Append an empty question-and-answer turn to chat history.

    Inputs: turns: mutable chronological list; question: user text; folder: scope key or
        blank.
    Returns: New turn dict with IDs, navigation and rollover placeholders; also appends it
        to turns.
    Details: Called when a question starts, or when initialize_turns reconstructs older
        messages.
    """
    previous = int(turns[-1]['id'].rsplit('_', 1)[1]) if turns else 0
    turn_id = f'turn_{previous + 1:06d}'
    navigation = {'header': limit_words(question, 7), 'summary': hover_summary(question)}
    rollover = {'header': limit_words(question, 20), 'bullets': [], 'variables': []}
    turn = {'id': turn_id, 'parent_id': turn_id, 'folder': folder or None,
            'question_id': None, 'answer_id': None, 'navigation': navigation, 'rollover': rollover}
    turns.append(turn)
    return turn


def initialize_turns(state):
    """Bring saved message history into the current turn format.

    Inputs: state: mutable chat state with messages and optional turns.
    Returns: Boolean indicating whether state changed.
    Details: Used while loading chats. Existing turns get bounded previews; older chats gain
        turn/message links and fallback summaries without model calls.
    """
    if 'turns' in state:
        changed = False
        for turn in state['turns']:
            navigation = turn['navigation']
            summary = hover_summary(navigation['summary'])
            if summary != navigation['summary']:
                navigation['summary'] = summary
                changed = True
        return changed
    turns = state['turns'] = []
    turn = None
    question = ''
    for message in state['messages']:
        if message['agent'] == 'You':
            question = message['text']
            turn = new_turn(turns, question, None)
            turn['question_id'] = message['id']
        if turn:
            message['turn_id'] = turn['id']
            if message['agent'] != 'You' and message.get('kind') != 'thinking':
                turn['answer_id'] = message['id']
                turn['navigation']['summary'] = hover_summary(message['text'])
                turn['rollover']['bullets'] = [
                    limit_words('Asked ' + question, 30),
                    limit_words('Answer ' + message['text'], 30),
                ]
    return True


def turn_messages(state, turn):
    """Look up the question and answer attached to a turn.

    Inputs: state: chat messages; turn: question_id and answer_id links.
    Returns: (question, answer) message dicts; a missing link produces an empty dict.
    Details: Shared by recall and transcript helpers.
    """
    messages = {message['id']: message for message in state['messages']}
    return messages.get(turn['question_id'], {}), messages.get(turn['answer_id'], {})


def turn_content(state, turn):
    """Extract the raw text pair for one saved turn.

    Inputs: state: chat messages; turn: linked turn record.
    Returns: Dict with turn_id, question and answer text.
    Details: Used by memory recall and the session manager; missing messages become empty
        text.
    """
    question, answer = turn_messages(state, turn)
    return {'turn_id': turn['id'], 'question': question.get('text', ''), 'answer': answer.get('text', '')}


def current_summary(turn):
    """Package a completed turn's compact memory record.

    Inputs: turn: ID, parent, folder and rollover notes.
    Returns: Dict combining identifiers with rollover header, bullets and variables.
    Details: Used to seed analysis memory and provide recent context to
        session_management_agent.
    """
    return {'turn_id': turn['id'], 'parent_id': turn['parent_id'], 'folder': turn['folder'], **turn['rollover']}


def apply_parents(turns, changes):
    """Validate and apply consecutive turn groupings.

    Inputs: turns: chronological mutable records; changes: turn ID to proposed parent ID.
    Returns: None; updates parent_id only after the complete proposed grouping passes
        validation.
    Details: Used by update_turn. Every group must begin at its own parent turn and contain
        only adjacent turns.
    """
    parents = {turn['id']: turn['parent_id'] for turn in turns}
    if not changes.keys() <= parents.keys() or not set(changes.values()) <= parents.keys():
        raise ValueError('A proposed parent or turn ID does not exist.')
    parents.update(changes)
    active = None
    for turn in turns:
        turn_id = turn['id']
        parent_id = parents[turn_id]
        if parent_id != active:
            if parent_id != turn_id:
                raise ValueError('Each group must start with its own ID and contain only consecutive turns.')
            active = parent_id
    for turn in turns:
        turn['parent_id'] = parents[turn['id']]


def update_turn(state, turn, reply):
    """Apply the session manager's summaries and grouping decisions.

    Inputs: state: chat turns; turn: current record; reply: SessionManagerReply dict.
    Returns: None; updates bounded navigation/rollover fields and parent links, or raises on
        invalid IDs/grouping.
    Details: Called after session_management_agent responds. Deduplicates variables, limits
        summary lengths and delegates group validation to apply_parents.
    """
    if reply['turn_id'] != turn['id']:
        raise ValueError('The session manager returned the wrong turn ID.')
    navigation = reply['navigation']
    rollover = reply['rollover']
    variables = list(dict.fromkeys(name.strip() for name in rollover['variables'] if name.strip()))
    exempt = set(variables)
    turn['navigation'] = {'header': limit_words(navigation['header'], 7) or turn['navigation']['header'],
                          'summary': hover_summary(navigation['summary']) or turn['navigation']['summary']}
    turn['rollover'] = {'header': limit_words(rollover['header'], 20) or turn['rollover']['header'],
                        'bullets': [limit_words(text, 30, exempt) for text in rollover['bullets'][:4] if text.strip()],
                        'variables': variables}
    older = {}
    for update in reply['previous_parent_updates']:
        key = update['turn_id']
        if key == turn['id'] or key in older:
            raise ValueError('Previous parent updates must name each earlier turn at most once.')
        older[key] = update['parent_id']
    apply_parents(state['turns'], {**older, turn['id']: reply['parent_id']})


def variable_key(variable):
    """Identify a variable consistently within its dataset.

    Inputs: variable: record containing folder and name.
    Returns: (folder, casefolded name) tuple.
    Details: Shared by analyst selection and turn history to deduplicate variables without
        combining different folders.
    """
    return variable['folder'], variable['name'].casefold()


def remember_variables(state, turn, selected, reported):
    """Record selected variables and mark first-time use in the chat.

    Inputs: state/turn: mutable history; selected: verified analyst selections; reported:
        manager descriptions.
    Returns: None; updates turn variables_used/new_variables and the chat-wide
        variables_used list.
    Details: Called during turn completion. The manager can describe selected variables but
        cannot add unselected variables to the record.
    """
    master = state.setdefault('variables_used', [])
    known = {variable_key(variable) for variable in master}
    descriptions = {variable_key(variable): variable for variable in reported}
    turn['variables_used'], turn['new_variables'] = [], []
    for variable in selected:
        key = variable_key(variable)
        summary = descriptions.get(key, {})
        entry = {**variable, 'description': summary.get('description') or variable['description'],
                 'categories_or_type': variable['categories_or_type'] or summary.get('categories_or_type', '')}
        turn['variables_used'].append(entry)
        if key not in known:
            known.add(key)
            master.append({**entry, 'first_turn_id': turn['id']})
            turn['new_variables'].append({'folder': entry['folder'], 'name': entry['name']})


def complete_turn(session, turn):
    """Summarize and group a finished analysis turn.

    When: Analysis manager session_management_agent, after the answer is saved.
    Inputs: session: chat state and model/logging methods; turn: current question record.
    Returns: None; sets answer links, navigation/rollover notes and variable history.
    Flow: Builds deterministic fallback notes, asks the manager using current text and
        recent summaries, validates grouping, and keeps the fallback if the manager fails or
        is cancelled.
    """
    state = session.state
    with session.lock:
        answer = next(message for message in reversed(state['messages'])
                      if message.get('turn_id') == turn['id'] and message['agent'] != 'You'
                      and message.get('kind') != 'thinking')
        turn['answer_id'] = answer['id']
        earlier = [item for item in state['turns'] if item['id'] != turn['id'] and item['answer_id']]
        current = turn_content(state, turn)
        selected = (answer.get('details') or {}).get('selected_variables', [])
        turn['navigation']['summary'] = hover_summary(current['answer']) or turn['navigation']['summary']
        turn['rollover']['bullets'] = [limit_words('Asked ' + current['question'], 30)]
        if current['answer']:
            turn['rollover']['bullets'].append(limit_words('Answer ' + current['answer'], 30))
        state['activity'] = 'Organizing turn history'
        session.save()
    # The manager sees the current answer plus compact history; it does not rerun the analysis.
    payload = {
        'current_turn': current,
        'selected_variables': selected,
        'chronological_index': [{'turn_id': item['id'], 'parent_id': item['parent_id'],
                                 'header': item['navigation']['header']} for item in earlier],
        'recent_rollover_summaries': [current_summary(item) for item in earlier[-10:]],
    }
    reported = []
    try:
        session.check_stop()
        reply = session.ask_agent('session_management_agent', SESSION_MANAGER,
                                  [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                                  SessionManagerReply, allow_user_questions=False)
        with session.lock:
            session.check_stop()
            reported = reply['variables_used']
            update_turn(state, turn, reply)
    except InterruptedError:
        pass
    except Exception as error:
        session.record('turn_management_error', 'session_management_agent', {'error': str(error)})
    # Even if manager summarization failed, retain the analyst's verified variable selections.
    with session.lock:
        remember_variables(state, turn, selected, reported)
    session.record('turn_finished', 'session_management_agent', turn)


def write_transcript(project, state, turn, run=None, *, directory=None):
    """Archive one turn's raw conversation and execution evidence.

    Inputs: project: project root; state/turn: linked messages; run: optional event
        directory; directory: optional chat archive override.
    Returns: None; writes one turn text file and appends it to transcript.txt; skips an
        already archived turn.
    Details: Called during chat cleanup. Includes display details, progress and bounded-path
        raw logs; writes the per-turn file before extending the combined transcript.
    """
    archive = directory if directory is not None else project / 'analysis'
    folder = archive / 'turns'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (turn['id'] + '.txt')
    if target.is_file():
        return
    temporary = target.with_suffix('.tmp')
    question, answer = turn_messages(state, turn)
    with temporary.open('w', encoding='utf-8', newline='\n') as output:
        output.write(f"------------\nTURN {turn['id']}\n\nUSER QUESTION\n{question.get('text', '')}\n"
                     f"\nANSWER SHOWN\n{answer.get('text', '')}\n\nANSWER DETAILS\n")
        output.write(json.dumps(answer.get('details') or {}, ensure_ascii=False, indent=2) + '\n')
        for message in state['messages']:
            if message.get('turn_id') == turn['id'] and message.get('kind') == 'thinking':
                output.write(f"\nCHAT PROGRESS · {message['time']}\n{message['text']}\n")
                if message.get('details'):
                    output.write(json.dumps(message['details'], ensure_ascii=False, indent=2) + '\n')
        if run:
            with (run / 'timeline.jsonl').open(encoding='utf-8') as timeline:
                for line in timeline:
                    event = json.loads(line)
                    data = event['data']
                    output.write(f"\n[{event['sequence']:04}] {event['kind']} · {event['agent']} · {event['time']}\n")
                    if event['kind'] == 'analysis_code':
                        output.write(f"{data['language'].upper()}\n{data['code'].rstrip()}\n")
                    else:
                        output.write(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
                    if event['kind'] == 'analysis_python_execution' and data.get('full_log'):
                        raw_log = Path(data['full_log']).resolve()
                        if raw_log.is_relative_to(run.resolve()) and raw_log.is_file():
                            output.write('\nRAW PYTHON OUTPUT\n')
                            with raw_log.open(encoding='utf-8', errors='replace') as stdout:
                                shutil.copyfileobj(stdout, output)
                            output.write('\n')
        output.write('\n')
    temporary.replace(target)
    with (archive / 'transcript.txt').open('ab') as transcript, target.open('rb') as source:
        shutil.copyfileobj(source, transcript)
