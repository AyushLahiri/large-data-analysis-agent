from contextlib import closing, contextmanager
import re
from psycopg2 import sql
from psycopg2.extras import Json
from v_5.app.database import get_db_connection


EMBEDDING_MODEL = 'text-embedding-3-large'


DIMENSIONS = 2000


CORE, UNMATCHED, INFO, GUIDE = 'codebook_core', 'codebook_unmatched', 'general_information', 'ingestion_guide'
SOFT_MATCHED = 'codebook_soft_matched'


PROJECT_REGISTRY_SQL = '''CREATE TABLE IF NOT EXISTS public.atlas_projects (
    project_id text PRIMARY KEY, project_name text NOT NULL, release text,
    publication jsonb, published_at timestamptz, user_id text NOT NULL DEFAULT '9999'
)'''


PROJECT_SUMMARY_SQL = '''CREATE TABLE IF NOT EXISTS public.atlas_project_summaries (
    project_id text PRIMARY KEY REFERENCES public.atlas_projects(project_id) ON DELETE CASCADE,
    summary_md text NOT NULL
)'''


CODEBOOK_FIELDS = {
    'variable_name': 'text NOT NULL', 'pct_missing': 'double precision', 'variable_type': 'text',
    'variable_label': 'text', 'variable_description': 'text', 'other_information': 'text',
    'battery_root': 'text', 'battery_members': 'jsonb', 'battery_summary': 'text', 'categories': 'text',
    'theme': 'text', 'subtheme': 'text', 'original_variable_name': 'text', 'table_names': 'text[]',
    'table_name': 'text', 'column_name': 'text',
}
SOURCE_CODEBOOK_FIELDS = {**CODEBOOK_FIELDS, 'soft_match': 'jsonb'}


def published_folders(state):
    """Find folders already represented in Atlas.

    Inputs: state: project state with optional atlas.data table records.
    Returns: Set of folder keys; empty if nothing is published.
    Details: Used to protect published inputs and decide which folders remain to ingest.
    """
    return {table['folder'] for table in state.get('atlas', {}).get('data', [])}


def identifier(name):
    """Validate and quote one PostgreSQL identifier.

    Inputs: name: schema, table or column name.
    Returns: psycopg SQL Identifier; raises ValueError for empty, overlong or
        null-containing names.
    Details: Shared by SQL builders. The length check uses UTF-8 bytes to match PostgreSQL's
        identifier limit.
    """
    if not name or len(name.encode('utf-8')) > 63 or '\0' in name:
        raise ValueError(f'Invalid PostgreSQL name: {name!r}.')
    return sql.Identifier(name)


def existing_publication(cursor, schema):
    """Read and lock the project publication record.

    Inputs: cursor: active transaction cursor; schema: project/schema ID.
    Returns: Publication dict or None; raises if the registry and schema disagree.
    Details: Used before publishing or summarizing. The row lock prevents overlapping
        publication changes within the transaction.
    """
    cursor.execute('SELECT publication FROM public.atlas_projects WHERE project_id = %s FOR UPDATE', (schema,))
    row = cursor.fetchone()
    publication = row[0] if row else None
    cursor.execute('SELECT to_regnamespace(%s)', (f'"{schema}"',))
    namespace = cursor.fetchone()[0]
    if namespace and not publication:
        raise ValueError('A schema already uses this project ID in Atlas. Choose a different project ID.')
    if publication and not namespace:
        raise ValueError('The registered Atlas schema is missing.')
    return publication


def ensure_registry(cursor):
    """Add the single admin owner to new and existing project registry rows."""
    cursor.execute(PROJECT_REGISTRY_SQL)
    cursor.execute("ALTER TABLE public.atlas_projects ADD COLUMN IF NOT EXISTS user_id text NOT NULL DEFAULT '9999'")


@contextmanager
def atlas_transaction():
    """Open a transaction against the Atlas database.

    Inputs: Uses get_db_connection; no arguments.
    Returns: Context manager yielding a cursor; commits on success, rolls back on error and
        closes the connection.
    Details: Shared by SQL asset helpers. Verifies the database name before yielding
        control.
    """
    with closing(get_db_connection()) as connection, connection, connection.cursor() as cursor:
        cursor.execute('SELECT current_database()')
        if cursor.fetchone()[0] != 'atlas':
            raise ValueError('The connection must point to the atlas database.')
        yield cursor


def rename_atlas_project(project_id, name):
    """Create or update a project's name in the Atlas registry.

    Inputs: project_id: stable project key; name: display name.
    Returns: None; inserts or updates the registry row.
    Details: Called by project creation/rename routes through atlas_transaction.
    """
    with atlas_transaction() as cursor:
        ensure_registry(cursor)
        cursor.execute('''INSERT INTO public.atlas_projects (project_id, project_name) VALUES (%s, %s)
            ON CONFLICT (project_id) DO UPDATE SET project_name = EXCLUDED.project_name''', (project_id, name))


def delete_atlas_project(project_id):
    """Delete a project schema and every registry row owned by that project.

    Inputs: project_id: schema and registry key.
    Returns: None; drops its data/codebook tables, indexes and schema. Deleting the
        registry row cascades to summaries, join rules, chats, turns and chat sessions.
    Details: Both changes share one transaction. Only generated project IDs are accepted,
        so a mistaken caller cannot name a shared schema such as public.
    """
    if not isinstance(project_id, str) or not re.fullmatch('[0-9a-f]{32}', project_id):
        raise ValueError('Invalid project ID for deletion.')
    with atlas_transaction() as cursor:
        cursor.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(identifier(project_id)))
        cursor.execute('DELETE FROM public.atlas_projects WHERE project_id = %s', (project_id,))


def project_table(project_id, name):
    """Build a safely quoted project-qualified table reference.

    Inputs: project_id: schema name; name: table name.
    Returns: Composable SQL fragment for schema.table.
    Details: Uses identifier for both parts; shared by ingestion, analysis and asset
        management.
    """
    return sql.SQL('{}.{}').format(identifier(project_id), identifier(name))


def guide_summary(guide):
    """Flatten the structured ingestion guide into readable text.

    Inputs: guide: dict with project text and datasets containing notes.
    Returns: Paragraph-separated project and dataset notes.
    Details: Used when publication or folder deletion updates the guide table.
    """
    return '\n\n'.join([guide['project'], *(dataset['notes'] for dataset in guide['datasets'])])


def delete_atlas_folder(project_id, folder):
    """Remove one published folder and its associated SQL assets.

    When: A caller requests SQL dataset cleanup; the current UI blocks deletion of published
        folders.
    Inputs: project_id: project/schema key; folder: stable folder key.
    Returns: Updated publication dict, or None when no published data remains.
    Flow: Drops that folder's tables, removes codebook/reference rows, refreshes
        guide/counts and invalidates the summary. Drops the schema only when its last
        dataset is removed.
    """
    with atlas_transaction() as cursor:
        cursor.execute('SELECT publication FROM public.atlas_projects WHERE project_id = %s FOR UPDATE', (project_id,))
        row = cursor.fetchone()
        publication = row[0] if row else None
        if not publication:
            return None
        removed = [item for item in publication['data'] if item['folder'] == folder]
        if not removed:
            return publication
        cursor.execute(PROJECT_SUMMARY_SQL)
        cursor.execute('DELETE FROM public.atlas_project_summaries WHERE project_id = %s', (project_id,))
        publication['data'] = [item for item in publication['data'] if item['folder'] != folder]
        if not publication['data']:
            cursor.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(identifier(project_id)))
            cursor.execute('''UPDATE public.atlas_projects SET release = NULL, publication = NULL, published_at = NULL
                WHERE project_id = %s''', (project_id,))
            return None

        # One logical folder may occupy several data tables; delete its shared metadata once by dataset ID.
        dataset_ids = list({item['dataset_id'] for item in removed})
        retained_books = []
        for item in publication.get('ingested_codebooks', []):
            if item['folder'] == folder:
                cursor.execute(sql.SQL('DROP TABLE {}').format(project_table(project_id, item['name'])))
            else:
                retained_books.append(item)
        publication['ingested_codebooks'] = retained_books
        for item in removed:
            cursor.execute(sql.SQL('DROP TABLE {}').format(project_table(project_id, item['name'])))
        for name in (CORE, UNMATCHED, SOFT_MATCHED, INFO):
            if name == SOFT_MATCHED and name not in publication.get('codebook', {}):
                continue
            table = project_table(project_id, name)
            cursor.execute(sql.SQL('DELETE FROM {} WHERE dataset_id = ANY(%s)').format(table), (dataset_ids,))
            counts = publication['information' if name == INFO else 'codebook']
            counts[name] -= cursor.rowcount
        cursor.execute(sql.SQL('SELECT count(embedding) FROM {}').format(project_table(project_id, CORE)))
        publication['embeddings'] = cursor.fetchone()[0]
        table = project_table(project_id, GUIDE)
        cursor.execute(sql.SQL('SELECT sections FROM {}').format(table))
        guide = cursor.fetchone()[0]
        guide['datasets'] = [section for section in guide['datasets'] if section['dataset_id'] not in dataset_ids]
        cursor.execute(sql.SQL('UPDATE {} SET summary = %s, sections = %s').format(table), (guide_summary(guide), Json(guide)))
        cursor.execute('UPDATE public.atlas_projects SET publication = %s WHERE project_id = %s', (Json(publication), project_id))
        return publication


def fetch_ingested_codebook(project_id, file_id, user_id='9999'):
    """Read a completed individual codebook and its source notes for exact-file reuse.

    Inputs: owning project/file IDs and user. Returns: unsuffixed text DataFrame and
    notes. Publication metadata supplies the table name; callers verify the raw hash.
    """
    import pandas as pd
    from v_5.ingest.codebook_ingestion.tables import COLUMNS
    with atlas_transaction() as cursor:
        ensure_registry(cursor)
        cursor.execute('SELECT publication FROM public.atlas_projects WHERE project_id=%s AND user_id=%s',
                       (project_id, str(user_id)))
        record = cursor.fetchone()
        entry = next((item for item in (record[0] if record and record[0] else {}).get('ingested_codebooks', [])
                      if item['file_id'] == file_id), None)
        if entry is None:
            raise ValueError('This source codebook has no completed SQL table.')
        cursor.execute(sql.SQL('SELECT {} FROM {} ORDER BY variable_name').format(
            sql.SQL(', ').join(map(identifier, COLUMNS)), project_table(project_id, entry['name'])))
        return pd.DataFrame(cursor.fetchall(), columns=COLUMNS), entry.get('notes', '')
