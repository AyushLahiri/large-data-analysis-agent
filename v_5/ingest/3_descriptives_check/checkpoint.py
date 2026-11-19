"""Resume the current descriptives turn without archiving its conversation locally."""
import json
from pathlib import Path

from v_5.app.conversations import ConversationCalls
from v_5.ingest.descriptives_check.agent import DescriptivesReply
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import atomic_text, file_lock, read_json, save_json
from v_5.main_functions.agent import GLITCH_PROMPT, GlitchReply
from v_5.main_functions.session import loader_request


class DescriptivesCalls(ConversationCalls):
    """Use the existing API transport for the review and its glitch agent."""

    def new_thread(self, role):
        """Create one remote conversation; its ID is saved before any agent request."""
        return super().new_thread()

    def submit(self, role, request):
        """Submit the existing Sol/medium contract through a retrievable background call."""
        name = 'glitch_agent' if role == 'glitch' else 'descriptives_agent'
        with self.session.lock:
            self.session.state['activity'] = ('Diagnosing a review error' if role == 'glitch'
                                               else 'Checking the prepared data with you')
            self.session.save()
        return super().submit(request, name)


class DescriptivesRunner:
    """Track only the current reply, execution result, diagnosis and pending feedback."""

    def __init__(self, directory, workspace, initial_input, prompt, provider, execute, answer,
                 *, observer=None, check_stop=None, hook=None):
        """Open the stable review checkpoint for one completed loader iteration.

        Inputs: directory: review/evidence root; workspace: prepared data root; initial_input:
            folder and loader reports; prompt/provider: agent settings and API; execute/answer:
            sandbox and user services; observer/check_stop/hook: UI, cancellation and tests.
        Returns: Runner. The same review directory is reused after restart; a loader
            correction produces a new loader iteration and therefore a new review directory.
        """
        self.root, self.workspace = Path(directory), Path(workspace)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'checkpoint.json'
        self.provider, self.prompt = provider, prompt
        self.execute, self.answer = execute, answer
        self.observer = observer or (lambda event, state: None)
        self.check_stop = check_stop or (lambda: None)
        self.hook = hook or (lambda point, unit: None)
        self.state = read_json(self.path, dict(
            thread_id=None, cycle=1, current_input=initial_input,
            input_sent=False, response_id=None, agent_returned=False, reply=None,
            code_ran=False, result=None, glitch=None, checked_outputs=False))

    def hit(self, point):
        """Expose a current-cycle boundary for fault tests; no extra recovery data is saved."""
        self.hook(point, str(self.state['cycle']))

    def save(self, point):
        """Replace current payloads and their success flags in one atomic JSON write."""
        self.hit('before_' + point)
        save_json(self.path, self.state)
        self.hit('after_' + point)

    def advance(self, feedback):
        """Save the next input and cycle together, then discard the previous turn's flags."""
        self.state.update(current_input=feedback, cycle=self.state['cycle'] + 1,
                          input_sent=False, response_id=None, agent_returned=False,
                          reply=None, code_ran=False, result=None, glitch=None)
        self.save('advance')

    def receive(self):
        """Submit an unsent input or retrieve its known response in the same review thread.

        Details: input_sent is False before an attempt, None while its receipt is unknown,
        and True once the response ID is saved. Only unknown/expired retries get the short
        retry note. A validated reply and agent_returned are saved together.
        """
        state = self.state
        if not state['thread_id']:
            state['thread_id'] = self.provider.new_thread('descriptives')
            self.save('thread')
        if state['input_sent'] is not True:
            messages = [{'role': 'user', 'content': json.dumps(state['current_input'], default=str)}]
            if state['input_sent'] is None:
                messages.insert(0, {'role': 'user', 'content': 'The app could not recover the previous response; retry the pending input below.'})
            request = loader_request(self.prompt, messages, DescriptivesReply)
            request.update(conversation=state['thread_id'], background=True, store=True, max_output_tokens=8000)
            # None covers a lost submission receipt without a separate retry flag.
            state['input_sent'] = None
            self.save('submission_started')
            self.hit('before_submit')
            receipt = self.provider.submit('descriptives', request)
            self.hit('after_submit')
            state.update(response_id=receipt['id'], input_sent=True)
            self.save('input_sent')
        self.hit('before_retrieve')
        try:
            response = self.provider.retrieve(state['response_id'])
        except getattr(self.provider, 'missing_response_error', ()):
            state.update(response_id=None, input_sent=None)
            self.save('expired_response')
            return
        self.hit('after_retrieve')
        try:
            if response['status'] != 'completed':
                raise ValueError('Incomplete review response')
            reply = DescriptivesReply.model_validate_json(response['output_text']).model_dump()
        except ValueError:
            self.advance({'instruction': 'Your last response was incomplete or invalid and no code was executed. Return a smaller complete response.'})
            return
        state.update(reply=reply, agent_returned=True)
        self.save('reply')

    def execution_feedback(self):
        """Save a completed check before asking glitch to diagnose an execution error.

        Inputs: Current saved code and result. Returns: The existing execution_result and
        saved_review_directory payload. A saved diagnosis is reused; an absent diagnosis
        retries in its saved thread. Prepared data remains read-only in the caller's sandbox.
        """
        state = self.state
        current = self.root / 'current'
        current.mkdir(exist_ok=True)
        if not state['code_ran']:
            atomic_text(current / 'code.py', state['reply']['code'])
            self.hit('before_execute')
            result = self.execute(state['reply']['code'], current)
            self.hit('after_execute')
            state.update(result=result, code_ran=True,
                         checked_outputs=state['checked_outputs'] or result.get('exit_code') == 0)
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
                log_path = Path(result.get('full_log', ''))
                raw = log_path.read_text(encoding='utf-8', errors='replace') if log_path.is_file() else ''
                payload = {'requesting_agent': 'descriptives_agent',
                           'executed_code': '\n'.join(f'{i}: {line}' for i, line in enumerate(state['reply']['code'].splitlines(), 1)),
                           'error_output': raw[-24000:] or result, 'output_was_excerpted': len(raw) > 24000}
                messages = [{'role': 'user', 'content': json.dumps(payload)}]
                if retrying:
                    messages.insert(0, {'role': 'user', 'content': 'The app has no saved diagnosis from the previous attempt; the code already ran, so diagnose the supplied error again.'})
                request = loader_request(GLITCH_PROMPT, messages, GlitchReply)
                request.update(conversation=glitch['thread_id'], background=True, store=True, max_output_tokens=8000)
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

    def run(self):
        """Continue checks and discussion until the saved reply confirms, hands off or stops.

        Returns: DescriptivesReply dict. A saved terminal reply is the completion marker:
        retrying after its return does not repeat model calls, checks or user questions.
        """
        with file_lock(self.root / 'review.lock'):
            self.state = read_json(self.path, self.state)
            while True:
                self.check_stop()
                if not self.state['agent_returned']:
                    self.receive()
                    if not self.state['agent_returned']:
                        continue
                reply = self.state['reply']
                self.observer('reply', self.state)
                self.check_stop()  # Preserve the received reply before stopping its action.
                if reply['action'] == 'run':
                    self.advance(self.execution_feedback())
                elif reply['action'] == 'clarify':
                    if self.state['checked_outputs']:
                        feedback = {'question': reply['question'], 'user_answer': self.answer(reply['question'])}
                        self.hit('after_answer')
                    else:
                        feedback = {'instruction': 'No question was shown. Run the prepared-data checks before asking the user.'}
                    self.advance(feedback)
                else:
                    self.hit('before_return')
                    return reply
