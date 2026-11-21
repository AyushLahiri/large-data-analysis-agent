import asyncio
import json
import queue
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from openai import AsyncOpenAI, OpenAI
from openai.lib._parsing._responses import type_to_text_format_param
from pathlib import Path
from typing import Any
from uuid import uuid4
from v_5.app.config import MODEL, PRICES, api_key
from v_5.app.docker import run_code
from v_5.main_functions.agent import GLITCH_PROMPT, GlitchReply


def write_json(path, value):
    """Save JSON without exposing a partly written file.

    Inputs: path: destination; value: JSON data, with other values converted to text.
    Returns: None; creates parent folders and replaces the destination.
    Details: Shared by state and event writers. Writes a sibling temporary file before
        replacing the old copy.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str), encoding='utf-8')
    temporary.replace(path)


def loader_request(prompt, history, contract=None):
    """Build a model request from a prompt and conversation.

    Inputs: prompt: system text; history: role/content messages; contract: optional response
        model.
    Returns: Request dict containing model, input messages, reasoning settings and optional
        JSON schema.
    Details: Used by agent callers and token counting so both see the same request shape.
    """
    request = dict(model=MODEL, input=[{'role': 'system', 'content': prompt}, *history], reasoning={'effort': 'medium'})
    if contract is not None:
        request['text'] = {'format': type_to_text_format_param(contract)}
    return request


def usage_cost(model, usage):
    """Price one model response in US dollars.

    Inputs: model: response model name; usage: Responses API usage dict.
    Returns: Dollar cost, or 0 for a model without a listed price.
    Details: Cached input tokens are part of input_tokens and are charged at the cached rate.
    """
    price = next((value for name, value in PRICES.items() if model.startswith(name)), None)
    if not price or not usage:
        return 0.0
    cached = (usage.get('input_tokens_details') or {}).get('cached_tokens') or 0
    fresh = usage.get('input_tokens', 0) - cached
    return (fresh * price[0] + cached * price[1] + usage.get('output_tokens', 0) * price[2]) / 1_000_000


def count_input(request):
    """Ask the API how many input tokens a request will use.

    Inputs: request: the model request built by loader_request or a caller.
    Returns: Integer input-token count; API failures propagate.
    Details: Called before budget decisions. Session.count_input adds cancellation for
        analysis and records the result.
    """
    return OpenAI(api_key=api_key()).post('/responses/input_tokens', body=request, cast_to=dict[str, Any])['input_tokens']


class Session:
    def __init__(self, path, state, *, analysis=False, turn_id=None, analysis_directory=None):
        """Set up a logged ingestion or analysis run.

        Inputs: path: project directory; state: mutable project/chat state; analysis: mode;
            turn_id: optional turn label; analysis_directory: optional chat directory.
        Returns: None; initializes queues, locks, cancellation state and a timestamped run
            directory.
        Details: Used when a route starts work. record saves the first event and links the run
            to the state file.
        """
        self.path, self.state = Path(path), state
        self.analysis = analysis
        self.turn_id = turn_id
        directory = (analysis_directory or self.path / 'analysis') if analysis else self.path
        self.state_path = directory / ('session.json' if analysis else 'project.json')
        self.lock = threading.RLock()
        self.handoff = None
        self.answers = queue.Queue()
        self.cancelled = threading.Event()
        self.analysis_error_count = 0
        self._cancel_operation = None
        self.phase_request = None
        self.run_path = directory / 'runs' / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ')
        self.run_path.mkdir(parents=True)
        self.sequence = 0
        self.record('run_started', 'app', {'turn_id': turn_id} if analysis else state)

    def save(self):
        """Persist the current project or chat state.

        Inputs: Uses this session's state and state_path.
        Returns: None; replaces the JSON state file.
        Details: Called after changes throughout the workflow. The reentrant lock also permits
            save inside other locked methods.
        """
        with self.lock:
            write_json(self.state_path, self.state)

    def record(self, kind, agent, data):
        """Append one event to the run journal and save its location.

        Inputs: kind: event type; agent: source label; data: event-specific payload.
        Returns: None; writes a full event JSON and chronological JSONL/Markdown entries.
        Details: Used by model, execution and user-interaction methods. Records sequence, time
            and optional analysis turn_id before saving state.
        """
        with self.lock:
            self.sequence += 1
            name = f'{self.sequence:05d}_{kind}.json'
            item = dict(sequence=self.sequence, time=datetime.now(timezone.utc).isoformat(), kind=kind, agent=agent, data=data)
            if self.turn_id:
                item['turn_id'] = self.turn_id
            write_json(self.run_path / 'events' / name, item)
            with (self.run_path / 'timeline.jsonl').open('a', encoding='utf-8') as log:
                log.write(json.dumps({**item, 'record': 'events/' + name}, default=str, ensure_ascii=False) + '\n')
            with (self.run_path / 'timeline.md').open('a', encoding='utf-8') as log:
                log.write(f"\n### {self.sequence:05d} · {agent} · {kind}\n\n[{name}](events/{name})\n\n")
                if kind in {'response', 'user_answer', 'error', 'phase_report'}:
                    log.write('```json\n' + json.dumps(data, indent=2, ensure_ascii=False, default=str) + '\n```\n')
            self.state['log_directory'] = str(self.run_path)
            self.save()

    def show(self, name, value):
        """Record an observation without adding a chat message.

        Inputs: name: observation label; value: the observed payload.
        Returns: None; appends an observation event.
        Details: Workflow-compatible display helper that delegates to record.
        """
        self.record('observation', name, value)

    def print(self, *values):
        """Record ordinary workflow output as one text event.

        Inputs: values: objects to join with spaces.
        Returns: None; appends an output event.
        Details: Lets workflow code send print-like progress to the permanent journal through
            record.
        """
        self.record('output', 'workflow', ' '.join(str(v) for v in values))

    def message(self, agent, text, kind='chat', details=None):
        """Add a message that the browser can display.

        Inputs: agent: speaker label; text: message; kind: chat or display category; details:
            optional tables, method, code or other UI payload.
        Returns: New message ID; also appends the message and saves state.
        Details: Used by agents and progress helpers. Messages carry time and, during analysis,
            the current turn_id.
        """
        with self.lock:
            message = {'id': uuid4().hex, 'agent': agent, 'text': text,
                       'kind': kind, 'details': details, 'time': datetime.now(timezone.utc).isoformat()}
            if self.turn_id:
                message['turn_id'] = self.turn_id
            phase = None if self.analysis else self.open_phase()
            if phase is not None:
                message['phase'] = phase
            self.state['messages'].append(message)
            self.save()
            return message['id']

    def begin_phase(self, label, folder=None):
        """Hand ingestion work to a new phase for the chat's timer and cost.

        Inputs: label: phase name shown in chat; folder: dataset folder, if any.
        Returns: None. Closes the open phase now; the new phase is listed once it
            produces a message or a priced response, so a phase that does no work is not shown.
        """
        with self.lock:
            self.close_phase()
            self.phase_request = {'label': label, 'folder': folder,
                                  'started': datetime.now(timezone.utc).isoformat()}

    def open_phase(self):
        """Return the index of the open ingestion phase, listing a requested one first."""
        with self.lock:
            phases = self.state.setdefault('phases', [])
            if self.phase_request:
                phases.append({**self.phase_request, 'ended': None, 'cost': 0.0, 'responses': []})
                self.phase_request = None
            return len(phases) - 1 if phases and not phases[-1]['ended'] else None

    def close_phase(self):
        """Stop the open phase's timer when work moves on or the run ends."""
        with self.lock:
            self.phase_request = None
            phases = self.state.get('phases') or []
            if phases and not phases[-1]['ended']:
                phases[-1]['ended'] = datetime.now(timezone.utc).isoformat()
                self.save()

    def add_cost(self, model, usage, response_id):
        """Add one model response's price to the open phase or analysis turn.

        Inputs: model and usage from the response; response_id prevents counting a
            retrieved background response twice.
        Returns: None. Ingestion adds to state phases; analysis adds to turn_costs.
        """
        cost = usage_cost(model, usage)
        with self.lock:
            if self.analysis:
                if self.turn_id:
                    costs = self.state.setdefault('turn_costs', {})
                    costs[self.turn_id] = costs.get(self.turn_id, 0.0) + cost
            else:
                index = self.open_phase()
                if index is None:
                    return
                phase = self.state['phases'][index]
                if response_id in phase['responses']:
                    return
                phase['responses'].append(response_id)
                phase['cost'] += cost
            self.save()

    def check_stop(self):
        """Stop work when the user has requested cancellation.

        Inputs: Reads this session's cancellation event.
        Returns: None, or raises InterruptedError.
        Details: Called between workflow steps and around cancellable operations.
        """
        if self.cancelled.is_set():
            raise InterruptedError('Stopped by the user.')

    @contextmanager
    def cancellable(self, cancel):
        """Register a cancellation callback for the current operation.

        Inputs: cancel: callable that interrupts the operation inside the context.
        Returns: Context manager yielding None; clears the callback on exit.
        Details: Used by API and database work. stop calls the registered callback; the lock
            protects registration.
        """
        with self.lock:
            self.check_stop()
            self._cancel_operation = cancel
        try:
            yield
        finally:
            with self.lock:
                self._cancel_operation = None

    def stop(self):
        """Request cancellation and interrupt any registered operation.

        When: The user stops an ingestion or analysis session.
        Inputs: Uses the session cancellation event and current callback.
        Returns: None; records the request and any failure to send the cancellation signal.
        Flow: Sets the stop flag once, then invokes the callback outside the lock. check_stop
            ends subsequent steps.
        """
        with self.lock:
            if self.cancelled.is_set():
                return
            self.cancelled.set()
            # Capture the callback under the lock, but call it after releasing the lock.
            cancel = self._cancel_operation
            self.record('stop_requested', 'user', {})
        if cancel is not None:
            try:
                cancel()
            except Exception as error:
                self.record('cancel_signal_error', 'analysis', {'error': str(error)})

    def analysis_request(self, operation):
        """Run an analysis API operation with cancellation support.

        Inputs: operation: async callable receiving an AsyncOpenAI client.
        Returns: The operation's result; cancellation or API errors propagate.
        Details: Creates a short-lived event loop/client and connects Session.stop to task
            cancellation.
        """
        async def run():
            """Await the API operation inside its cancellable client context.

            Inputs: Captures operation and the enclosing session.
            Returns: The awaited API result.
            Details: Private coroutine for analysis_request. A cancelled task becomes
                InterruptedError when the session was stopped.
            """
            loop, task = asyncio.get_running_loop(), asyncio.current_task()
            with self.cancellable(lambda: loop.call_soon_threadsafe(task.cancel)):
                try:
                    async with AsyncOpenAI(api_key=api_key()) as client:
                        return await operation(client)
                except asyncio.CancelledError:
                    self.check_stop()
                    raise

        return asyncio.run(run())

    def call_loader(self, name, request, max_output_tokens=None, *, allow_user_questions=True):
        """Call a model, record its response and surface user-facing content.

        When: Any workflow agent needs a raw model response.
        Inputs: name: agent label; request: API payload; max_output_tokens: optional limit;
            allow_user_questions: whether to publish forms/messages.
        Returns: (text, usage, status): raw output text, token-usage dict and completion status;
            may also update UI state.
        Flow: Logs both sides of the call, uses cancellation for analysis, and extracts
            structured questions/messages when enabled. ask_agent validates the returned text
            separately.
        """
        self.check_stop()
        if not self.analysis:
            with self.lock:
                self.state['activity'] = ('Checking unmatched variable names' if name == 'master_to_codebook_reconciler'
                                          else 'Building the final codebook' if name == 'final_codebook_creator'
                                          else 'Organizing codebooks' if name.startswith(('codebook_', 'pdf_codebook'))
                                          else 'Checking the prepared data with you' if name.startswith('descriptives')
                                          else 'Preparing your data' if name.startswith('data_loader') else 'Reviewing your materials')
                self.save()
        if max_output_tokens is not None:
            request = {**request, 'max_output_tokens': max_output_tokens}
        self.record('request', name, request)
        if self.analysis:
            response = self.analysis_request(lambda client: client.responses.create(**request, store='conversation' in request))
        else:
            response = OpenAI(api_key=api_key()).responses.create(**request, store=False)
        self.record('response', name, response.model_dump())
        self.add_cost(response.model, response.usage.model_dump() if response.usage else None, response.id)
        if self.analysis:
            self.check_stop()
        text = response.output_text
        self.present_reply(name, text, allow_user_questions=allow_user_questions)
        self.check_stop()  # Ingestion finishes and displays in-flight replies before stopping.
        return text, response.usage.model_dump(), response.status

    def present_reply(self, name, text, *, allow_user_questions=True):
        """Show a completed or restored reply using the existing question forms and messages.

        Inputs: agent name, JSON/text reply and question permission. Returns: None.
        The checkpointed loader calls this after saving its reply, so a restart can show
        an unanswered question without generating another model response.
        """
        if not allow_user_questions:
            return
        payload = {}
        try:
            payload = json.loads(text)
            # Normalize the different agent question formats into the one form shape the UI renders.
            questions = payload.get('user_questions', [])
            if name == 'context_checker' and not payload.get('accepted'):
                questions = [{'id': f'context_{i}', 'question': q, 'help_text': '', 'options': []}
                             for i, q in enumerate(payload.get('questions', []))]
            if payload.get('action') == 'clarify' and not questions:
                questions = [{'id': 'clarification', 'question': payload['question'], 'help_text': '', 'options': []}]
            if questions:
                form = {'id': uuid4().hex, 'questions': questions}
                with self.lock:
                    self.state['question_form'] = form
                    self.save()
                self.record('question_form', name, form)
            message = payload.get('message') or payload.get('summary') or payload.get('explanation')
            if not message and questions:
                message = 'A little context will help me continue.'
        except json.JSONDecodeError:
            message = text
        if message:
            thinking = name.startswith(('agent_', 'data_loader', 'descriptives', 'codebook_', 'pdf_codebook', 'loader_summarizer', 'glitch')) and payload.get('action') not in {'clarify', 'finish', 'stop', 'confirm', 'handoff'}
            self.message(name, message, 'thinking' if thinking else 'chat', payload if thinking else None)

    def ask_agent(self, name, prompt, history, contract, *, allow_user_questions=True, conversation=None):
        """Get a validated structured reply and extend the conversation.

        Inputs: name and prompt identify the role and its instructions. history contains
            the messages to send, contract defines the reply, and allow_user_questions
            controls presentation. conversation optionally identifies an existing API thread.
        Returns: A validated reply dict. Appends the assistant response to history.
        Details: Uses an 8,000-token output limit. The visualizer requests high
            reasoning effort. With a conversation ID, instructions and new input
            are sent separately and the API retains the earlier exchanges.
        """
        request = loader_request(prompt, history, contract)
        if name == 'visualizer':
            request['reasoning'] = {'effort': 'high'}
        if conversation:
            request.update(instructions=prompt, input=history.copy(), conversation=conversation)
        text, usage, status = self.call_loader(name, request, 8000,
                                               allow_user_questions=allow_user_questions)
        if status != 'completed':
            raise RuntimeError(f'{name} returned an incomplete response. See the saved response.')
        reply = contract.model_validate_json(text).model_dump()
        history.append({'role': 'assistant', 'content': json.dumps(reply)})
        return reply

    def count_input(self, request, agent='data_loader_agent'):
        """Count request tokens and record the budget evidence.

        Inputs: request: API payload; agent: label for the journal entry.
        Returns: Integer input-token count.
        Details: Uses cancellable analysis_request for analysis, otherwise the shared
            count_input helper; callers apply their own budgets.
        """
        if self.analysis:
            response = self.analysis_request(lambda client: client.post('/responses/input_tokens', body=request, cast_to=dict[str, Any]))
            result = response['input_tokens']
        else:
            result = count_input(request)
        self.record('token_count', agent, {'input_tokens': result})
        return result

    def answer(self, prompt, ready=False):
        """Pause the workflow until the browser supplies an answer.

        When: An agent asks a question, or initial ingestion waits for Prepare data.
        Inputs: prompt: waiting message; ready: use the preparation-ready state instead of a
            question state.
        Returns: Queued answer text, or variable-to-instruction dict for codebook review;
            cancellation raises InterruptedError.
        Flow: Saves the waiting state, polls the answer queue with stop checks, then logs the
            answer and clears the pending form.
        """
        with self.lock:
            self.state['status'] = 'ready' if ready else 'awaiting_user'
            self.state['pending'] = prompt
            self.save()
        self.record('waiting', 'workflow', prompt)
        while True:
            self.check_stop()
            try:
                answer = self.answers.get(timeout=1)
                break
            except queue.Empty:
                continue
        self.record('user_answer', 'user', answer)
        with self.lock:
            self.state.update(status='running', pending='', question_form=None)
            self.save()
        return answer

    def run_code(self, code, files, workspace, turn_dir, agent='data_loader_agent', read_only=False):
        """Run agent code in Docker and attach an error diagnosis when needed.

        When: Ingestion phases execute code; failures invoke the shared glitch_agent subagent.
        Inputs: code: Python source; files: upload map; workspace: work root; turn_dir: output
            directory; agent: requesting role; read_only: work mount mode.
        Returns: Success: execution result. Failure: exit_code, glitch_report and full_log; full
            output remains in the journal.
        Flow: Delegates execution to app.docker.run_code, records code/results, and sends
            numbered code plus a bounded error excerpt to glitch_agent.
        """
        self.check_stop()
        self.record('code', agent, {'code': code, 'turn_directory': str(turn_dir)})
        result = run_code(code, files, workspace, turn_dir, read_only=read_only)
        log = Path(result.get('full_log', ''))
        raw = log.read_text(encoding='utf-8', errors='replace') if log.is_file() else ''
        self.record('execution', 'docker', {**result, 'full_output': raw})
        self.message('execution', 'Code finished successfully.' if result.get('exit_code') == 0 else 'The code encountered an error.',
                     'thinking', {'code': code, 'result': result})
        if result.get('exit_code') != 0:
            numbered = '\n'.join(f'{i}: {line}' for i, line in enumerate(code.splitlines(), 1))
            # Keep the complete original error in logs; traceback tails keep agent context bounded.
            report = self.ask_agent('glitch_agent', GLITCH_PROMPT, [{'role': 'user', 'content': json.dumps({
                'requesting_agent': agent,
                'executed_code': numbered, 'error_output': raw[-24000:] or result,
                'output_was_excerpted': len(raw) > 24000})}], GlitchReply)
            return {'exit_code': result.get('exit_code'), 'glitch_report': report, 'full_log': result.get('full_log')}
        return result

    def progress(self, folder, number, used):
        """Forward the loader budget update to its owning phase.

        Inputs: folder: dataset; number: loader session number; used: consumed tokens.
        Returns: None; saves loader_budget in project state.
        Details: Compatibility method used by phase 2; delegates to
            ingest.data_loader.state.progress.
        """
        from v_5.ingest.data_loader.state import progress as update_state
        return update_state(self, folder, number, used)

    def phase_report(self, folder, payload):
        """Forward a preparation report to the loader state helper.

        Inputs: folder: dataset; payload: latest loader reply, including phase reports.
        Returns: None; records the report and saves it under preparations.
        Details: Used in phase 2. The implementation lives in
            ingest.data_loader.state.phase_report.
        """
        from v_5.ingest.data_loader.state import phase_report as update_state
        return update_state(self, folder, payload)

    def save_handoff(self, project_notes, folder_notes, files, findings, previews, user_clarifications, *, protected_folders=()):
        """Forward the completed material review to phase 1 persistence.

        Inputs: project_notes/folder_notes: decisions; files: upload metadata;
            findings/previews: review evidence; user_clarifications: answers; protected_folders:
            published datasets.
        Returns: None; updates project state and writes ingestion_handoff.json.
        Details: Delegates to initial_ingestion.state.save_handoff, which also removes confirmed
            unrelated uploads outside protected folders.
        """
        from v_5.ingest.initial_ingestion.state import save_handoff as update_state
        return update_state(self, project_notes, folder_notes, files, findings, previews, user_clarifications, protected_folders=protected_folders)
