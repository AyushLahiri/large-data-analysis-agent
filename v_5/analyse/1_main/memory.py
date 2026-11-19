import json
from openai import BadRequestError
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


def rollover_finished_contexts(session):
    """Reset exhausted analysis budgets after a turn finishes.

    When: Analysis turn cleanup, after complete_turn; distinct from ingestion handoff
        summarization.
    Inputs: session: state containing per-agent analysis_context budgets.
    Returns: None; increments affected generations, resets used_tokens and records rollover
        reasons.
    Flow: Checks an explicit context-limit flag or the cumulative 800,000-token budget. The
        next question is seeded from saved turn summaries by AnalysisMemory.
    """
    for name, budget in session.state.get('analysis_context', {}).items():
        reason = budget.pop('limit_hit', '')
        if not reason and budget['used_tokens'] >= CONTEXT_LIMIT:
            reason = '800,000-token analysis budget'
        if reason:
            budget.update(generation=budget['generation'] + 1, used_tokens=0)
            session.record('analysis_context_rollover', name, {'generation': budget['generation'], 'reason': reason})


class AnalysisMemory:
    def __init__(self, session):
        """Select the completed turns available to this question.

        Inputs: session: current analysis chat and turn ID.
        Returns: None; binds per-role budgets and indexes twenty earlier completed turns.
        Details: The newest ten supply summaries; the preceding ten supply headers and require
            summary-first recall. The current turn is excluded.
        """
        self.session = session
        self.budgets = session.state.setdefault('analysis_context', {})
        previous = [turn for turn in session.state.get('turns', [])
                    if turn['answer_id'] and turn['id'] != session.turn_id]
        self.recent = {turn['id']: turn for turn in previous[-10:]}
        self.earlier = {turn['id']: turn for turn in previous[-20:-10]}
        self.read_summaries = set()

    def seed(self, payload):
        """Start a role conversation with compact saved turn context.

        Inputs: payload: current question, scope and role-specific context.
        Returns: One-item message list containing payload plus turn_memory.
        Details: Used by analyst and orchestrator. Includes recent summaries and earlier headers
            instead of replaying complete old conversations.
        """
        return [user_message({
            **payload,
            'turn_memory': {
                'recent_summaries': [current_summary(turn) for turn in self.recent.values()],
                'earlier_headers': [{'turn_id': turn['id'], 'parent_id': turn['parent_id'],
                                     'header': turn['rollover']['header']} for turn in self.earlier.values()],
            },
        })]

    def recall(self, agent, request):
        """Retrieve a permitted past turn summary or full answer.

        Inputs: agent: requesting role; request: turn_id and detail (summary/full).
        Returns: Summary/content dict, or an error dict explaining why recall is unavailable.
        Details: Restricts access to the twenty-turn window and requires the same agent to read
            an earlier summary before its full question/answer.
        """
        if not request:
            return {'error': 'Supply a turn ID and summary or full.'}
        turn_id, detail = request['turn_id'], request['detail']
        turn = self.recent.get(turn_id) or self.earlier.get(turn_id)
        if not turn:
            return {'error': 'The turn is outside the available twenty-turn window.'}
        key = (agent, turn_id)
        if detail == 'summary':
            self.read_summaries.add(key)
            return current_summary(turn)
        if turn_id in self.earlier and key not in self.read_summaries:
            return {'error': 'Read the earlier turn summary first, then request its full question and answer if needed.'}
        return turn_content(self.session.state, turn)

    def request_too_large(self, name, request):
        """Check whether a model request leaves room for the reply.

        Inputs: name: agent label; request: complete API payload.
        Returns: Boolean indicating input exceeds the available context.
        Details: Uses serialized byte size as a cheap screen, then Session.count_input only for
            possibly large requests; reserves REPLY_TOKENS.
        """
        # Use the exact token count only when the request may exceed the context window.
        estimate = len(json.dumps(request, ensure_ascii=False).encode('utf-8')) + 1024
        max_input = CONTEXT_LIMIT - REPLY_TOKENS
        return estimate > max_input and self.session.count_input(request, agent=name) > max_input

    def ask(self, name, prompt, history, contract):
        """Call an analysis role while tracking its context budget.

        When: Main analysis orchestrator or analyst requests a structured reply.
        Inputs: name/prompt: role; history: mutable conversation; contract: expected reply
            model.
        Returns: Validated reply dict; updates cumulative usage/history and flags context
            exhaustion.
        Flow: Builds the request, checks size, calls Session.call_loader without automatic UI
            questions, and records usage. Rollover waits until turn cleanup rather than
            restarting mid-question.
        """
        budget = self.budgets.setdefault(name, {'generation': 1, 'used_tokens': 0})
        request = loader_request(prompt, history, contract)
        try:
            if self.request_too_large(name, request):
                raise AnalysisContextLimit()
            text, usage, status = self.session.call_loader(name, request, REPLY_TOKENS, allow_user_questions=False)
        except (AnalysisContextLimit, BadRequestError) as error:
            if isinstance(error, BadRequestError) and error.code != 'context_length_exceeded':
                raise
            budget['limit_hit'] = 'Model context limit'
            raise AnalysisContextLimit('This question exceeded the analysis context limit. Try narrowing the question or selecting a folder.') from error
        budget['used_tokens'] += usage['input_tokens'] + usage['output_tokens']
        self.session.save()
        if status != 'completed':
            raise RuntimeError(f'{name} returned an incomplete response. See the saved response.')
        reply = contract.model_validate_json(text).model_dump()
        if 'why_user' in reply:
            reply['why_user'] = limit_words(reply['why_user'], 40) if reply['action'] == 'clarify' else ''
        history.append({'role': 'assistant', 'content': json.dumps(reply, ensure_ascii=False)})
        return reply
