"""Current-cycle loader recovery, shared by the app and its failure-test notebook."""
import json
from pathlib import Path
from v_5.ingest.data_loader.agent import LOADER_PROMPT, LoaderReply
from v_5.ingest.data_loader.sub_agents.rollover import summarize_loader_history
from v_5.ingest.manifest import validate_prepared_manifest
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import atomic_text, file_lock, read_json, save_json
from v_5.main_functions.agent import GLITCH_PROMPT, GlitchReply
from v_5.main_functions.session import loader_request

SESSION_TOKENS, OUTPUT_TOKENS = 250_000, 8_000


def fresh_loader(context):
    """Create one loader's current-cycle state from the saved ingestion output.

    Returns: Delivery flags, current payloads and the counters used by rollover.
    Details: A glitch response's presence is its success marker; no extra flag is needed.
    """
    return dict(thread_id=None, input_sent=False, response_id=None,
                loader_returned=False, reply=None, code_ran=False, result=None,
                glitch=None, current_input=context, cycle=1, session_number=1,
                used_tokens=0, reserved_tokens=0, attempted_investigation=False)


class LoaderRunner:
    """Resume a folder from its saved inputs, execution result and remote agent threads."""

    def __init__(self, root, provider, execute, ingest, review, answer, hook=None, *,
                 prompt=LOADER_PROMPT, observer=None, check_stop=None):
        """Open the current checkpoint and supply the same external boundaries as the app.

        Inputs: root: workspace; provider: thread/response API; execute: code runner;
            ingest/review/answer: phase and user boundaries; hook: test interruption;
            prompt: loader instructions and packages; observer/check_stop: UI services.
        Returns: A runner that can be recreated after process death.
        """
        self.root, self.provider, self.execute = Path(root), provider, execute
        self.ingest, self.review, self.answer = ingest, review, answer
        self.hook = hook or (lambda point, unit: None)
        self.prompt = prompt
        self.observer = observer or (lambda event, state: None)
        self.check_stop = check_stop or (lambda: None)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'checkpoint.json'
        self.state = read_json(self.path, {
            'phase': 'ingestion', 'ingestion_final_output_created': False,
            'context': None, 'loader': None, 'rollover_summary': None})

    def save(self, point):
        """Replace payloads and their flags together, then expose a test boundary."""
        self.hook('before_' + point, self.unit)
        save_json(self.path, self.state)
        self.hook('after_' + point, self.unit)

    @property
    def unit(self):
        """Name the current iteration for the test injector; it is not saved metadata."""
        return str((self.state.get('loader') or {}).get('cycle', 0))

    def advance(self, payload):
        """Replace the completed cycle with its feedback and clear only transient work.

        Inputs: payload: execution result, answer or repair instruction for the next turn.
        Details: Keep the latest loader reply for rollover; its returned flag is now false.
        """
        loader = self.state['loader']
        loader.update(current_input=payload, input_sent=False, response_id=None,
                      loader_returned=False, code_ran=False, result=None, glitch=None,
                      reserved_tokens=0, cycle=loader['cycle'] + 1)
        self.state['phase'] = 'loader'
        self.save('advance')

    def request(self):
        """Build the real loader request, attaching only this turn to its remote thread."""
        loader = self.state['loader']
        allowance = (f"Session {loader['session_number']}: "
                     f"{SESSION_TOKENS - loader['used_tokens']} tokens remain, input + output "
                     "including reasoning. Continue the next necessary step.")
        request = loader_request(self.prompt, [
            {'role': 'user', 'content': json.dumps(loader['current_input'])},
            {'role': 'user', 'content': allowance}], LoaderReply)
        # An unsettled reservation identifies a prior attempt without adding a retry flag.
        if loader['reserved_tokens']:
            request['input'].insert(1, {'role': 'user', 'content': 'The app could not recover the previous response; retry the pending input below.'})
        request.update(conversation=loader['thread_id'], background=True,
                       store=True, max_output_tokens=OUTPUT_TOKENS)
        return request

    def receive_loader(self):
        """Resume submission/retrieval, validate the reply, and account for its usage once.

        When: Phase 2, before code, clarification or finish is handled.
        Details: Unknown submission retries the same thread. A known response is retrieved.
            Incomplete/invalid replies request a replacement and never execute partial code.
        """
        loader = self.state['loader']
        if not loader['thread_id']:
            loader['thread_id'] = self.provider.new_thread('loader')
            self.save('loader_thread')
        if not loader['input_sent']:
            request = self.request()
            count = self.provider.count_input(request)
            if count + OUTPUT_TOKENS > SESSION_TOKENS - loader['used_tokens']:
                if not loader['used_tokens']:
                    raise RuntimeError('The handoff cannot fit a fresh loader session.')
                self.state.update(phase='rollover', rollover_summary=None)
                self.save('rollover_start')
                return
            # Unknown submissions keep this conservative charge; known replies replace it.
            loader['reserved_tokens'] = count + OUTPUT_TOKENS
            loader['used_tokens'] += loader['reserved_tokens']
            self.save('reservation')
            self.hook('before_loader_submit', self.unit)
            receipt = self.provider.submit('loader', request)
            self.hook('after_loader_submit', self.unit)
            loader.update(response_id=receipt['id'], input_sent=True)
            self.save('input_sent')
        if not loader['loader_returned']:
            self.hook('before_loader_retrieve', self.unit)
            try:
                response = self.provider.retrieve(loader['response_id'])
            except getattr(self.provider, 'missing_response_error', ()):
                # An expired receipt cannot be retrieved; repeat only this pending input.
                loader.update(input_sent=False, response_id=None)
                self.save('expired_response')
                return
            self.hook('after_loader_retrieve', self.unit)
            usage = response['usage']
            if usage:
                loader['used_tokens'] += usage['input_tokens'] + usage['output_tokens'] - loader['reserved_tokens']
            loader['reserved_tokens'] = 0
            try:
                if response['status'] != 'completed':
                    raise ValueError('Incomplete response')
                reply = LoaderReply.model_validate_json(response['output_text']).model_dump()
            except ValueError as error:
                self.advance({'instruction': f'Your last response was incomplete or invalid and no code was executed: {str(error)[:1000]}. '
                                             'Return a smaller complete response.'})
                return
            loader.update(reply=reply, loader_returned=True)
            self.save('loader_reply')

    def execution_feedback(self):
        """Save code's result before requesting a glitch diagnosis, then build app feedback.

        When: Phase 2 action=run. Inputs: Saved LoaderReply and its current code/result.
        Returns: execution_result in the production success or glitch-report shape.
        Details: A failed script still sets code_ran. An absent glitch reply retries the
            complete diagnosis in its saved thread; a saved reply is reused immediately.
        """
        loader = self.state['loader']
        current = self.root / 'current'
        current.mkdir(exist_ok=True)
        if not loader['code_ran']:
            self.hook('before_code_save', self.unit)
            atomic_text(current / 'code.py', loader['reply']['code'])
            self.hook('after_code_save', self.unit)
            self.hook('before_execute', self.unit)
            result = self.execute((current / 'code.py').read_text(encoding='utf-8'), self.root, current)
            self.hook('after_execute', self.unit)
            # One durable write includes the error, if any, AND the completed-execution flag.
            loader.update(result=result, code_ran=True, attempted_investigation=True)
            self.save('execution_result')
        result = loader['result']
        self.observer('execution', self.state)
        if result.get('exit_code') != 0:
            retrying_glitch = loader['glitch'] is not None
            if not loader['glitch']:
                loader['glitch'] = {'thread_id': self.provider.new_thread('glitch'), 'response': None}
                self.save('glitch_thread')
            glitch = loader['glitch']
            if glitch['response'] is None:
                log_path = Path(result.get('full_log', ''))
                log = log_path.read_text(encoding='utf-8', errors='replace') if log_path.is_file() else ''
                payload = {
                    'requesting_agent': 'data_loader_agent',
                    'executed_code': '\n'.join(f'{i}: {line}' for i, line in enumerate(loader['reply']['code'].splitlines(), 1)),
                    'error_output': log[-24000:] or result, 'output_was_excerpted': len(log) > 24000}
                request = loader_request(GLITCH_PROMPT, [{'role': 'user', 'content': json.dumps(payload)}], GlitchReply)
                if retrying_glitch:
                    request['input'].insert(1, {'role': 'user', 'content': 'The app has no saved diagnosis from the previous attempt; the code already ran, so diagnose the supplied error again.'})
                request.update(conversation=glitch['thread_id'], background=True,
                               store=True, max_output_tokens=OUTPUT_TOKENS)
                self.hook('before_glitch_submit', self.unit)
                receipt = self.provider.submit('glitch', request)
                self.hook('after_glitch_submit', self.unit)
                response = self.provider.retrieve(receipt['id'])
                self.hook('after_glitch_retrieve', self.unit)
                if response['status'] != 'completed':
                    raise RuntimeError('Glitch reply was incomplete; retry diagnosis in its thread.')
                glitch['response'] = GlitchReply.model_validate_json(response['output_text']).model_dump()
                self.save('glitch_response')
            self.observer('glitch', self.state)
            result = {'exit_code': result.get('exit_code'), 'glitch_report': glitch['response'],
                      'full_log': result.get('full_log')}
        # Current execution files overwrite; prepared tables and reusable loading code remain.
        return {'execution_result': {**result, 'code_path': '/work/current/code.py',
                                     'result_path': '/work/checkpoint.json'}}

    def rollover(self):
        """Use the actual chunked summarizer, save its final note, and replace the thread.

        Inputs: Remote conversation, latest reply and still-pending feedback.
        Details: Only the final summary is checkpointed. The ordinary input_sent flag handles
            delivery to the replacement thread; a separate ingestion flag is unnecessary.
        """
        loader = self.state['loader']
        if self.state['rollover_summary'] is None:
            owner = self

            class SummarySession:
                def call_loader(self, name, request, allow_user_questions=False):
                    """Adapt the app summarizer's call to the prototype transport."""
                    owner.hook('before_summary_submit', owner.unit)
                    receipt = owner.provider.submit('rollover', request)
                    owner.hook('after_summary_submit', owner.unit)
                    response = owner.provider.retrieve(receipt['id'])
                    owner.hook('after_summary_retrieve', owner.unit)
                    return response['output_text'], response['usage'], response['status']

            history = self.provider.messages(loader['thread_id'])
            summary = summarize_loader_history(SummarySession(), history, loader['reply'],
                                               self.root, loader['session_number'], save_chunks=False)
            self.state['rollover_summary'] = summary
            self.save('rollover_summary')
        self.hook('before_rollover_thread', self.unit)
        thread = self.provider.new_thread('loader')
        self.hook('after_rollover_thread', self.unit)
        handoff = {'original_context': self.state['context'],
                   'handoff': self.state['rollover_summary'], 'latest_payload': loader['reply'],
                   'pending_input': loader['current_input'],
                   'instruction': 'Continue this work. Reuse saved results and code in /work; do not repeat resolved investigations or reset completed phases. Preserve their evidence, decisions and findings.'}
        loader.update(thread_id=thread, current_input=handoff, used_tokens=0,
                      reserved_tokens=0, session_number=loader['session_number'] + 1,
                      input_sent=False, response_id=None, loader_returned=False)
        self.state.update(phase='loader', rollover_summary=None)
        self.save('rollover_handoff')

    def run(self):
        """Continue the saved folder with one writer, even across app processes.

        Returns: Final checkpoint after confirmation or stop. The caller handles codebooks
            and publication; prepared artifacts are validated before that handoff.
        """
        with file_lock(self.root / 'loader.lock'):
            self.state = read_json(self.path, self.state)
            return self._run()

    def _run(self):
        """Apply the next unsatisfied flag, overwriting each completed cycle's state."""
        if not self.state['ingestion_final_output_created']:
            self.hook('before_ingestion', self.unit)
            context = self.ingest()
            self.hook('after_ingestion', self.unit)
            self.state.update(context=context, ingestion_final_output_created=True,
                              loader=fresh_loader(context), phase='loader')
            self.save('ingestion_output')
        while True:
            self.check_stop()
            if self.state['phase'] in ('complete', 'stopped'):
                return self.state
            if self.state['phase'] == 'rollover':
                self.rollover()
                continue
            loader = self.state['loader']
            if not loader['loader_returned']:
                self.receive_loader()
                if self.state['phase'] == 'rollover' or not loader['loader_returned']:
                    continue
            reply = loader['reply']
            self.observer('reply', self.state)
            self.check_stop()  # The reply is saved; stop before executing its next action.
            if reply['action'] == 'run':
                self.advance(self.execution_feedback())
            elif reply['action'] == 'clarify':
                if loader['attempted_investigation']:
                    feedback = {'question': reply['question'], 'user_answer': self.answer(reply['question'])}
                    self.hook('after_answer', self.unit)
                else:
                    feedback = {'instruction': 'No question was shown. Investigate the available files with code before asking the user.'}
                self.advance(feedback)
            elif reply['action'] == 'stop':
                self.state['phase'] = 'stopped'
                self.save('stop')
            else:
                try:
                    if reply['phases']['loading_artefacts']['completed']:
                        validate_prepared_manifest(self.root, reply['manifest_path'])
                except (OSError, ValueError) as error:
                    reply['phases']['loading_artefacts'].update(completed=False, details=str(error))
                    self.observer('manifest_error', self.state)
                    self.advance({'manifest_error': str(error), 'instruction': 'Correct the manifest to match the saved Parquets, then finish again.'})
                    continue
                if not all(phase['completed'] for phase in reply['phases'].values()):
                    self.advance({'instruction': 'Finish requires all six phases to be complete, with evidence and saved loading artefacts. Continue the outstanding work, or stop if it cannot be completed reliably.'})
                    continue
                self.state['phase'] = 'review'
                self.save('review_ready')
                self.hook('before_review', self.unit)
                review = self.review(reply)
                self.hook('after_review', self.unit)
                if review['action'] == 'handoff':
                    self.advance({'descriptives_handoff': review,
                                  'instruction': 'Continue this loader conversation. Investigate this exact concern, correct affected prepared outputs in place if warranted, and finish for another user review.'})
                else:
                    self.state['phase'] = 'complete' if review['action'] == 'confirm' else 'stopped'
                    self.save('complete')
