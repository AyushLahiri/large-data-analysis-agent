"""PDF progress and settings over the shared resumable API transport."""
from v_5.app.config import CODEBOOK_PDF_MODEL
from v_5.app.conversations import ConversationCalls


class PdfCalls(ConversationCalls):
    """Keep the existing PDF interface and page progress messages."""

    def __init__(self, session, *, client=None, model=CODEBOOK_PDF_MODEL, output_limit=16000):
        """Use the shared transport with the PDF worker's existing model and output limit."""
        super().__init__(session, client=client)
        self.model, self.output_limit = model, output_limit

    def progress(self, name, payload, result, usage):
        """Show completed PDF work without writing another copy of its conversation.

        Inputs: agent label, current payload, validated output and token usage.
        Returns: None; preserves the app's page/merge progress messages and issue display.
        """
        page = payload.get('current_page', {}).get('page')
        message = (f"Page {page}: extracted {len(result['rows'])} variable rows and "
                   f"{len(result['general_information'])} general-information entries."
                   if page else f"Combined definitions for {len(result['rows'])} variables.")
        observations = list(result.get('issues', []))
        if usage:
            observations.insert(0, f"Input tokens: {usage['input_tokens']:,}; output tokens: {usage['output_tokens']:,} (includes reasoning).")
        self.session.message(name, message, 'thinking', {'observations': observations})

    def rejected(self, name, problem, attempt):
        """Show that a reply was rejected and is being asked again with its problem."""
        self.session.message(name, f'Reply rejected; asking again (correction {attempt}).', 'thinking',
                             {'observations': [problem]})
