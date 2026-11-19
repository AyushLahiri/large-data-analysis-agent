


MAX_ROWS, MAX_COLUMNS = 50, 10


FRAME_ROWS = 250_000


class ResultLimit(ValueError):
    def __init__(self, rows, columns):
        """Describe a result that is too large for chat.

        Inputs: rows/columns: actual result dimensions.
        Returns: None; sets shape and the user-facing exception message.
        Details: Raised by SQL/Python and table validators; analyse uses shape for logging and
            asks the user to narrow the result.
        """
        self.shape = {'rows': rows, 'columns': columns}
        super().__init__(f'The answer would return {rows:,} rows × {columns:,} columns. Chat can show at most 50 rows and 10 columns. Try narrowing the groups or variables.')


class JoinCardinalityError(ValueError):
    pass


def result_table(columns, rows):
    """Check a result fits the chat table limits.

    Inputs: columns: headers; rows: sequences of cells in header order.
    Returns: Dict containing the same columns and rows; raises for excess size or uneven row
        widths.
    Details: Shared by SQL execution, Python output parsing and analyst answer validation.
    """
    if len(columns) > MAX_COLUMNS or len(rows) > MAX_ROWS:
        raise ResultLimit(len(rows), len(columns))
    if any(len(row) != len(columns) for row in rows):
        raise ValueError('Each result row must have the same number of cells as the header.')
    return {'columns': columns, 'rows': rows}


def progress(session, title, text, **details):
    """Publish a concise analysis progress update.

    Inputs: session: current chat; title: activity label; text: message; details: optional
        code/result metadata.
    Returns: None; updates activity and appends a thinking message unless it repeats the
        last update.
    Details: Used throughout analysis. Checks cancellation while holding the session lock
        before changing visible state.
    """
    with session.lock:
        session.check_stop()
        messages = session.state['messages']
        if messages and messages[-1].get('kind') == 'thinking' and messages[-1]['text'] == text:
            return
        session.state['activity'] = title
        session.message('analysis_progress', text, 'thinking', {'title': title, **details})
