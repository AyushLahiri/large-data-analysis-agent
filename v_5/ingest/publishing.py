import csv
import io
import json
import math
import pyarrow.parquet as pq
import re
from copy import deepcopy
from contextlib import closing
from openai import OpenAI
from pathlib import Path
from psycopg2 import sql
from psycopg2.extras import Json
from v_5.app.config import api_key
from v_5.app.database import get_db_connection
from v_5.ingest.manifest import manifest_columns, work_path
from v_5.main_functions.session import write_json
from v_5.main_functions.sql import (
    CODEBOOK_FIELDS, SOURCE_CODEBOOK_FIELDS, CORE, DIMENSIONS, EMBEDDING_MODEL, GUIDE, INFO, UNMATCHED, SOFT_MATCHED, existing_publication,
    guide_summary, identifier, project_table, ensure_registry,
)


SQL_TYPES = {'character': 'TEXT', 'int': 'BIGINT', 'numeric': 'NUMERIC'}


SQL_TYPE_PATTERN = re.compile(r'TEXT|BIGINT|SMALLINT|INTEGER|NUMERIC(?:\(\d{1,4}(?:,\s*-?\d{1,4})?\))?')


def local_artifact(project, path):
    """Check that a publication input is a saved file in this project.

    Inputs: project: project directory; path: artifact path.
    Returns: Resolved Path, or ValueError if missing/outside project runs.
    Details: Used before reading loader/codebook outputs so a saved plan cannot point at
        unrelated files.
    """
    artifact = Path(path).resolve()
    if not artifact.is_relative_to((project / 'runs').resolve()) or not artifact.is_file():
        raise ValueError('An expected Atlas input is missing from this project’s saved runs.')
    return artifact


def saved_atlas_plan(project, state):
    """Load or reconstruct the dataset publication plan.

    Inputs: project: project directory; state: saved preparation/codebook state.
    Returns: Dict with release and datasets; each dataset carries folder, workspace,
        prepared report and book metadata.
    Details: The publish route uses a saved retry handoff when available; otherwise
        completed artifacts must exist for every data folder.
    """
    if state.get('atlas_handoff_path'):
        path = local_artifact(project, state['atlas_handoff_path'])
        return json.loads(path.read_text(encoding='utf-8'))
    if state['status'] != 'complete' or not state.get('handoff_path'):
        raise ValueError('Finish data preparation and the final codebook before publishing to Atlas.')
    folders = list(dict.fromkeys(file['folder'] for file in state['files'].values()
                                 if file['role'] in {'primary', 'continuation'} and not file.get('ignore')))
    datasets = []
    for folder in folders:
        book = state.get('codebooks', {}).get(folder, {})
        prepared = state.get('preparations', {}).get(folder, {})
        if not book.get('final') or not prepared.get('manifest_path'):
            raise ValueError('Finish the codebook for every dataset before publishing to Atlas.')
        workspace = local_artifact(project, book['final']).parent.parent
        datasets.append({'folder': folder, 'workspace': str(workspace), 'prepared': prepared, 'book': book})
    if not datasets:
        raise ValueError('There are no prepared datasets to publish.')
    return {'release': Path(datasets[0]['workspace']).parents[1].name, 'datasets': datasets}


def prepared_sql_type(metadata):
    """Turn prepared column metadata into an allowed SQL type.

    Inputs: metadata: type/sql_type/sql_definition and optional column name.
    Returns: Composable SQL type fragment; raises for unsupported declarations.
    Details: Used by output validation and table creation; permits only the existing text
        and numeric type forms.
    """
    declared = metadata.get('sql_definition') or metadata.get('sql_type') or SQL_TYPES.get(metadata.get('type'), '')
    declared = str(declared).strip().upper()
    if not SQL_TYPE_PATTERN.fullmatch(declared):
        raise ValueError(f"Unsupported prepared SQL type for {metadata.get('name', 'column')}: {declared!r}.")
    return sql.SQL(declared)


def collect_outputs(project, state, plan, publication=None):
    """Validate saved dataset artifacts and assemble publication payloads.

    Inputs: project/state: owner and notes; plan: datasets to publish; publication: optional
        existing tables.
    Returns: (data, core, unmatched, soft_matched, info, guide): table/file metadata,
        three codebook row sets, source notes and structured project/dataset notes.
    Details: Called by publish_atlas. Checks manifests against Parquet, assigns unique SQL
        names/dataset IDs and maps original variables to their prepared columns.
    """
    used_names = {CORE, UNMATCHED, SOFT_MATCHED, INFO, GUIDE}
    published_tables = (publication or {}).get('data', [])
    used_names.update(item['name'] for item in published_tables)
    used_names.update(item['name'] for item in (publication or {}).get('ingested_codebooks', []))
    first_dataset = 1 + max((int(item['dataset_id'].removeprefix('dataset_')) for item in published_tables), default=0)
    data, core, unmatched, soft_matched, info = [], [], [], [], []
    context = state['context']
    project_notes = [f"# {context['name']}", context.get('description', ''), state.get('project_notes', '')]
    for key in ('folder_relationships', 'before_ingestion'):
        if context.get(key):
            project_notes.append(context[key])
    if state.get('handoff_path'):
        handoff = local_artifact(project, state['handoff_path'])
        clarifications = json.loads(handoff.read_text(encoding='utf-8')).get('user_clarifications', [])
        if clarifications:
            project_notes.extend(['## User clarifications', *clarifications])
    guide = {'project': '\n\n'.join(filter(None, project_notes)), 'datasets': []}
    for index, dataset in enumerate(plan['datasets'], first_dataset):
        folder = dataset['folder']
        dataset_id = f'dataset_{index:03d}'
        name = state['folders'].get(folder, {}).get('display_name') or folder
        prepared, book = dataset['prepared'], dataset['book']
        workspace = Path(dataset['workspace']).resolve()
        manifest_path = local_artifact(project, work_path(workspace, prepared['manifest_path']))
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        locations = {}
        folder_tables = []
        for table in manifest['tables']:
            table_name = table['name']
            if table_name in used_names:
                suffix = f'_{dataset_id}_{len(folder_tables) + 1}'
                table_name = table_name.encode('utf-8')[:63 - len(suffix)].decode('utf-8', errors='ignore') + suffix
                if table_name in used_names:
                    raise ValueError(f'Prepared table name is repeated: {table_name}.')
            identifier(table_name)
            used_names.add(table_name)
            folder_tables.append(table_name)
            parquet_path = table.get('file_path') or table.get('path') or table.get('parquet_path')
            path = local_artifact(project, work_path(workspace, parquet_path))
            columns = manifest_columns(table)
            parquet = pq.ParquetFile(path)
            if set(parquet.schema_arrow.names) != set(columns):
                raise ValueError(f'{table_name}: Parquet columns do not match the prepared manifest.')
            row_count = parquet.metadata.num_rows
            if row_count != table['row_count']:
                raise ValueError(f'{table_name}: Parquet row count differs from the prepared manifest.')
            for column in parquet.schema_arrow.names:
                definition = columns[column]
                identifier(column)
                prepared_sql_type({**definition, 'name': column})
                key = column.lower()
                locations.setdefault(key, []).append((table_name, column, definition.get('type') or definition.get('sql_definition') or definition.get('sql_type')))
            data.append({'dataset_id': dataset_id, 'dataset_name': name, 'folder': folder, 'name': table_name, 'path': path,
                         'columns': columns, 'row_count': row_count, 'primary_key': table['primary_key']})
            if prepared.get('_reuse_sql'):
                reuse = prepared['_reuse_sql']
                data[-1]['copy_sql'] = {'project_id': reuse['project_id'], 'folder': reuse['folder'],
                                       'table': reuse['table_map'][table['name']]}

        for artifact, destination in [('final', core), ('final_unmatched', unmatched), ('soft_matched', soft_matched)]:
            if artifact == 'soft_matched' and not book.get(artifact):
                continue  # Previously completed folders predate the additional table.
            with local_artifact(project, book[artifact]).open(encoding='utf-8', newline='') as source:
                rows = list(csv.DictReader(source))
            if artifact == 'final' and ({row['variable_name'] for row in rows} != set(locations) or len(rows) != len(locations)):
                raise ValueError(f'{name}: the final codebook does not match the prepared variables.')
            for row in rows:
                fields = CODEBOOK_FIELDS if artifact == 'final' else SOURCE_CODEBOOK_FIELDS
                values = {key: row.get(key) or None for key in fields}
                values['pct_missing'] = row.get('% missing') or None
                values['battery_members'] = row.get('battery_members') or '[]'
                if artifact != 'final':
                    values['soft_match'] = row.get('soft_match') or '[]'
                if artifact == 'final':
                    matches = locations[row['variable_name']]
                    values['table_name'], values['column_name'], declared_type = matches[0]
                    values['variable_type'] = declared_type or values['variable_type']
                    values['table_names'] = [match[0] for match in matches]
                destination.append((dataset_id, *values.values()))
        for source in book.get('source_tables', {}).values():
            notes = local_artifact(project, source['notes']).read_text(encoding='utf-8')
            info.append((dataset_id, source['filename'], notes))
        note = state.get('folder_notes', {}).get(folder, {})
        folder_notes = [f'## {name}', f"Prepared tables: {', '.join(folder_tables)}.", note.get('summary', '')]
        if note.get('primary_codebook'):
            folder_notes.append(f"Primary codebook: {note['primary_codebook']}")
        if prepared.get('preparation_notes_path'):
            preparation = local_artifact(project, work_path(workspace, prepared['preparation_notes_path']))
            folder_notes.append(preparation.read_text(encoding='utf-8'))
        guide['datasets'].append({'dataset_id': dataset_id, 'notes': '\n\n'.join(filter(None, folder_notes))})
    # Tuple order is shared with write_publication; core rows also determine embedding order.
    return data, core, unmatched, soft_matched, info, guide


def embed_descriptions(session, rows):
    """Create search embeddings for useful variable descriptions.

    Inputs: session: cancellation/logging; rows: core codebook tuples in CODEBOOK_FIELDS
        order.
    Returns: List aligned with rows: JSON vector strings, or None where no useful
        description exists.
    Details: Used before SQL publication. Sends batches of 64, verifies complete 2,000-value
        vectors and records usage without shifting row alignment.
    """
    descriptions = [row[1 + list(CODEBOOK_FIELDS).index('variable_description')] for row in rows]
    positions = [i for i, text in enumerate(descriptions) if text and text.strip().lower() != 'not in source']
    # Keep one slot per codebook row even when its description is absent.
    embeddings = [None] * len(rows)
    if not positions:
        return embeddings
    client = OpenAI(api_key=api_key())
    for start in range(0, len(positions), 64):
        session.check_stop()
        batch = positions[start:start + 64]
        response = client.embeddings.create(model=EMBEDDING_MODEL, dimensions=DIMENSIONS,
                                            input=[descriptions[i].strip() for i in batch])
        if sorted(item.index for item in response.data) != list(range(len(batch))):
            raise ValueError('OpenAI returned an incomplete embedding batch.')
        for item in response.data:
            if len(item.embedding) != DIMENSIONS:
                raise ValueError('OpenAI did not return 2,000-dimensional embeddings.')
            embeddings[batch[item.index]] = json.dumps(item.embedding, separators=(',', ':'), allow_nan=False)
        session.record('atlas_embeddings', 'atlas', {'model': EMBEDDING_MODEL, 'dimensions': DIMENSIONS,
                       'progress': min(start + len(batch), len(positions)), 'descriptions': len(positions),
                       'usage': response.usage.model_dump()})
    return embeddings


def copy_rows(cursor, table, columns, rows):
    """Bulk-copy rows while preserving the difference between null and empty text.

    Inputs: cursor: active SQL cursor; table: quoted target; columns: ordered names; rows:
        matching value sequences.
    Returns: None; streams CSV-formatted rows into PostgreSQL COPY.
    Details: write_publication uses this for Parquet batches. Non-null fields are quoted;
        null fields remain unquoted empty fields.
    """
    def field(value):
        """Encode one value for PostgreSQL CSV import.

        Inputs: value: cell value, including None or floating NaN.
        Returns: CSV field string: unquoted empty for null, quoted/escaped text otherwise.
        Details: Private helper for copy_rows; an actual empty string remains a quoted empty
            value.
        """
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return ''
        return '"' + str(value).replace('"', '""') + '"'

    stream = io.StringIO()
    for row in rows:
        stream.write(','.join(map(field, row)) + '\n')
    if stream.tell():
        stream.seek(0)
        query = sql.SQL("COPY {} ({}) FROM STDIN WITH (FORMAT CSV, NULL '')").format(
            table, sql.SQL(', ').join(map(identifier, columns)))
        cursor.copy_expert(query, stream)


def source_table_name(filename, used):
    """Name an individual ingested codebook; add a suffix only for a real collision."""
    from werkzeug.utils import secure_filename
    label = Path(filename)
    base = 'ingestedcodebook_' + (secure_filename(label.stem).lower() or 'file') + '_' + (label.suffix.lstrip('.').lower() or 'file')
    name, number = base[:63], 2
    while name in used:
        suffix = f'_{number}'
        name = base[:63 - len(suffix)] + suffix
        number += 1
    used.add(name)
    return name


def register_existing_codebooks(path, state):
    """Register older completed local codebooks when a matching upload first needs them.

    Inputs: an inactive completed source project. Returns: its current publication.
    This small compatibility step uses saved normalized tables and makes no agent calls.
    """
    from v_5.main_functions.sql import atlas_transaction
    with atlas_transaction() as cursor:
        ensure_registry(cursor)
        previous = existing_publication(cursor, state['id'])
        if not previous or 'ingested_codebooks' in previous:
            return previous or {}
        from types import SimpleNamespace
        session = SimpleNamespace(path=Path(path), state=state)
        records = save_ingested_codebooks(cursor, session, previous['data'], previous)
        previous['ingested_codebooks'] = records
        cursor.execute('UPDATE public.atlas_projects SET publication=%s WHERE project_id=%s',
                       (Json(previous), state['id']))
    state['atlas'] = previous
    write_json(Path(path) / 'project.json', state)
    return previous


def save_ingested_codebooks(cursor, session, data, previous):
    """Publish each normalized source table and register its original file identity.

    Inputs: transaction, destination session, newly published data records and old registry.
    Returns: full source-table registry. Regular ingestion reads its prefixed CSV; whole
    folder copies use SQL directly. Both store unsuffixed COLUMNS for later extraction reuse.
    """
    import pandas as pd
    from v_5.ingest.codebook_ingestion.tables import COLUMNS
    from v_5.main_functions.assets import file_digest
    state, records = session.state, deepcopy((previous or {}).get('ingested_codebooks', []))
    used = {CORE, UNMATCHED, SOFT_MATCHED, INFO, GUIDE, *(t['name'] for t in data),
            *(t['name'] for t in (previous or {}).get('data', [])), *(t['name'] for t in records)}
    folders = {item['folder']: item['dataset_id'] for item in data}
    for folder, dataset_id in folders.items():
        book = state.get('codebooks', {}).get(folder, {})
        for prefix, source in book.get('source_tables', {}).items():
            file_id = source.get('file_id')
            if not file_id:
                candidates = [key for key, item in state['files'].items()
                              if item['folder'] == folder and item['filename'] == source['filename']]
                if len(candidates) != 1:
                    raise ValueError('The source codebook must identify its original uploaded file.')
                file_id = candidates[0]
            file = state['files'][file_id]
            name = source_table_name(file['filename'], used)
            target = project_table(state['id'], name)
            if source.get('copy_sql'):
                original = source['copy_sql']
                cursor.execute(sql.SQL('CREATE TABLE {} (LIKE {} INCLUDING ALL)').format(
                    target, project_table(original['project_id'], original['name'])))
                cursor.execute(sql.SQL('INSERT INTO {} SELECT * FROM {}').format(
                    target, project_table(original['project_id'], original['name'])))
                notes = source.get('notes_text', '')
                count = cursor.rowcount
            else:
                frame = pd.read_csv(local_artifact(session.path, source['table']), dtype=str, keep_default_na=False)
                frame = frame.rename(columns={c + '_' + prefix: c for c in COLUMNS if c != 'variable_name'})[COLUMNS]
                notes = local_artifact(session.path, source['notes']).read_text(encoding='utf-8')
                definitions = sql.SQL(', ').join(sql.SQL('{} text NOT NULL').format(identifier(c)) for c in COLUMNS)
                cursor.execute(sql.SQL('CREATE TABLE {} ({}, PRIMARY KEY (variable_name))').format(target, definitions))
                copy_rows(cursor, target, COLUMNS, frame.itertuples(index=False, name=None))
                count = len(frame)
            records.append({'name': name, 'file_id': file_id, 'filename': file['filename'], 'folder': folder,
                            'dataset_id': dataset_id, 'sha256': file.get('sha256') or file_digest(file['path']),
                            'bytes': file['bytes'], 'notes': notes, 'row_count': count,
                            'embedded_for': file.get('embedded_for'), 'primary': source.get('primary', False)})
    return records


def write_publication(cursor, session, release, outputs, embeddings, previous=None):
    """Write a folder's data and reference assets within a SQL transaction.

    When: Ingestion publication, after collect_outputs and embed_descriptions.
    Inputs: cursor/session/release: transaction and owner; outputs:
        data/core/unmatched/soft_matched/info/guide tuple; embeddings: aligned vectors; previous:
        optional publication.
    Returns: Updated publication dict with tables, counts and embedding metadata; writes
        schema assets and registry.
    Flow: Creates typed tables, copies Parquet batches, inserts codebooks/notes and extends
        the guide. The caller controls commit/rollback; existing folder records are
        retained.
    """
    data, core, unmatched, soft_matched, info, guide = outputs
    state = session.state
    schema = state['id']

    def table(name):
        """Qualify a publication table with this project's schema.

        Inputs: name: local table name; captures schema.
        Returns: Composable SQL table reference.
        Details: Private shorthand for project_table inside write_publication.
        """
        return project_table(schema, name)

    if previous is None:
        cursor.execute(sql.SQL('CREATE SCHEMA {}').format(identifier(schema)))
    for item in data:
        session.check_stop()
        if item.get('copy_sql'):
            source = item['copy_sql']
            cursor.execute('SELECT publication FROM public.atlas_projects WHERE project_id=%s AND user_id=%s',
                           (source['project_id'], str(state.get('user_id', '9999'))))
            source_row = cursor.fetchone()
            if not source_row or not any(t['folder'] == source['folder'] and t['name'] == source['table']
                                         for t in (source_row[0] or {}).get('data', [])):
                raise ValueError('The copied dataset is no longer available for this user.')
            original = project_table(source['project_id'], source['table'])
            cursor.execute(sql.SQL('CREATE TABLE {} (LIKE {} INCLUDING ALL)').format(table(item['name']), original))
            cursor.execute(sql.SQL('INSERT INTO {} SELECT * FROM {}').format(table(item['name']), original))
            if cursor.rowcount != item['row_count']:
                raise ValueError('The source data changed; review this folder’s copy choice again.')
            continue
        parquet = pq.ParquetFile(item['path'])
        columns = parquet.schema_arrow.names
        definitions = []
        for column in columns:
            metadata = item['columns'][column]
            definition = sql.SQL('{} {}').format(identifier(column), prepared_sql_type({**metadata, 'name': column}))
            if metadata.get('nullable') is False:
                definition += sql.SQL(' NOT NULL')
            definitions.append(definition)
        definitions.append(sql.SQL('PRIMARY KEY ({})').format(sql.SQL(', ').join(map(identifier, item['primary_key']))))
        target = table(item['name'])
        cursor.execute(sql.SQL('CREATE TABLE {} ({})').format(target, sql.SQL(', ').join(definitions)))
        for batch in parquet.iter_batches(batch_size=1024):
            session.check_stop()
            copy_rows(cursor, target, columns, zip(*(array.to_pylist() for array in batch.columns)))

    if previous is None:
        fields = sql.SQL(', ').join(sql.SQL('{} {}').format(identifier(name), sql.SQL(kind))
                                   for name, kind in CODEBOOK_FIELDS.items())
        for name in (CORE, UNMATCHED):
            vector_field = sql.SQL(', embedding vector(2000)') if name == CORE else sql.SQL('')
            cursor.execute(sql.SQL('CREATE TABLE {} (dataset_id text NOT NULL, {}{}, PRIMARY KEY (dataset_id, variable_name))')
                           .format(table(name), fields, vector_field))
        cursor.execute(sql.SQL('CREATE TABLE {} (dataset_id text NOT NULL, source_filename text NOT NULL, content text NOT NULL)').format(table(INFO)))
        cursor.execute(sql.SQL('CREATE TABLE {} (summary text NOT NULL, sections jsonb NOT NULL)').format(table(GUIDE)))
    # Add the source-to-master links when an existing project publishes another folder.
    cursor.execute(sql.SQL('ALTER TABLE {} ADD COLUMN IF NOT EXISTS soft_match jsonb').format(table(UNMATCHED)))
    source_fields = sql.SQL(', ').join(sql.SQL('{} {}').format(identifier(name), sql.SQL(kind))
                                     for name, kind in SOURCE_CODEBOOK_FIELDS.items())
    cursor.execute(sql.SQL('CREATE TABLE IF NOT EXISTS {} (dataset_id text NOT NULL, {}, PRIMARY KEY (dataset_id, variable_name))')
                   .format(table(SOFT_MATCHED), source_fields))
    for name, rows in ((UNMATCHED, unmatched), (SOFT_MATCHED, soft_matched)):
        cursor.executemany(sql.SQL('INSERT INTO {} ({}) VALUES ({})').format(table(name),
            sql.SQL(', ').join(map(identifier, ['dataset_id', *SOURCE_CODEBOOK_FIELDS])),
            sql.SQL(', ').join([sql.Placeholder()] * (len(SOURCE_CODEBOOK_FIELDS) + 1))), rows)
    cursor.executemany(sql.SQL('INSERT INTO {} VALUES ({})').format(table(CORE), sql.SQL(', ').join([sql.Placeholder()] * (len(CODEBOOK_FIELDS) + 2))),
                       [(*row, vector) for row, vector in zip(core, embeddings)])
    cursor.executemany(sql.SQL('INSERT INTO {} VALUES (%s, %s, %s)').format(table(INFO)), info)
    if previous is None:
        cursor.execute(sql.SQL('CREATE INDEX codebook_embedding_hnsw ON {} USING hnsw (embedding vector_cosine_ops)').format(table(CORE)))
        cursor.execute(sql.SQL('INSERT INTO {} VALUES (%s, %s)').format(table(GUIDE)), (guide_summary(guide), Json(guide)))
    else:
        cursor.execute(sql.SQL('SELECT sections FROM {}').format(table(GUIDE)))
        old_guide = cursor.fetchone()[0]
        guide = {**guide, 'datasets': old_guide['datasets'] + guide['datasets']}
        cursor.execute(sql.SQL('UPDATE {} SET summary = %s, sections = %s').format(table(GUIDE)), (guide_summary(guide), Json(guide)))

    old_tables = [dict(item, release=item.get('release', previous['release'])) for item in previous['data']] if previous else []
    new_tables = [dict({key: item[key] for key in ('dataset_id', 'dataset_name', 'folder', 'name', 'row_count')}, release=release) for item in data]
    old_codebook = (previous or {}).get('codebook', {})
    old_information = (previous or {}).get('information', {})
    publication = {'status': 'published', 'database': 'atlas', 'schema': schema, 'release': release,
                   'data': old_tables + new_tables,
                   'codebook': {CORE: old_codebook.get(CORE, 0) + len(core), UNMATCHED: old_codebook.get(UNMATCHED, 0) + len(unmatched),
                                SOFT_MATCHED: old_codebook.get(SOFT_MATCHED, 0) + len(soft_matched)},
                   'information': {INFO: old_information.get(INFO, 0) + len(info), GUIDE: 1},
                   'embedding_model': EMBEDDING_MODEL, 'dimensions': DIMENSIONS,
                   'embeddings': (previous or {}).get('embeddings', 0) + sum(v is not None for v in embeddings),
                   'index': 'codebook_embedding_hnsw'}
    publication['ingested_codebooks'] = save_ingested_codebooks(cursor, session, data, previous)
    cursor.execute('''INSERT INTO public.atlas_projects (project_id, project_name, release, publication, published_at)
        VALUES (%s, %s, %s, %s, now()) ON CONFLICT (project_id) DO UPDATE
        SET release = EXCLUDED.release, publication = EXCLUDED.publication, published_at = EXCLUDED.published_at''',
                   (schema, state['context']['name'], release, Json(publication)))
    return publication


def publish_atlas(session, plan):
    """Publish prepared folders and retain a retryable handoff.

    When: After phase 4 finalization, or when the user retries publication.
    Inputs: session: project state/logging; plan: release and dataset bundles, with optional
        next_folder.
    Returns: complete on success; records publication state. Errors mark Atlas failed and
        propagate.
    Flow: Saves the plan, checks Atlas/pgvector, validates artifacts and embeds definitions,
        then writes each folder transactionally. Rechecks concurrent changes and avoids
        republishing the same release.
    """
    state = session.state
    handoff = session.run_path / 'atlas_handoff.json'
    write_json(handoff, plan)
    state['atlas_handoff_path'] = str(handoff)
    session.save()
    try:
        schema, release = state['id'], plan['release']
        if not re.fullmatch('[0-9a-f]{32}', schema):
            raise ValueError('Atlas publishing requires a valid project ID.')
        with closing(get_db_connection()) as connection:
            with connection, connection.cursor() as cursor:
                ensure_registry(cursor)
                cursor.execute('SELECT current_database(), to_regtype(\'vector\')')
                database, vector = cursor.fetchone()
                if database != 'atlas' or not vector:
                    raise ValueError('The Atlas database must have pgvector available before publishing.')
            for index, dataset in enumerate(plan['datasets']):
                session.check_stop()
                folder = dataset['folder']
                label = state['folders'].get(folder, {}).get('display_name') or folder
                with connection, connection.cursor() as cursor:
                    previous = existing_publication(cursor, schema)
                published = next((item for item in (previous or {}).get('data', []) if item['folder'] == folder), None)
                if published:
                    if published.get('release', previous['release']) != release:
                        raise ValueError(f'{label} is already published in Atlas. Publishing a replacement requires a separate update.')
                    publication = previous
                else:
                    state['atlas'] = {**(previous or {}), 'status': 'publishing'}
                    state['activity'] = 'Publishing to Atlas'
                    session.message('atlas', f'Publishing {label} to Atlas.')
                    outputs = collect_outputs(session.path.resolve(), state, {'datasets': [dataset]}, previous)
                    embeddings = embed_descriptions(session, outputs[1])
                    session.check_stop()
                    with connection, connection.cursor() as cursor:
                        # Model calls happen outside this transaction; recheck that another writer has not changed the project.
                        if existing_publication(cursor, schema) != previous:
                            raise ValueError('This project changed in Atlas during upload. Retry the folder upload.')
                        publication = write_publication(cursor, session, release, outputs, embeddings, previous)
                        session.check_stop()
                state['atlas'] = publication
                write_json(session.run_path / 'atlas_publication.json', publication)
                session.record('atlas_published', 'atlas', {'folder': folder, 'publication': publication})
                remaining = plan['datasets'][index + 1:]
                next_folder = remaining[0]['folder'] if remaining else plan.get('next_folder')
                if not any(message.get('kind') == 'folder_complete' and
                           message['details'] == {'folder': folder, 'release': release} for message in state['messages']):
                    message = f'Ingestion is complete for {label}.'
                    if next_folder:
                        next_label = state['folders'].get(next_folder, {}).get('display_name') or next_folder
                        message += f' Beginning with {next_label}.'
                    session.message('atlas', message, 'folder_complete', {'folder': folder, 'release': release})
        state.pop('atlas_handoff_path', None)
        return 'complete'
    except Exception:
        state['atlas'] = {**state.get('atlas', {}), 'status': 'failed'}
        raise


def publish_reused_folder(session, folder, source_project_id, source_folder, next_folder=None):
    """Copy one confirmed complete folder in SQL, without loading or calling agents.

    Inputs: destination session/folder and the chosen source project/folder. Returns:
    publication dict. New tables, remapped codebook references and the registry commit
    together; a retry reads that registry rather than copying twice. Source rows stay intact.
    """
    from v_5.main_functions.sql import atlas_transaction
    from v_5.main_functions.assets import source_project
    state = session.state
    _, source_state = source_project(session.path, source_project_id, state.get('user_id', '9999'))
    # This persistent form choice is also the copy specification used after interruption.
    choice = state['folders'][folder]['reuse_dataset']
    if (choice['project_id'], choice['folder']) != (source_project_id, source_folder):
        raise ValueError('The selected folder copy changed.')
    # Keep the reusable context before SQL commits, including on a receipt-only retry.
    state.setdefault('preparations', {})[folder] = deepcopy(source_state.get('preparations', {}).get(source_folder, {}))
    source_notes = {**deepcopy(source_state.get('folder_notes', {}).get(source_folder, {})), 'folder': folder}
    for file in state['files'].values():
        reuse = file.get('reuse_codebook') or {}
        old_file = source_state['files'].get(reuse.get('file_id'), {})
        if file['folder'] == folder and old_file.get('filename') == source_notes.get('primary_codebook'):
            source_notes['primary_codebook'] = file['filename']
    state.setdefault('folder_notes', {}).setdefault(folder, source_notes)
    state.setdefault('descriptive_reviews', {})[folder] = deepcopy(source_state.get('descriptive_reviews', {}).get(source_folder, {}))
    session.save()
    with atlas_transaction() as cursor:
        ensure_registry(cursor)
        previous = existing_publication(cursor, state['id'])
        if any(item['folder'] == folder for item in (previous or {}).get('data', [])):
            state['atlas'] = previous
            session.save()
            return previous
        cursor.execute('SELECT publication FROM public.atlas_projects WHERE project_id=%s AND user_id=%s FOR SHARE',
                       (source_project_id, str(state.get('user_id', '9999'))))
        row = cursor.fetchone()
        source = row[0] if row else None
        original_tables = [item for item in (source or {}).get('data', []) if item['folder'] == source_folder]
        if not original_tables:
            raise ValueError('The selected source folder is no longer published.')
        source_dataset = original_tables[0]['dataset_id']
        number = 1 + max((int(t['dataset_id'].removeprefix('dataset_')) for t in (previous or {}).get('data', [])), default=0)
        dataset_id = f'dataset_{number:03d}'
        label = state['folders'][folder].get('display_name') or folder
        used = {CORE, UNMATCHED, SOFT_MATCHED, INFO, GUIDE, *(t['name'] for t in (previous or {}).get('data', [])),
                *(t['name'] for t in (previous or {}).get('ingested_codebooks', []))}
        data, table_map = [], {}
        for index, item in enumerate(original_tables, 1):
            name = item['name']
            if name in used:
                suffix = f'_{dataset_id}_{index}'
                name = name.encode('utf-8')[:63 - len(suffix)].decode('utf-8', errors='ignore') + suffix
            if name in used:
                raise ValueError('The destination table name is already used.')
            used.add(name)
            table_map[item['name']] = name
            data.append({'folder': folder, 'dataset_id': dataset_id, 'dataset_name': label,
                         'name': name, 'row_count': item['row_count'],
                         'copy_sql': {'project_id': source_project_id, 'folder': source_folder, 'table': item['name']}})

        def definitions(name):
            """Read source definitions and remap physical table references to this folder."""
            fields = list(CODEBOOK_FIELDS if name == CORE else SOURCE_CODEBOOK_FIELDS)
            if name == SOFT_MATCHED and name not in source.get('codebook', {}):
                return [], []
            columns = [key for key in fields if key != 'soft_match' or SOFT_MATCHED in source.get('codebook', {})]
            columns += ['embedding'] if name == CORE else []
            cursor.execute(sql.SQL('SELECT {} FROM {} WHERE dataset_id=%s ORDER BY variable_name').format(
                sql.SQL(', ').join(map(identifier, columns)), project_table(source_project_id, name)), (source_dataset,))
            values, embeddings = [], []
            for record in cursor.fetchall():
                row = dict(zip(columns, record))
                row['table_name'] = table_map.get(row['table_name'], row['table_name'])
                row['table_names'] = [table_map.get(t, t) for t in row['table_names']] if row['table_names'] else row['table_names']
                row['battery_members'] = json.dumps(row['battery_members'] or [])
                if name != CORE:
                    row['soft_match'] = json.dumps(row.get('soft_match') or [])
                values.append((dataset_id, *(row[key] for key in fields)))
                if name == CORE:
                    embeddings.append(row['embedding'])
            return values, embeddings

        core, embeddings = definitions(CORE)
        unmatched, _ = definitions(UNMATCHED)
        soft_matched, _ = definitions(SOFT_MATCHED)
        cursor.execute(sql.SQL('SELECT source_filename, content FROM {} WHERE dataset_id=%s').format(
            project_table(source_project_id, INFO)), (source_dataset,))
        info = [(dataset_id, *item) for item in cursor.fetchall()]
        cursor.execute(sql.SQL('SELECT sections FROM {}').format(project_table(source_project_id, GUIDE)))
        old_guide = cursor.fetchone()[0]
        notes = next((item['notes'] for item in old_guide['datasets'] if item['dataset_id'] == source_dataset), '')
        guide = {'project': '\n\n'.join(filter(None, [state['context']['name'], state['context'].get('description', '')])),
                 'datasets': [{'dataset_id': dataset_id, 'notes': f'## {label}\nCopied from {source_state["context"]["name"]} / {source_folder}.\n\n{notes}'}]}
        books = {}
        for file_id, file in state['files'].items():
            if file['folder'] != folder or not file.get('reuse_codebook') or file.get('ignore'):
                continue
            reuse = file['reuse_codebook']
            entry = next((b for b in source.get('ingested_codebooks', []) if b['file_id'] == reuse['file_id']), None)
            if entry is None:
                raise ValueError('The copied folder’s ingested codebook is no longer available.')
            books[file_id] = {'filename': file['filename'], 'file_id': file_id,
                             'copy_sql': {'project_id': source_project_id, 'name': entry['name']},
                             'notes_text': entry.get('notes', ''), 'primary': entry.get('primary', False)}
        state.setdefault('codebooks', {})[folder] = {'folder': folder, 'source_tables': books,
                                                    'reused_from': {'project_id': source_project_id, 'folder': source_folder}}
        session.save()
        publication = write_publication(cursor, session, session.run_path.name,
                                        (data, core, unmatched, soft_matched, info, guide), embeddings, previous)
    state['atlas'] = publication
    state.pop('loader_resume', None)
    state.pop('codebook_resume', None)
    session.save()
    session.message('atlas', f'Ingestion is complete for {label}. Reused the saved dataset and codebooks.',
                    'folder_complete', {'folder': folder, 'release': publication['release']})
    return publication
