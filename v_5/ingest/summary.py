import json
import re
from collections import defaultdict
from psycopg2 import sql
from v_5.ingest.agent import FINAL_SUMMARY_PROMPT, FinalSummaryReply
from v_5.main_functions.sql import (
    CORE, GUIDE, INFO, PROJECT_SUMMARY_SQL, UNMATCHED, atlas_transaction, existing_publication,
    project_table,
)


SUMMARY_FILE = 'final_summary.md'


TABLE_DESCRIPTIONS = {
    CORE: 'One codebook entry per published variable in each folder.',
    UNMATCHED: 'Codebook entries with no matching column in the published data.',
    INFO: 'Reference information from the source documents.',
    GUIDE: 'Project and folder reference notes.',
}


def data_folders(state):
    """Find folders containing retained primary or continuation files.

    Inputs: state: project upload metadata.
    Returns: Set of active data-folder keys.
    Details: Used by summary readiness and inventory checks; ignored files and
        supporting-only folders do not count.
    """
    return {file['folder'] for file in state['files'].values()
            if not file.get('ignore') and file['role'] in {'primary', 'continuation'}}


def summary_ready(state):
    """Check whether every active data folder has been published.

    Inputs: state: project files and Atlas publication.
    Returns: Boolean readiness flag.
    Details: Used by ingestion and the publish route before write_final_summary; requires a
        nonempty exact folder match and published status.
    """
    atlas = state.get('atlas', {})
    folders = data_folders(state)
    return bool(folders and atlas.get('status') == 'published' and
                folders == {item['folder'] for item in atlas['data']})


def md_cell(value):
    """Make a value safe for one Markdown table cell.

    Inputs: value: display value, None or not in source.
    Returns: Single-line string with pipes escaped; absent values become empty.
    Details: Used by folder and project summaries to keep user/source text from breaking
        table columns.
    """
    if value is None or value == 'not in source':
        return ''
    return ' '.join(str(value).split()).replace('|', r'\|')


def describe(session, scope, context):
    """Ask for a short description of published data.

    When: Ingestion completion: final_summary_agent describes a folder or the project.
    Inputs: session: model calls; scope: description level; context: inventory and source
        evidence.
    Returns: Trimmed summary string.
    Flow: Calls the shared summary prompt/contract with user questions disabled;
        summarize_folder and write_final_summary provide the evidence.
    """
    reply = session.ask_agent('final_summary_agent', FINAL_SUMMARY_PROMPT,
                              [{'role': 'user', 'content': json.dumps({'scope': scope, **context}, ensure_ascii=False)}],
                              FinalSummaryReply, allow_user_questions=False)
    return reply['summary'].strip()


def read_inventory(state):
    """Read the published tables and variables for the final summary.

    Inputs: state: project ID and retained data folders.
    Returns: (publication, widths, variables): registry snapshot, table-to-column counts and
        codebook rows grouped by dataset ID.
    Details: Checks complete publication inside atlas_transaction, then reads SQL metadata
        rather than trusting only local preparation reports.
    """
    schema = state['id']
    with atlas_transaction() as cursor:
        publication = existing_publication(cursor, schema)
        if not publication or {item['folder'] for item in publication['data']} != data_folders(state):
            raise ValueError('Publish every data folder before writing the final summary.')
        cursor.execute('SELECT table_name, count(*) FROM information_schema.columns WHERE table_schema = %s GROUP BY table_name',
                       (schema,))
        widths = dict(cursor.fetchall())
        cursor.execute(sql.SQL('''SELECT dataset_id, variable_name, variable_type, variable_label,
            variable_description, theme, subtheme, table_names FROM {}
            ORDER BY dataset_id, theme, subtheme, variable_name''').format(project_table(schema, CORE)))
        variables = defaultdict(list)
        for dataset_id, *row in cursor.fetchall():
            variables[dataset_id].append(row)
    return publication, widths, variables


def summarize_folder(session, folder, tables, columns, widths):
    """Build the narrative and complete variable listing for one dataset.

    Inputs: session/folder: owner and key; tables: published parts; columns: ordered
        codebook rows; widths: SQL table column counts.
    Returns: Dict with name, records, variables, description and markdown.
    Details: Calls describe with bounded examples, then renders all columns/themes
        deterministically for write_final_summary.
    """
    state = session.state
    details = state['folders'].get(folder, {})
    label = details.get('display_name') or folder
    rows, variable_count = tables[0]['row_count'], len(columns)
    themes = defaultdict(set)
    examples = {}
    for name, kind, title, description, theme, subtheme, _ in columns:
        if theme and subtheme:
            themes[theme].add(subtheme)
        example = [name, kind, (title or '')[:120], (description or '')[:400]]
        key = (theme, subtheme)
        if key not in examples or not md_cell(examples[key][-1]):
            examples[key] = example
    sources = [file['dataset_context'] for file in state['files'].values()
               if file['folder'] == folder and file['role'] in {'primary', 'continuation'}
               and not file.get('ignore') and file.get('dataset_context')]
    description = describe(session, 'folder', {
        'folder': label, 'folder_description': details.get('description', ''),
        'project_description': state['context'].get('description', ''), 'source_context': sources,
        'shape': {'records': rows, 'variables': variable_count},
        'themes': {key: sorted(values) for key, values in themes.items()},
        'example_fields': ['name', 'type', 'label', 'description'], 'examples': list(examples.values()),
    })
    lines = [f'## {md_cell(label)}', '', description, '', f'**Dataset shape** — {rows:,} records × {variable_count:,} variables.', '',
             '| Data table | Rows | Columns |', '| --- | ---: | ---: |']
    lines.extend(f"| {md_cell(item['name'])} | {item['row_count']:,} | {widths[item['name']]:,} |" for item in tables)
    lines.extend(['', f'### Themes and subthemes ({len(themes)} themes)', '',
                  *(f"- **{md_cell(theme)}** — {', '.join(map(md_cell, sorted(subthemes)))}"
                    for theme, subthemes in sorted(themes.items())), ''])
    lines.extend(['### Columns', '', '| Variable | Type | Label | Description | Theme | Subtheme | Data tables |',
                  '| --- | --- | --- | --- | --- | --- | --- |'])
    lines.extend('| ' + ' | '.join(map(md_cell, [*column[:-1], ', '.join(column[-1] or [])])) + ' |' for column in columns)
    return {'name': label, 'records': rows, 'variables': variable_count,
            'description': description, 'markdown': '\n'.join(lines)}


def write_final_summary(session):
    """Save the complete project summary locally and in Atlas.

    When: All retained data folders are published; final_summary_agent supplies brief
        descriptions.
    Inputs: session: project state, logging and model calls.
    Returns: complete; writes final_summary.md, SQL summary text and per-folder cache
        metadata.
    Flow: Reads the publication inventory, reuses unchanged folder summaries, renders tables
        and rechecks the publication before saving. A matching completed release is reused.
    """
    state = session.state
    if not summary_ready(state):
        raise ValueError('Publish every data folder before writing the final summary.')
    state['activity'] = 'Writing the project summary'
    session.save()
    publication, widths, variables = read_inventory(state)
    target = session.path / SUMMARY_FILE
    if state.get('final_summary_release') == publication['release'] and target.is_file():
        return 'complete'

    datasets = defaultdict(list)
    for table in publication['data']:
        datasets[table['dataset_id']].append(table)
    cached = state.get('final_summary_folders', [])
    sections = re.split(r'^---------- Folder \d+ ----------\n\n', target.read_text(encoding='utf-8'), flags=re.M)[1:] if target.is_file() else []
    saved = {entry['dataset_id']: {**entry, 'markdown': section.rstrip()}
             for entry, section in zip(cached, sections)}
    folders = []
    for index, (dataset_id, tables) in enumerate(datasets.items()):
        table = tables[0]
        release = table.get('release', publication['release'])
        folder = saved.get(dataset_id)
        if not cached and index < len(sections):
            # Summaries from before the folder cache used the same section order.
            section = sections[index].rstrip()
            match = re.match(r'^## ([^\n]+)\n\n(.*?)\n\n\*\*Dataset shape\*\*', section, flags=re.S)
            if match and match[1] == md_cell(table['dataset_name']):
                folder = {'name': table['dataset_name'], 'records': table['row_count'],
                          'variables': len(variables[dataset_id]), 'description': match[2],
                          'markdown': section, 'release': release}
        if not folder or folder['release'] != release:
            folder = summarize_folder(session, table['folder'], tables, variables[dataset_id], widths)
        folders.append({**folder, 'dataset_id': dataset_id, 'release': release})
    # Logical dataset totals count each folder once; stored-table totals below include repeated table parts.
    total_rows = sum(folder['records'] for folder in folders)
    total_variables = sum(folder['variables'] for folder in folders)
    project_description = describe(session, 'project', {
        'project': state['context']['name'], 'project_description': state['context'].get('description', ''),
        'folders': [{key: folder[key] for key in ('name', 'records', 'variables', 'description')} for folder in folders],
    })

    data_rows = sum(item['row_count'] for item in publication['data'])
    data_columns = sum(widths[item['name']] for item in publication['data'])
    reference_counts = {**publication['codebook'], **publication['information']}
    lines = [f"# {md_cell(state['context']['name'])}", '', project_description, '', '## Atlas schema and tables', '',
             f"- **Location** — `atlas`, schema `{publication['schema']}`.",
             f'- **Logical datasets** — {len(folders):,} folders, {total_rows:,} records, '
             f'{total_variables:,} variables summed across folders. Each dataset counts once across its table parts.',
             f"- **Data tables** — {len(publication['data']):,} tables, {data_rows:,} stored rows and {data_columns:,} column slots "
             'summed across tables. Columns and rows repeated across table parts count in each part.',
             f'- **All listed tables** — {data_rows + sum(reference_counts.values()):,} stored rows and '
             f'{data_columns + sum(widths[name] for name in reference_counts):,} column slots, including the reference tables below.',
             '- **Variable organization** — contains variables organized at two levels, themes and subthemes, listed for each folder.',
             '', '| Reference table | Rows | Columns | Contents |', '| --- | ---: | ---: | --- |']
    lines.extend(f'| {name} | {reference_counts[name]:,} | {widths[name]:,} | {description} |'
                 for name, description in TABLE_DESCRIPTIONS.items())
    for index, folder in enumerate(folders, 1):
        lines.extend(['', f'---------- Folder {index} ----------', '', folder['markdown']])
    markdown = '\n'.join(lines) + '\n'

    session.check_stop()
    temporary = target.with_suffix('.md.tmp')
    try:
        temporary.write_bytes(markdown.encode('utf-8'))
        with atlas_transaction() as cursor:
            if existing_publication(cursor, state['id']) != publication:
                raise ValueError('The Atlas project changed while its summary was being written. Retry the summary.')
            cursor.execute(PROJECT_SUMMARY_SQL)
            cursor.execute('''INSERT INTO public.atlas_project_summaries (project_id, summary_md) VALUES (%s, %s)
                ON CONFLICT (project_id) DO UPDATE SET summary_md = EXCLUDED.summary_md''', (state['id'], markdown))
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    state.update(final_summary_path=SUMMARY_FILE, final_summary_release=publication['release'])
    state['final_summary_folders'] = [{key: value for key, value in folder.items() if key != 'markdown'} for folder in folders]
    session.record('final_summary_saved', 'final_summary_agent', {'path': str(target), 'folders': [folder['name'] for folder in folders]})
    session.message('final_summary_agent', 'The project data summary is ready to download in Atlas publication.')
    return 'complete'
