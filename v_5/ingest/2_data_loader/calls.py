"""Loader, glitch and rollover calls using API conversations, without local chat archives."""
from v_5.app.conversations import ConversationCalls


class LoaderCalls(ConversationCalls):
    """Adapt shared transport to the loader runner's three agent roles."""

    NAMES = {'loader': 'data_loader_agent', 'glitch': 'glitch_agent',
             'rollover': 'loader_summarizer_agent'}

    def new_thread(self, role):
        """Create the loader or its glitch agent's persistent conversation ID."""
        return super().new_thread()

    def count_input(self, request):
        """Count the pending loader input and all remote history before budgeting."""
        return super().count_input(request, self.NAMES['loader'])

    def submit(self, role, request):
        """Submit one agent request, preserving its model, effort, schema and output limit."""
        # Summaries use the same background transport; a long generation must not hold
        # the short submission connection open until it finishes.
        request = {**request, 'background': True, 'store': True}
        with self.session.lock:
            self.session.state['activity'] = ('Summarizing loader progress' if role == 'rollover'
                                              else 'Diagnosing a code error' if role == 'glitch'
                                              else 'Preparing your data')
            self.session.save()
        return super().submit(request, self.NAMES[role])

    def messages(self, thread):
        """Read public conversation messages for the chunked rollover summarizer.

        Inputs: thread: current loader conversation ID. Returns: chronological role/text
        messages in memory. Reasoning items and repeated system instructions are excluded;
        no local transcript is written, and SDK pagination includes all messages.
        """
        history = []
        for item in self.client.conversations.items.list(thread, order='asc', limit=100):
            self.check_stop()
            value = item.model_dump()
            if value.get('type') != 'message' or value.get('role') not in ('user', 'assistant'):
                continue
            content = value.get('content', [])
            text = content if isinstance(content, str) else '\n'.join(
                part.get('text') or part.get('refusal') or '' for part in content)
            if text:
                history.append({'role': value['role'], 'content': text})
        return history
