"""Shared API thread transport; workflows own their checkpoints and budgets."""
import time
from openai import BadRequestError, NotFoundError, OpenAI
from v_5.app.config import api_key


class ConversationCalls:
    """Store conversations remotely and retrieve known background responses on retry."""

    missing_response_error = NotFoundError

    def __init__(self, session, *, client=None):
        """Connect cancellation and token counting to the API without automatic submissions.

        Inputs: session: app services; client: optional API-compatible test client.
        Unknown submissions are handled by the workflow, so SDK retries are disabled.
        """
        self.session = session
        self.client = client or OpenAI(api_key=api_key(), max_retries=0, timeout=45)

    def check_stop(self):
        """Stop local work when the user cancels; saved API responses remain retrievable."""
        self.session.check_stop()

    def new_thread(self):
        """Create one API conversation and return its ID; no local chat copy is made."""
        self.check_stop()
        return self.client.conversations.create().id

    def count_input(self, request, name):
        """Count this input including its API conversation before reserving the budget.

        Inputs: request: pending call; name: progress label. Returns: input token count.
        Conversation item IDs are counted as references, without attaching the counting
        request to the live conversation. This avoids competing with its background work.
        No message text or local chat file is needed; pagination includes every item.
        """
        self.check_stop()
        body = {key: value for key, value in request.items()
                if key not in {'background', 'store', 'max_output_tokens', 'conversation'}}
        references = []
        for item in self.client.conversations.items.list(request['conversation'], order='asc', limit=100):
            self.check_stop()
            references.append({'type': 'item_reference', 'id': item.id})
        body['input'] = [*references, *request['input']]
        return self.session.count_input(body, agent=name)

    def submit(self, request, name):
        """Submit the current input and return the API's response ID and status.

        A prior unknown submission may briefly lock the same conversation. Only that
        explicit rejection is retried here, for at most 30 seconds; other errors surface.
        """
        for attempt in range(16):
            self.check_stop()
            try:
                response = self.client.responses.create(**request)
                return {'id': response.id, 'status': response.status}
            except BadRequestError as error:
                if error.code != 'conversation_locked' or attempt == 15:
                    raise
                if attempt == 0:
                    self.session.message(name, 'The previous request is still finishing. I’ll retry this request shortly.', 'thinking')
                time.sleep(2)

    def retrieve(self, response_id):
        """Wait for a known response without generating another answer.

        Inputs: response_id: saved submission receipt. Returns: terminal status, text and
            token usage. Ingestion lets an accepted request finish and save its reply before
            honoring Stop at the next workflow step. Timeouts retain the ID for retry.
        """
        deadline = time.monotonic() + 240
        while True:
            if getattr(self.session, 'analysis', False):
                self.check_stop()
            response = self.client.responses.retrieve(response_id)
            if response.status not in ('queued', 'in_progress'):
                add_cost = getattr(self.session, 'add_cost', None)
                if add_cost:  # Test sessions without pricing skip it.
                    add_cost(response.model, response.usage.model_dump() if response.usage else None, response.id)
                return {'id': response.id, 'status': response.status,
                        'output_text': response.output_text,
                        'usage': response.usage.model_dump() if response.usage else None}
            if time.monotonic() >= deadline:
                raise TimeoutError('The response is still pending. Retry preparation to continue the saved request.')
            time.sleep(1)

