"""One overwritten checkpoint for final codebook batches and soft-name reconciliation."""
from copy import deepcopy
import json
from pathlib import Path

from v_5.app.conversations import ConversationCalls
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json, save_json
from v_5.main_functions.session import loader_request


class ReconciliationCheckpoint:
    """Keep accepted rows plus only the current agent's input, receipt and output."""

    def __init__(self, directory, session, *, provider=None, hook=None):
        """Open phase-4 progress in the existing codebook directory.

        Inputs: directory: output root; session: app services; provider/hook: optional test
            transport and interruption callback. Returns: checkpoint owner.
        The cursor identifies the next 50-row batch. Accepted definitions, taxonomy and
        that cursor share one atomic save; remote threads retain conversation history.
        """
        self.path = Path(directory) / 'reconciliation_checkpoint.json'
        self.session, self.provider = session, provider
        self.hook = hook or (lambda point, state: None)
        self.state = read_json(self.path, dict(phase='matched', start=0, matched={}, unmatched={},
            taxonomy={}, recent=[], annotations={}, agent=None))

    def save(self, point):
        """Replace progress and its payloads together; expose write boundaries to tests."""
        self.hook('before_' + point, self.state)
        save_json(self.path, self.state)
        self.hook('after_' + point, self.state)

    def next_input(self, payload, *, rejected=False):
        """Keep this agent's thread while replacing its finished exchange with new input.

        Input: next lookahead, correction or soft-match decision payload. Returns: rejection
        count for the existing three-attempt validation limit. No previous replies are kept.
        """
        agent = self.state['agent']
        if agent is None:
            return 0
        rejected_count = agent['rejections'] + 1 if rejected else agent['rejections']
        agent.update(input={**agent['input'], **payload}, input_sent=False, response_id=None,
                     output_received=False, output=None, reserved_tokens=0,
                     rejections=0 if rejected_count >= 3 else rejected_count)
        self.save('next_input')
        return rejected_count

    def call(self, name, prompt, payload, contract, counter, *, output_limit, model=None):
        """Resume one agent exchange using input_sent and output_received.

        Inputs: existing role/prompt/payload/contract, budget counter and output cap.
        Returns: validated reply. A known response ID is retrieved; an unknown submission
        repeats only this input in the same thread with a short retry note. Stop is honored
        after an accepted output is saved. The normal 250k per-agent budget still applies.
        An invalid or incomplete reply, or an expired response, is asked again in the same
        thread with the exact problem; the third rejection stops the run.
        """
        from v_5.ingest.codebook_ingestion.sessions import BUDGET
        check_stop = getattr(self.session, 'check_stop', lambda: None)
        check_stop()
        agent = self.state['agent']
        if agent is None:
            agent = dict(thread_id=None, input=deepcopy(payload), input_sent=False, response_id=None,
                         output_received=False, output=None, used_tokens=0, reserved_tokens=0,
                         rejections=0)
            self.state['agent'] = agent
        if agent['output_received']:
            return deepcopy(agent['output'])
        if self.provider is None:
            self.provider = ConversationCalls(self.session)
        if not agent['thread_id']:
            agent['thread_id'] = self.provider.new_thread()
            self.save('agent')
        while True:
            if agent['input_sent'] is not True:
                messages = [{'role': 'user', 'content': json.dumps(agent['input'], ensure_ascii=False, default=str)}]
                if agent['input_sent'] is None:
                    messages.insert(0, {'role': 'user', 'content': 'The app could not recover the previous response; retry the pending input below.'})
                request = loader_request(prompt, messages, contract)
                if model:
                    request.update(model=model, reasoning={'effort': 'high' if model == 'gpt-6-luna' else 'medium'})
                request.update(conversation=agent['thread_id'], background=True, store=True,
                               max_output_tokens=output_limit)
                count = self.provider.count_input(request, name)
                if agent['used_tokens'] + count + output_limit + 512 > BUDGET:
                    raise RuntimeError(f'{name}: the next call would exceed the {BUDGET:,}-token session budget. Saved work is retained.')
                request['input'].append({'role': 'user', 'content':
                    f"Thread {counter['thread']}: {BUDGET-agent['used_tokens']-count} tokens remain after this input; output includes reasoning."})
                agent.update(input_sent=None, reserved_tokens=count + output_limit,
                             used_tokens=agent['used_tokens'] + count + output_limit)
                self.save('submission_started')
                receipt = self.provider.submit(request, name)
                self.hook('after_submit', self.state)
                agent.update(input_sent=True, response_id=receipt['id'])
                self.save('input_sent')
            try:
                response = self.provider.retrieve(agent['response_id'])
            except getattr(self.provider, 'missing_response_error', ()):
                # An expired response is sent again with the recovery note, within the same attempt limit.
                agent.update(input_sent=None, response_id=None, rejections=agent['rejections'] + 1)
                self.save('expired_response')
                if agent['rejections'] >= 3:
                    agent['rejections'] = 0
                    self.save('expired_response')
                    raise RuntimeError('The saved response expired three times; retry this batch in its existing thread.')
                continue
            self.hook('after_retrieve', self.state)
            usage = response.get('usage')
            if usage:
                agent['used_tokens'] += usage['input_tokens'] + usage['output_tokens'] - agent['reserved_tokens']
            agent['reserved_tokens'] = 0
            counter['used'] = agent['used_tokens']
            try:
                if response['status'] != 'completed':
                    raise ValueError(f"the reply ended with status {response['status']} before it was complete, "
                                     'often at the output limit. Return a complete, more concise reply')
                output = contract.model_validate_json(response['output_text']).model_dump()
            except ValueError as error:
                problem = str(error)[:1000]
                rejected = self.next_input({'feedback': f'Your last response was rejected: {problem}. '
                                            'Return a complete response for the pending input.'}, rejected=True)
                if rejected >= 3:
                    raise ValueError(f'{name}: the reply was rejected {rejected} times. Last problem: {problem}') from error
                self.session.message(name, f'Reply rejected; asking again (correction {rejected}).', 'thinking',
                                     {'observations': [problem]})
                agent = self.state['agent']
                continue
            break
        agent.update(output=output, output_received=True)
        self.save('output')
        check_stop()
        return deepcopy(output)
