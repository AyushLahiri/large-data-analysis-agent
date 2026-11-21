"""Current-turn recovery for non-PDF codebooks; conversations stay in API threads."""
import json
from pathlib import Path
import pandas as pd

from v_5.app.conversations import ConversationCalls
from v_5.ingest.codebook_ingestion.sessions import BUDGET
from v_5.ingest.codebook_ingestion.sub_agents.agent import CodebookTableReply
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import atomic_text, file_lock, read_json, save_json
from v_5.ingest.codebook_ingestion.tables import COLUMNS, normalize_rows
from v_5.ingest.manifest import work_path
from v_5.main_functions.agent import GLITCH_PROMPT, GlitchReply
from v_5.main_functions.session import loader_request

OUTPUT_LIMIT = 16000


class StructuredCalls(ConversationCalls):
    """Use existing thread transport and model settings for extraction and diagnosis."""

    def new_thread(self, role):
        """Return a conversation ID to save before sending its first input."""
        return super().new_thread()

    def count_input(self, request):
        """Count the pending input and remote history against the reader's budget."""
        return super().count_input(request, 'codebook_table_agent')

    def submit(self, role, request):
        """Submit a retrievable reply using the extraction or glitch progress label."""
        return super().submit(request, 'glitch_agent' if role == 'glitch' else 'codebook_table_agent')


class StructuredRunner:
    """Save the current input, reply, code result and diagnosis for one source."""

    def __init__(self, directory, workspace, initial_input, prompt, provider, execute,
                 *, observer=None, check_stop=None, hook=None):
        """Open one phase-4 codebook reader's checkpoint.

        Inputs: directory/workspace: saved outputs; initial_input: source and folder context;
            prompt/provider: agent settings and calls; execute: read-only sandbox callback;
            observer/check_stop/hook: UI, cancellation and fault-test callbacks.
        Returns: Runner. Existing turn files remain available to code that refers to them;
            flags and pending payloads are overwritten at each new turn.
        """
        self.root, self.workspace = Path(directory), Path(workspace)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'checkpoint.json'
        self.base, self.prompt, self.provider = initial_input, prompt, provider
        self.execute = execute
        self.observer = observer or (lambda event, state: None)
        self.check_stop = check_stop or (lambda: None)
        self.hook = hook or (lambda point, unit: None)
        self.state = read_json(self.path)
        if self.state is None:
            # Older unfinished runs can still refer to their saved code and output paths.
            turn = max((int(p.name[5:]) for p in self.root.glob('turn_[0-9][0-9][0-9][0-9]')), default=0)
            latest = read_json(self.root / 'latest_payload.json') if turn else None
            feedback = read_json(self.root / f'turn_{turn:04d}' / 'feedback.json', {}) if turn else {}
            self.state = dict(thread_id=None, turn=turn + 1,
                current_input={**self.base, 'latest_payload': latest, 'latest_result': feedback},
                input_sent=False, response_id=None, agent_returned=False, reply=None,
                code_ran=False, result=None, glitch=None, complete=False,
                used_tokens=0, reserved_tokens=0)

    def hit(self, point):
        """Expose a turn boundary to fault tests without extra recovery fields."""
        self.hook(point, str(self.state['turn']))

    def save(self, point):
        """Atomically replace current payloads and their success flags together."""
        self.hit('before_' + point)
        save_json(self.path, self.state)
        self.hit('after_' + point)

    def advance(self, feedback):
        """Save the next self-contained input and clear the previous turn's flags.

        Input: execution or validation feedback. Saved output paths let a fresh rollover
        thread continue; no conversation transcript is copied locally.
        """
        latest = self.state['reply'] or self.state['current_input'].get('latest_payload')
        self.state.update(current_input={**self.base, 'latest_payload': latest, 'latest_result': feedback},
            turn=self.state['turn'] + 1, input_sent=False, response_id=None,
            agent_returned=False, reply=None, code_ran=False, result=None, glitch=None,
            reserved_tokens=0)
        self.save('advance')

    def receive(self):
        """Retrieve a known reply, or submit pending input in its saved thread.

        input_sent is False before submission, None when receipt is unknown and True
        after the response ID is saved. Unknown retries include a short error note.
        Budget rollover replaces only the thread; the current input stays available.
        """
        state = self.state
        if not state['thread_id']:
            state['thread_id'] = self.provider.new_thread('codebook')
            self.save('thread')
        if state['input_sent'] is not True:
            messages = [{'role': 'user', 'content': json.dumps(state['current_input'], default=str)}]
            if state['input_sent'] is None:
                messages.insert(0, {'role': 'user', 'content': 'The app could not recover the previous response; retry the pending input below.'})
            request = loader_request(self.prompt, messages, CodebookTableReply)
            request.update(conversation=state['thread_id'], background=True, store=True,
                           max_output_tokens=OUTPUT_LIMIT)
            tokens = self.provider.count_input(request)
            if state['used_tokens'] + tokens + OUTPUT_LIMIT + 512 > BUDGET:
                if not state['used_tokens']:
                    raise RuntimeError('Codebook input exceeds the fresh-thread token budget.')
                state.update(thread_id=self.provider.new_thread('codebook'), used_tokens=0,
                             reserved_tokens=0, response_id=None)
                self.save('rollover')
                return
            state.update(input_sent=None, reserved_tokens=tokens + OUTPUT_LIMIT,
                         used_tokens=state['used_tokens'] + tokens + OUTPUT_LIMIT)
            self.save('submission_started')
            self.hit('before_submit')
            receipt = self.provider.submit('codebook', request)
            self.hit('after_submit')
            state.update(response_id=receipt['id'], input_sent=True)
            self.save('input_sent')
        try:
            response = self.provider.retrieve(state['response_id'])
        except getattr(self.provider, 'missing_response_error', ()):
            state.update(response_id=None, input_sent=None)
            self.save('expired_response')
            return
        self.hit('after_retrieve')
        usage = response.get('usage')
        if usage:
            state['used_tokens'] += usage['input_tokens'] + usage['output_tokens'] - state['reserved_tokens']
        state['reserved_tokens'] = 0
        try:
            if response['status'] != 'completed':
                raise ValueError('Incomplete extraction reply')
            reply = CodebookTableReply.model_validate_json(response['output_text']).model_dump()
        except ValueError as error:
            self.advance({'instruction': f'Your last response was incomplete or invalid and no code was executed: {str(error)[:1000]}. '
                                         'Return a smaller complete response.'})
            return
        state.update(reply=reply, agent_returned=True)
        self.save('reply')

    def execution_feedback(self):
        """Run code when its result is missing, then save before diagnosing errors.

        Returns: execution_result and saved_review_directory for the next agent input.
        Glitch retries use the same diagnosis thread and saved error; a saved diagnosis
        is reused. The caller keeps sources and prepared data read-only in the sandbox.
        """
        state = self.state
        current = self.root / f"turn_{state['turn']:04d}"
        current.mkdir(exist_ok=True)
        if not state['code_ran']:
            atomic_text(current / 'code.py', state['reply']['code'])
            self.hit('before_execute')
            result = self.execute(state['reply']['code'], current)
            self.hit('after_execute')
            state.update(result=result, code_ran=True)
            self.save('execution_result')
        result = state['result']
        self.observer('execution', state)
        if result.get('exit_code') != 0:
            retrying = state['glitch'] is not None
            if not state['glitch']:
                state['glitch'] = {'thread_id': self.provider.new_thread('glitch'), 'response': None}
                self.save('glitch_thread')
            glitch = state['glitch']
            if glitch['response'] is None:
                log_path = Path(result.get('full_log') or '')
                raw = log_path.read_text(encoding='utf-8', errors='replace') if log_path.is_file() else ''
                payload = {'requesting_agent': 'codebook_table_agent',
                    'executed_code': '\n'.join(f'{i}: {line}' for i, line in enumerate(state['reply']['code'].splitlines(), 1)),
                    'error_output': raw[-24000:] or result, 'output_was_excerpted': len(raw) > 24000}
                messages = [{'role': 'user', 'content': json.dumps(payload)}]
                if retrying:
                    messages.insert(0, {'role': 'user', 'content': 'The app has no saved diagnosis from the previous attempt; the code already ran, so diagnose the supplied error again.'})
                request = loader_request(GLITCH_PROMPT, messages, GlitchReply)
                request.update(conversation=glitch['thread_id'], background=True, store=True,
                               max_output_tokens=8000)
                self.hit('before_glitch_submit')
                receipt = self.provider.submit('glitch', request)
                self.hit('after_glitch_submit')
                response = self.provider.retrieve(receipt['id'])
                self.hit('after_glitch_retrieve')
                if response['status'] != 'completed':
                    raise RuntimeError('Glitch reply was incomplete; retry diagnosis in its thread.')
                glitch['response'] = GlitchReply.model_validate_json(response['output_text']).model_dump()
                self.save('glitch_response')
            self.observer('glitch', state)
            result = {'exit_code': result.get('exit_code'), 'glitch_report': glitch['response'],
                      'full_log': result.get('full_log')}
        return {'execution_result': result,
                'saved_review_directory': '/work/' + current.relative_to(self.workspace).as_posix()}

    def finish(self):
        """Validate the finish reply, then overwrite all three extraction outputs.

        Returns: normalized DataFrame, or None when the agent must repair its output.
        Export failures retain the finish reply and complete=False, so Retry rewrites
        CSV, notes and verification without another agent call.
        """
        reply = self.state['reply']
        try:
            frame = pd.read_csv(work_path(self.workspace, reply['table_path']), dtype=str, keep_default_na=False)
            if list(frame.columns) != COLUMNS:
                raise ValueError('Output CSV must contain exactly the specified columns in order.')
            frame = normalize_rows(frame.to_dict('records'))
            notes = work_path(self.workspace, reply['notes_path']).read_text(encoding='utf-8')
            verification = work_path(self.workspace, reply['verification_path']).read_text(encoding='utf-8')
        except (ValueError, OSError) as error:
            self.advance({'output_check_error': str(error), 'instruction': 'Fix the saved output, then finish again.'})
            return None
        # Cheap derived assets: a partial export is simply replaced on retry.
        atomic_text(self.root / 'normalized.csv', frame.to_csv(index=False))
        self.hit('after_normalized')
        atomic_text(self.root / 'general_information.md', notes)
        self.hit('after_notes')
        atomic_text(self.root / 'verification.txt', verification)
        self.hit('after_verification')
        self.state['complete'] = True
        self.save('complete')
        return frame

    def run(self):
        """Resume the unfinished stage and return the completed source table.

        Stop is checked after saving accepted replies. Missing derived exports are rebuilt
        from the saved finish reply; completed sources otherwise need no model calls.
        """
        with file_lock(self.root / 'structured.lock'):
            self.state = read_json(self.path, self.state)
            while True:
                self.check_stop()
                if self.state['complete']:
                    if all((self.root / name).is_file() for name in ('normalized.csv', 'general_information.md', 'verification.txt')):
                        return normalize_rows(pd.read_csv(self.root / 'normalized.csv', dtype=str, keep_default_na=False).to_dict('records'))
                    self.state['complete'] = False
                    self.save('rebuild_outputs')
                if not self.state['agent_returned']:
                    self.receive()
                    if not self.state['agent_returned']:
                        continue
                self.observer('reply', self.state)
                self.check_stop()
                if self.state['reply']['action'] == 'run':
                    self.advance(self.execution_feedback())
                elif self.state['reply']['action'] == 'stop':
                    raise ValueError(self.state['reply']['message'])
                else:
                    frame = self.finish()
                    if frame is not None:
                        return frame
