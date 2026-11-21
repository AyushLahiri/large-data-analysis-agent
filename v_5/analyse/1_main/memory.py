import json
from openai import BadRequestError
from v_5.analyse.main.agent import TURN_MEMORY
from v_5.analyse.managers.turns import current_summary, limit_words, turn_content
from v_5.main_functions.session import loader_request


CONTEXT_LIMIT = 800_000


REPLY_TOKENS = 8_000


class AnalysisContextLimit(RuntimeError):
    """The current question cannot fit in the model's context."""


def user_message(payload):
    """Wrap a payload as a JSON conversation message.

    Inputs: payload: structured context or feedback, with other values converted to text.
    Returns: Dict with role=user and JSON content.
    Details: Shared by analyst/orchestrator memory, recalls and error feedback.
    """
    return {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, default=str)}


def recent_variables_text(state, folders):
    """Summarize recently selected variables within the current scope.

    Inputs: state: chat variable history; folders: allowed folder keys.
    Returns: Text listing up to ten recent variables, oldest first, or empty text.
    Details: Added to the analyst's initial context; includes locations, descriptions and
        categories/types without expanding full past turns.
    """
    entries = [item for item in state.get('variables_used', []) if item['folder'] in folders][-10:]
    if not entries:
        return ''
    lines = ['Recently selected variables, oldest first']
    for item in entries:
        location = f"{item['folder']}/{item['table_name']}.{item['column_name']}"
        fields = [item['name'], location, item['description'], item['categories_or_type']]
        lines.append(' | '.join(' '.join(value.split()) for value in fields if value))
    return '\n'.join(lines)


def roll_context(session, name, budget, reason):
    """Close one analyst or orchestrator conversation generation.

    Inputs: session supplies the journal, name identifies the role, budget holds
        its saved context state and reason explains the rollover.
    Returns: None. Advances the generation and clears its thread, recall cursors and read guides.
    Details: The role creates its replacement conversation on the next request.
    """
    budget.update(generation=budget['generation'] + 1, used_tokens=0)
    for key in ('thread_id', 'recall_ids', 'read_summaries', 'read_skills'):
        budget.pop(key, None)
    session.record('analysis_context_rollover', name, {'generation': budget['generation'], 'reason': reason})


def rollover_finished_contexts(session):
    """Apply pending rollovers after turn completion.

    Inputs: session contains the saved per-role token budgets.
    Returns: None. Closes each generation at its limit or carrying a limit flag.
    Details: The new conversation can then seed itself from the completed turn.
    """
    for name, budget in session.state.get('analysis_context', {}).items():
        if budget['used_tokens'] >= CONTEXT_LIMIT or budget.get('limit_hit'):
            roll_context(session, name, budget, budget.pop('limit_hit', '800,000-token analysis context'))


class AnalysisMemory:
    """Two independent API conversations per chat. Local histories contain only this question."""

    def __init__(self, session):
        self.session = session
        self.budgets = session.state.setdefault('analysis_context', {})
        self.previous = {turn['id']: turn for turn in session.state.get('turns', [])
                         if turn['answer_id'] and turn['id'] != session.turn_id}
        self.sent = {}

    def legacy_exchanges(self, turn_ids):
        """Recover displayed exchanges when a pre-thread chat has only its ordinary archive."""
        messages = {item['id']: item for item in self.session.state.get('messages', [])}
        result = []
        for turn_id in turn_ids:
            turn = self.previous[turn_id]
            question = messages.get(turn['question_id'], {})
            answer = messages.get(turn['answer_id'], {})
            result.append(user_message({'turn_id': turn_id, 'folder': turn['folder'], 'question': question.get('text', '')}))
            shown = {'answer_shown': answer.get('text', ''), 'details': answer.get('details')}
            result.append({'role': 'assistant', 'content': json.dumps(shown, ensure_ascii=False)})
        return result

    def starting_messages(self, name, budget):
        """Build the initial messages for a role's new conversation.

        Inputs: name selects the role and budget holds its generation and saved exchanges.
        Returns: Messages to prepend once to the first request in that conversation.
        Flow: Recover displayed exchanges for a first-generation legacy chat. After
            rollover, seed the orchestrator with up to five exchanges or the analyst
            with up to ten recent summaries and the preceding ten headers.
        """
        ids = list(self.previous)
        if budget['generation'] == 1:
            return self.legacy_exchanges(ids)
        if name == 'analysis_orchestrator':
            sets = budget.get('recent_sets', [])
            # Older installs archived displayed answers before per-role exchanges existed.
            before = ids[:ids.index(sets[0]['turn_id'])] if sets and sets[0]['turn_id'] in self.previous else ids
            missing = 5 - len(sets)
            messages = self.legacy_exchanges(before[-missing:]) if missing > 0 else []
            for entry in sets:
                messages.extend(entry['messages'])
            return messages
        recent, earlier = ids[-10:], ids[-20:-10]
        budget['recall_ids'] = {'recent': recent, 'earlier': earlier}
        return [user_message({'turn_memory': {
            'recent_summaries': [current_summary(self.previous[key]) for key in recent],
            'earlier_headers': [{'turn_id': key, 'parent_id': self.previous[key]['parent_id'],
                                 'header': self.previous[key]['rollover']['header']} for key in earlier],
        }})]

    def remember_orchestrator(self, history):
        """Checkpoint one complete orchestrator exchange for a future rollover.

        Inputs: history spans the current question, recalls and routing or direct reply.
        Returns: None. Retains up to five exchanges keyed by user turn ID.
        """
        if self.session.turn_id:
            budget = self.budgets['analysis_orchestrator']
            sets = [entry for entry in budget.get('recent_sets', []) if entry['turn_id'] != self.session.turn_id]
            budget['recent_sets'] = [*sets[-4:], {'turn_id': self.session.turn_id, 'messages': history.copy()}]
            self.session.save()

    def recall(self, agent, request):
        """Retrieve a saved summary or full question and answer for one completed turn.

        Inputs: agent is the requesting role. request contains a turn ID and detail level.
        Returns: The requested record or an error the role can act on.
        Details: The analyst can recall turns listed at rollover. For an earlier turn
            supplied as a header, it reads the summary before requesting the full exchange.
        """
        if not request:
            return {'error': 'Supply a turn ID and summary or full.'}
        turn_id = request['turn_id']
        turn = self.previous.get(turn_id)
        if not turn:
            return {'error': 'This turn is outside the completed history of this chat.'}
        budget = self.budgets[agent]
        if agent == 'analysis_agent':
            listed = budget.get('recall_ids', {})
            if turn_id not in listed.get('recent', []) and turn_id not in listed.get('earlier', []):
                return {'error': 'Choose a turn ID supplied in the rollover context.'}
            if turn_id in listed['earlier']:
                read = budget.setdefault('read_summaries', [])
                if request['detail'] == 'full' and turn_id not in read:
                    return {'error': 'Read the earlier turn summary first, then request its full question and answer.'}
                if request['detail'] == 'summary' and turn_id not in read:
                    read.append(turn_id)
        return current_summary(turn) if request['detail'] == 'summary' else turn_content(self.session.state, turn)

    def request_too_large(self, name, request, used):
        """Check whether a request would use the space reserved for the reply.

        Inputs: name identifies the role, request is the pending API payload and
            used is the prior conversation usage.
        Returns: True if the input exceeds the context limit less the reply allowance.
        Details: A byte estimate avoids an API token count for small requests.
        """
        upper = used + len(json.dumps(request, ensure_ascii=False).encode('utf-8')) + 1024
        maximum = CONTEXT_LIMIT - REPLY_TOKENS
        return upper > maximum and self.session.count_input(request, agent=name) > maximum

    def ask(self, name, prompt, history, contract):
        """Send one structured request on the analyst or orchestrator conversation.

        Inputs: name selects the role, prompt gives instructions, history contains
            this question's messages and contract defines the reply.
        Returns: The validated reply dict. Advances the per-role history cursor.
        Flow: Send new messages on the existing thread or initialize a new one with
            its role's seed. At the context limit, roll over and retry once.
        """
        budget = self.budgets.setdefault(name, {'generation': 1, 'used_tokens': 0})
        for attempt in range(2):
            existing = budget.get('thread_id')
            bootstrap = []
            if existing:
                inputs = history[self.sent.get(name, 0):]
                thread_id = existing
            else:
                bootstrap = self.starting_messages(name, budget)
                inputs = bootstrap + history
                thread_id = self.session.analysis_request(lambda client: client.conversations.create()).id
            instructions = prompt + TURN_MEMORY if name == 'analysis_agent' and budget['generation'] > 1 else prompt
            request = loader_request(instructions, [], contract)
            request.update(instructions=instructions, input=inputs, conversation=thread_id)
            try:
                if self.request_too_large(name, request, budget['used_tokens'] if existing else 0):
                    # A single long investigation can fill a fresh thread. Keep the live
                    # question (including its variables) and latest feedback, plus the rollover seed.
                    if attempt and name == 'analysis_agent' and len(history) > 2:
                        request['input'] = bootstrap + [history[0], history[-1]]
                    if not attempt or self.request_too_large(name, request, 0):
                        raise AnalysisContextLimit()
                text, usage, status = self.session.call_loader(name, request, REPLY_TOKENS, allow_user_questions=False)
            except (AnalysisContextLimit, BadRequestError) as error:
                if isinstance(error, BadRequestError) and error.code != 'context_length_exceeded':
                    raise
                if attempt:
                    raise AnalysisContextLimit('This question and its required rollover context exceed the analysis limit.') from error
                roll_context(self.session, name, budget, '800,000-token analysis context or model context limit')
                continue
            budget.update(thread_id=thread_id, used_tokens=usage['input_tokens'] + usage['output_tokens'])
            self.session.save()
            if status != 'completed':
                raise RuntimeError(f'{name} returned an incomplete response. See the saved response.')
            reply = contract.model_validate_json(text).model_dump()
            if 'why_user' in reply:
                reply['why_user'] = limit_words(reply['why_user'], 40) if reply['action'] == 'clarify' else ''
            history.append({'role': 'assistant', 'content': json.dumps(reply, ensure_ascii=False)})
            self.sent[name] = len(history)
            return reply
