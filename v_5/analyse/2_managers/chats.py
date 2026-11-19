from contextlib import contextmanager
from psycopg2.extras import Json, execute_values
from v_5.analyse.managers.turns import limit_words
from v_5.main_functions.sql import atlas_transaction


CHAT_SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS public.atlas_chats (
        chat_id text PRIMARY KEY,
        project_id text NOT NULL REFERENCES public.atlas_projects(project_id) ON DELETE CASCADE,
        title text NOT NULL DEFAULT 'New chat',
        updated_at timestamptz NOT NULL DEFAULT now(),
        checkpoint jsonb NOT NULL DEFAULT '{}'::jsonb
    )''',
    '''CREATE TABLE IF NOT EXISTS public.atlas_chat_turns (
        chat_id text NOT NULL REFERENCES public.atlas_chats(chat_id) ON DELETE CASCADE,
        turn_id text NOT NULL,
        turn_number integer NOT NULL,
        question jsonb NOT NULL,
        answer jsonb NOT NULL,
        PRIMARY KEY (chat_id, turn_id),
        UNIQUE (chat_id, turn_number)
    )''',
    '''CREATE TABLE IF NOT EXISTS public.atlas_chat_sessions (
        chat_id text NOT NULL,
        turn_id text NOT NULL,
        parent_id text NOT NULL,
        folder text,
        navigation jsonb NOT NULL,
        rollover_notes jsonb NOT NULL,
        PRIMARY KEY (chat_id, turn_id),
        FOREIGN KEY (chat_id, turn_id) REFERENCES public.atlas_chat_turns(chat_id, turn_id) ON DELETE CASCADE
    )''',
    'CREATE INDEX IF NOT EXISTS atlas_chats_project ON public.atlas_chats(project_id, updated_at DESC)',
)


CHAT_COLUMNS = (
    "ALTER TABLE public.atlas_chats ADD COLUMN IF NOT EXISTS variables_used jsonb NOT NULL DEFAULT '[]'::jsonb",
    "ALTER TABLE public.atlas_chat_turns ADD COLUMN IF NOT EXISTS feedback jsonb",
    "ALTER TABLE public.atlas_chat_sessions ADD COLUMN IF NOT EXISTS variables_used jsonb NOT NULL DEFAULT '{}'::jsonb",
)


@contextmanager
def chat_transaction():
    """Open a transaction with the required chat tables available.

    Inputs: Uses Atlas connection helpers; no arguments.
    Returns: Context manager yielding an SQL cursor.
    Details: Called by chat persistence methods. Checks expected columns and uses a
        transaction lock when first-time setup or migration is needed.
    """
    with atlas_transaction() as cursor:
        cursor.execute('''SELECT count(*) FROM pg_attribute
            WHERE NOT attisdropped AND (attrelid, attname) IN (
                (to_regclass('public.atlas_chats'), 'variables_used'),
                (to_regclass('public.atlas_chat_turns'), 'feedback'),
                (to_regclass('public.atlas_chat_sessions'), 'variables_used'))''')
        if cursor.fetchone()[0] != len(CHAT_COLUMNS):
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('v5 atlas chats')::bigint)")
            for statement in (*CHAT_SCHEMA, *CHAT_COLUMNS):
                cursor.execute(statement)
        yield cursor


def chat_title(state):
    """Derive a short chat title from its first question.

    Inputs: state: chat messages.
    Returns: First user question limited to seven words, or New chat.
    Details: Shared by local UI state and SQL chat records.
    """
    question = next((message['text'] for message in state['messages'] if message['agent'] == 'You'), '')
    return limit_words(question, 7) or 'New chat'


def display_message(message):
    """Keep the user-visible part of a message for SQL archival.

    Inputs: message: full local chat message.
    Returns: psycopg Json wrapper with display fields and tables/method/precision/analysis
        details.
    Details: archive_chat uses this smaller payload; full execution evidence remains in
        local run logs and transcripts.
    """
    fields = ('id', 'agent', 'text', 'kind', 'time', 'turn_id')
    saved = {key: message[key] for key in fields if key in message}
    details = message.get('details') or {}
    saved['details'] = {key: details[key] for key in ('tables', 'method', 'decimal_places', 'ran_analysis') if key in details}
    return Json(saved)


def create_chat(project_id, chat_id):
    """Register a new analysis chat in Atlas.

    Inputs: project_id: owner; chat_id: new unique key.
    Returns: None; inserts a chat registry row.
    Details: Called by the new-chat route inside chat_transaction; local session state is
        created when the chat is opened.
    """
    with chat_transaction() as cursor:
        cursor.execute('INSERT INTO public.atlas_chats (chat_id, project_id) VALUES (%s, %s)', (chat_id, project_id))


def list_chats(project_id):
    """List a project's saved analysis chats.

    Inputs: project_id: owner key.
    Returns: List of id/title dicts, newest updated first.
    Details: Used by the browser chat selector; local fallback is handled by routes, not
        this SQL helper.
    """
    with chat_transaction() as cursor:
        cursor.execute('''SELECT chat_id, title FROM public.atlas_chats WHERE project_id = %s
                          ORDER BY updated_at DESC, chat_id''', (project_id,))
        return [{'id': chat_id, 'title': title} for chat_id, title in cursor.fetchall()]


def archive_chat(project_id, chat_id, state, *, register=False):
    """Save newly completed turns and their compact session state in Atlas.

    When: Chat synchronization after turn completion or legacy-chat registration.
    Inputs: project_id/chat_id: owner keys; state: local messages, turns, weights and
        memory; register: allow initial registry insertion.
    Returns: Highest archived turn number; writes new question/answer and session rows.
    Flow: Only complete linked turns are archived. Updates changed parent links for older
        turns, preserves feedback and checkpoints weights/context separately from local raw
        logs.
    """
    messages = {message['id']: message for message in state['messages']}
    finished = [(int(turn['id'].rsplit('_', 1)[1]), turn) for turn in state['turns']
                if turn['question_id'] in messages and turn['answer_id'] in messages]
    archived = state.get('atlas_archived_through', 0)
    # The checkpoint avoids reuploading completed answers; parent links may still change after later grouping.
    new_turns = [(number, turn) for number, turn in finished if number > archived]
    if not new_turns and not register:
        return archived
    with chat_transaction() as cursor:
        if register:
            cursor.execute('''INSERT INTO public.atlas_chats (chat_id, project_id) VALUES (%s, %s)
                              ON CONFLICT (chat_id) DO NOTHING''', (chat_id, project_id))
        cursor.execute('''UPDATE public.atlas_chats SET title = %s, checkpoint = %s, updated_at = now()
                          WHERE project_id = %s AND chat_id = %s''',
                       (chat_title(state), Json({key: state[key] for key in ('weights', 'analysis_context') if key in state}),
                        project_id, chat_id))
        if not cursor.rowcount:
            raise ValueError('This chat no longer exists.')
        if new_turns:
            execute_values(cursor, '''INSERT INTO public.atlas_chat_turns (chat_id, turn_id, turn_number, question, answer, feedback)
                VALUES %s ON CONFLICT (chat_id, turn_id) DO UPDATE SET question = EXCLUDED.question, answer = EXCLUDED.answer,
                feedback = COALESCE(EXCLUDED.feedback, atlas_chat_turns.feedback)''',
                [(chat_id, turn['id'], number, display_message(messages[turn['question_id']]),
                  display_message(messages[turn['answer_id']]), Json(turn['feedback']) if turn.get('feedback') else None)
                 for number, turn in new_turns])
            execute_values(cursor, '''INSERT INTO public.atlas_chat_sessions (chat_id, turn_id, parent_id, folder, navigation, rollover_notes, variables_used)
                VALUES %s ON CONFLICT (chat_id, turn_id) DO UPDATE SET parent_id = EXCLUDED.parent_id,
                folder = EXCLUDED.folder, navigation = EXCLUDED.navigation, rollover_notes = EXCLUDED.rollover_notes,
                variables_used = EXCLUDED.variables_used''',
                [(chat_id, turn['id'], turn['parent_id'], turn['folder'], Json(turn['navigation']), Json(turn['rollover']),
                  Json({'selected': turn.get('variables_used', []), 'new': turn.get('new_variables', [])}))
                 for _, turn in new_turns])
            execute_values(cursor, '''UPDATE public.atlas_chat_sessions AS session SET parent_id = saved.parent_id
                FROM (VALUES %s) AS saved(chat_id, turn_id, parent_id)
                WHERE session.chat_id = saved.chat_id AND session.turn_id = saved.turn_id
                AND session.parent_id IS DISTINCT FROM saved.parent_id''',
                [(chat_id, turn['id'], turn['parent_id']) for _, turn in finished])
            if any(turn.get('new_variables') for _, turn in new_turns):
                cursor.execute('UPDATE public.atlas_chats SET variables_used = %s WHERE project_id = %s AND chat_id = %s',
                               (Json(state['variables_used']), project_id, chat_id))
    return new_turns[-1][0] if new_turns else archived


def restore_chat(project_id, chat_id):
    """Rebuild an idle chat from its Atlas archive.

    Inputs: project_id/chat_id: owner keys.
    Returns: State dict with messages, turns, variables, weights/context and archive
        checkpoint; None if the chat is absent.
    Details: Used when the local session file is missing. Restores displayed question/answer
        pairs in turn order, not raw execution logs.
    """
    with chat_transaction() as cursor:
        cursor.execute('SELECT checkpoint, variables_used FROM public.atlas_chats WHERE project_id = %s AND chat_id = %s',
                       (project_id, chat_id))
        chat = cursor.fetchone()
        if chat is None:
            return None
        cursor.execute('''SELECT turn.turn_id, turn.turn_number, turn.question, turn.answer, session.parent_id,
                                 session.folder, session.navigation, session.rollover_notes, session.variables_used, turn.feedback
                          FROM public.atlas_chat_turns AS turn JOIN public.atlas_chat_sessions AS session USING (chat_id, turn_id)
                          WHERE turn.chat_id = %s ORDER BY turn.turn_number''', (chat_id,))
        state = {'weights': {}, **chat[0], 'messages': [], 'turns': [], 'status': 'idle',
                 'variables_used': chat[1], 'atlas_archived_through': 0}
        for turn_id, number, question, answer, parent, folder, navigation, rollover, variables, feedback in cursor.fetchall():
            state['messages'].extend((question, answer))
            state['turns'].append({'id': turn_id, 'parent_id': parent, 'folder': folder, 'navigation': navigation,
                                   'rollover': rollover, 'question_id': question['id'], 'answer_id': answer['id'],
                                   'variables_used': variables.get('selected', []), 'new_variables': variables.get('new', []),
                                   'feedback': feedback})
            state['atlas_archived_through'] = number
        return state


def save_turn_feedback(project_id, chat_id, turn_id, feedback):
    """Save a rating on an archived answer.

    Inputs: project_id/chat_id/turn_id: ownership and answer keys; feedback: rating and
        optional comment.
    Returns: None; raises if the answer no longer exists.
    Details: The feedback route uses this for already archived turns and stores unarchived
        feedback in local state first.
    """
    with chat_transaction() as cursor:
        cursor.execute('''UPDATE public.atlas_chat_turns AS turn SET feedback = %s
            FROM public.atlas_chats AS chat WHERE chat.project_id = %s AND chat.chat_id = %s
            AND turn.chat_id = chat.chat_id AND turn.turn_id = %s''', (Json(feedback), project_id, chat_id, turn_id))
        if not cursor.rowcount:
            raise ValueError('This answer is no longer available.')


def delete_chat(project_id, chat_id):
    """Remove a chat and its dependent SQL records.

    Inputs: project_id/chat_id: owner keys.
    Returns: None; deletes the matching chat row.
    Details: The deletion route coordinates local-directory cleanup; database foreign keys
        remove dependent turn/session records.
    """
    with chat_transaction() as cursor:
        cursor.execute('DELETE FROM public.atlas_chats WHERE project_id = %s AND chat_id = %s', (project_id, chat_id))
