import hashlib
import json
import re
from psycopg2.extras import Json
from v_5.analyse.sql_assets.catalog import read_atlas
from v_5.main_functions.sql import atlas_transaction


JOIN_SKILL = 'cross_folder_joins'


ID_NAMES = re.compile(
    r'(?:^|_)(?:id|identification|identifier|line|household|region|block|cluster|psu|serial)(?:_|\d|$)'
    r'|(?:id|identifier|number|num|no)$', re.IGNORECASE,
)


def candidate_identifiers(catalog):
    """Suggest columns worth inspecting as join keys.

    Inputs: catalog: scoped folders, tables, columns and primary keys.
    Returns: Folder-to-table-list mapping, each table carrying candidate column names.
    Details: Included when the analyst reads the cross-folder join skill. Names and declared
        keys are hints, not proof of a valid relationship.
    """
    return {
        folder: [
            {'table': table['name'], 'columns': [
                column for column in catalog.columns[table['name']]
                if column in table['primary_key'] or ID_NAMES.search(column)
            ]}
            for table in dataset['tables']
        ]
        for folder, dataset in catalog.folders.items()
    }


def read_join_rules(catalog):
    """Read saved join discoveries that fit the current scope.

    Inputs: catalog: project ID, scoped folders and optional session.
    Returns: Newest-first list of folders/rules records, or empty if no rules table exists.
    Details: Used by the join skill; returns only rules whose folder set is contained in the
        selected scope.
    """
    with read_atlas(session=catalog.session) as (_, cursor):
        cursor.execute("SELECT to_regclass('public.atlas_join_rules')")
        if cursor.fetchone()[0] is None:
            return []
        cursor.execute('''SELECT folders, rules FROM public.atlas_join_rules
            WHERE project_id = %s AND folders <@ %s::text[] ORDER BY recorded_at DESC''',
                       (catalog.schema, list(catalog.folders)))
        return [{'folders': folders, 'rules': rules} for folders, rules in cursor.fetchall()]


def save_join_rules(project_id, folders, rules):
    """Persist a confirmed set of join notes without duplicates.

    Inputs: project_id: owner; folders: participating keys; rules: concise discovered notes.
    Returns: None; creates the storage table if necessary and inserts a fingerprinted
        record.
    Details: Called after a successful analyst handoff. A transaction lock coordinates
        first-time setup; identical payloads do not create another row.
    """
    fingerprint = hashlib.sha256(json.dumps([folders, rules], ensure_ascii=False).encode()).hexdigest()
    with atlas_transaction() as cursor:
        cursor.execute("SELECT to_regclass('public.atlas_join_rules')")
        if cursor.fetchone()[0] is None:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtext('v5 atlas joins')::bigint)")
            cursor.execute('''CREATE TABLE IF NOT EXISTS public.atlas_join_rules (
                project_id text NOT NULL REFERENCES public.atlas_projects(project_id) ON DELETE CASCADE,
                fingerprint text NOT NULL,
                folders text[] NOT NULL,
                rules jsonb NOT NULL,
                recorded_at timestamptz NOT NULL DEFAULT now(),
                PRIMARY KEY (project_id, fingerprint)
            )''')
        cursor.execute('''INSERT INTO public.atlas_join_rules (project_id, fingerprint, folders, rules)
            VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING''',
                       (project_id, fingerprint, folders, Json(rules)))
