"""Local, immutable source tables for analysis chat. The first assigned ID survives regrouping."""
import json
import re
from collections import Counter

from markdown_it import MarkdownIt

from v_5.analyse.managers.turns import current_summary
from v_5.main_functions.session import write_json


MARKDOWN = MarkdownIt('default', {'html': False}).disable('image')
OPENING = 'turn_000000'


def markdown_tables(text):
    """Extract pipe tables from one Markdown message in display order.

    Inputs: text is the message body or a rendered methodology step.
    Returns: Tables with a title, column labels and rows of visible cell text.
    Details: The parser follows the browser's table grammar. The nearest preceding
        heading supplies the title when available.
    """
    if '|' not in text:
        return []
    tables = []
    table = row = None
    heading = ''
    in_heading = False
    for token in MARKDOWN.parse(text):
        if token.type == 'heading_open':
            in_heading = True
        elif token.type == 'heading_close':
            in_heading = False
        elif token.type == 'table_open':
            table = {'title': heading, 'columns': [], 'rows': []}
        elif token.type == 'tr_open':
            row = []
        elif token.type == 'inline' and (row is not None or in_heading):
            value = ''.join(' ' if part.type in ('hardbreak', 'softbreak') else part.content
                            for part in token.children or [])
            if in_heading:
                heading = value
            else:
                row.append(value)
        elif token.type == 'tr_close' and table is not None:
            if table['columns']:
                table['rows'].append(row)
            else:
                table['columns'] = row
            row = None
        elif token.type == 'table_close':
            tables.append(table)
            table = None
    return tables


def table_file(directory, table_id):
    """Resolve an issued table ID to its local JSON path.

    Inputs: directory is the chat archive and table_id is the full composite ID.
    Returns: Path under tables/, raising ValueError for an invalid ID format.
    Details: The chat directory supplies the namespace. Only the source turn and
        table ordinal are needed in the filename.
    """
    match = re.fullmatch(r'[0-9a-f]{32}_turn_\d+_(turn_\d+_table_\d+)', table_id)
    if not match:
        raise ValueError('Invalid table ID.')
    return directory / 'tables' / (match[1] + '.json')


def save_tables(directory, chat_id, state, *, restore=False):
    """Register source tables in the order in which the chat displays them.

    When: A turn finishes or the chat manager opens an older or restored archive.
    Inputs: directory, chat_id and mutable chat state. Set restore to revisit saved
        references and recreate missing assets.
    Returns: True when message references changed. A missing file can be recreated
        even when its reference already exists.
    Flow: Number Markdown, structured and methodology tables within each turn.
        Reuse issued identities from the message or disk before assigning new ones.
    """
    turns = {turn['id']: turn for turn in state.get('turns', [])}
    counters = Counter()
    changed = False
    for message in state.get('messages', []):
        if message['agent'] == 'You':
            continue
        details = message.get('details') or {}
        turn_id = message.get('turn_id') or OPENING
        turn = turns.get(turn_id, {})
        parent_id = turn.get('parent_id', turn_id)
        refs = details.get('table_assets', [])
        if refs and not restore:
            counters[turn_id] = max(counters[turn_id], *(ref['ordinal'] for ref in refs))
            continue
        tables = markdown_tables(message.get('text', ''))
        if message.get('kind') != 'thinking':
            tables.extend(details.get('tables', []))
        # Method steps are also rendered as Markdown beneath structured result tables.
        method = details.get('method') or {}
        if message.get('kind') != 'thinking' and isinstance(method, dict) and details.get('ran_analysis', bool(details.get('tables'))):
            steps = [step.strip() for step in method.get('steps', []) if step.strip()][:8]
            for step in steps:
                tables.extend(markdown_tables(re.sub(r'^(?:[-*•]|\d+[.)]|\(\d+\))\s+', '', step)))
        if not tables:
            continue
        assigned = []
        for index, table in enumerate(tables):
            ordinal = refs[index]['ordinal'] if index < len(refs) else counters[turn_id] + 1
            counters[turn_id] = max(counters[turn_id], ordinal)
            table_id = (refs[index]['table_id'] if index < len(refs) else
                        f'{chat_id}_{parent_id}_{turn_id}_table_{ordinal:03d}')
            target = table_file(directory, table_id)
            if index >= len(refs) and target.is_file():
                # An older Atlas checkpoint can predate the locally issued identity.
                issued = json.loads(target.read_text(encoding='utf-8'))['table_id']
                if not issued.startswith(chat_id + '_') or table_file(directory, issued) != target:
                    raise ValueError('The saved table belongs to another source.')
                table_id = issued
            ref = {'table_id': table_id, 'turn_id': turn_id, 'ordinal': ordinal,
                   'title': table.get('title') or f'Table {ordinal}', 'columns': table['columns']}
            if not target.is_file():
                write_json(target, {**table, **ref, 'decimal_places': details.get('decimal_places', 2)})
            assigned.append(ref)
        if assigned != refs:
            message['details'] = {**details, 'table_assets': assigned}
            changed = True
    return changed


def table_index(state, folder=''):
    """List the table references available for a chart request.

    Inputs: state is the current chat and folder is the optional analysis scope.
    Returns: References in message order, including IDs, titles and column labels.
    Details: Project scope includes all turns. Folder scope includes that folder's
        turns and tables from turns with no folder.
    """
    turns = {turn['id']: turn for turn in state.get('turns', [])}
    return [ref for message in state.get('messages', [])
            if not folder or turns.get(message.get('turn_id'), {}).get('folder') in (None, folder)
            for ref in (message.get('details') or {}).get('table_assets', [])]


def selected_table(directory, state, table_id, folder=''):
    """Load the selected source table from this chat and analysis scope.

    Inputs: directory and state identify the chat. table_id selects the source,
        and folder optionally limits the turns eligible for selection.
    Returns: The saved table, including its original rows and display precision.
    Flow: Resolve the ID through the chat index, restore a missing file from saved
        messages, then verify the identity inside the file.
    """
    ref = next((ref for ref in table_index(state, folder) if ref['table_id'] == table_id), None)
    if ref is None:
        raise ValueError('Choose a table from this chat and analysis scope.')
    path = table_file(directory, table_id)
    if not path.is_file():
        save_tables(directory, table_id.split('_', 1)[0], state, restore=True)
    asset = json.loads(path.read_text(encoding='utf-8'))
    if asset['table_id'] != table_id:
        raise ValueError('The saved table does not match the selection.')
    return asset


def source_context(state, table):
    """Gather the turn summary and conversation that explain a selected table.

    Inputs: state is the current chat and table is the resolved source asset.
    Returns: A pair containing the source-turn summary and completed turns with
        their messages from the same current parent group.
    Details: Regrouping changes the context the worker sees. Tables from the chat
        opening use the opening messages as their context.
    """
    turns = state.get('turns', [])
    source = next((turn for turn in turns if turn['id'] == table['turn_id']), None)
    if source is None:
        summary = {'turn_id': OPENING, 'parent_id': OPENING, 'header': 'Chat opening'}
        return summary, [{**summary, 'messages': [m for m in state['messages'] if not m.get('turn_id')]}]
    group = [{**current_summary(turn), 'messages': []} for turn in turns
             if turn['parent_id'] == source['parent_id'] and turn['answer_id']]
    by_turn = {entry['turn_id']: entry['messages'] for entry in group}
    for message in state['messages']:
        if message.get('turn_id') in by_turn:
            by_turn[message['turn_id']].append(message)
    return current_summary(source), group
