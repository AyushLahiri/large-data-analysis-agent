import json
import re
from collections import defaultdict
from contextlib import closing, contextmanager, nullcontext
from psycopg2 import sql
from psycopg2.extras import RealDictCursor
from random import Random
from v_5.app.database import get_db_connection
from v_5.main_functions.sql import (
    CODEBOOK_FIELDS, CORE, DIMENSIONS, EMBEDDING_MODEL, GUIDE, INFO, UNMATCHED, SOFT_MATCHED, identifier, project_table,
)


VARIABLE_FIELDS = (
    'variable_name', 'column_name', 'variable_label', 'variable_description', 'variable_type',
    'pct_missing', 'theme', 'subtheme', 'categories', 'other_information',
    'battery_members', 'battery_summary', 'table_name', 'table_names',
)


BATTERY_FILTER = sql.SQL("jsonb_typeof(battery_members) = 'array' AND battery_members <> '[]'::jsonb")


@contextmanager
def read_atlas(*, vector=False, session=None):
    """Open a bounded, read-only Atlas transaction.

    Inputs: vector: allow public extension functions; session: optional cancellation owner.
    Returns: Context manager yielding (connection, cursor); closes both after use.
    Details: Shared by analysis queries and metadata readers. Uses repeatable-read
        isolation, timeouts, a restricted search path and an Atlas database check.
    """
    if session is not None:
        session.check_stop()
    with closing(get_db_connection()) as connection:
        connection.set_session(readonly=True, isolation_level='REPEATABLE READ')
        cancel = session.cancellable(connection.cancel) if session is not None else nullcontext()
        with connection, connection.cursor() as cursor, cancel:
            cursor.execute("SET LOCAL statement_timeout = '25s'")
            cursor.execute("SET LOCAL lock_timeout = '3s'")
            cursor.execute('SET LOCAL search_path TO pg_catalog, public' if vector else 'SET LOCAL search_path TO pg_catalog')
            cursor.execute('SELECT current_database()')
            if cursor.fetchone()[0] != 'atlas':
                raise ValueError('The connection must point to the atlas database.')
            if session is not None:
                session.check_stop()
            yield connection, cursor


def category_preview(text):
    """Shorten documented category labels for search results.

    Inputs: text: newline- or HTML-break-separated category descriptions.
    Returns: Up to five nonempty lines, with an option-count note if longer; empty for
        absent evidence.
    Details: Used by candidate_rows so variable discovery stays compact.
    """
    lines = [line.strip() for line in re.split(r'\n|<br\s*/?>', text or '', flags=re.I) if line.strip()]
    if not lines or lines == ['not in source']:
        return ''
    return '\n'.join(lines[:5]) + (f'\n… {len(lines)} options total' if len(lines) > 5 else '')


def candidate_rows(rows):
    """Condense codebook matches into a small candidate list.

    Inputs: rows: ranked codebook records, including optional battery membership.
    Returns: At most ten candidate dicts with short category previews, missingness, source
        notes and grouped battery descriptions.
    Details: VariableSearch uses the first thirty hits, coalesces members of the same
        battery and retains locations needed for later exact selection.
    """
    candidates, seen = [], set()
    for row in rows[:30]:
        members = row.get('battery_members') or []
        name = row['variable_name']
        if members:
            members = sorted(set([name, *members]))
        key = tuple(members) if members else (name,)
        if key in seen:
            continue
        seen.add(key)
        out = {key: row.get(key) for key in VARIABLE_FIELDS if key not in {'battery_members', 'battery_summary'}}
        out['categories'] = category_preview(out['categories'])
        if (out['other_information'] or '').strip().casefold() == 'not in source':
            out['other_information'] = ''
        if members:
            out['battery_members'] = members
            out['variable_description'] = row.get('battery_summary') or out['variable_description']
        candidates.append(out)
    top_15 = candidates[:15]
    return top_15[:10]


def category_labels(text):
    """Parse documented code-to-label pairs.

    Inputs: text: category lines using colon, equals or spaced dash separators.
    Returns: Mapping from code strings to label strings.
    Details: Used by label_tables to replace result codes only when the available mappings
        agree.
    """
    labels = {}
    for line in re.split(r'\n|<br\s*/?>', text or '', flags=re.I):
        parts = re.split(r'\s+[-–—]\s+|\s*[:=]\s*', line.strip(), maxsplit=1)
        if len(parts) == 2 and all(parts):
            labels[parts[0]] = parts[1]
    return labels


def codebook_themes(cursor, schema, dataset_ids):
    """Read the available themes for selected dataset IDs.

    Inputs: cursor: active SQL cursor; schema: project; dataset_ids: scoped IDs.
    Returns: Dataset-ID-to-theme-list dict; empty if no core codebook table exists.
    Details: Shared by AtlasCatalog and orchestrator scope discovery; checks table presence
        before querying.
    """
    cursor.execute('SELECT to_regclass(%s)', (f'"{schema}"."{CORE}"',))
    if not cursor.fetchone()[0]:
        return {}
    cursor.execute(sql.SQL('SELECT DISTINCT dataset_id, theme FROM {} WHERE dataset_id=ANY(%s) ORDER BY 1,2')
                   .format(project_table(schema, CORE)), (dataset_ids,))
    themes = {}
    for dataset_id, theme in cursor.fetchall():
        labels = themes.setdefault(dataset_id, [])
        if theme:
            labels.append(theme)
    return themes


class AtlasCatalog:
    def __init__(self, state, selected_folder, *, session=None):
        """Load published data structure for a project or folder.

        Inputs: state: project ID/publication owner; selected_folder: key or blank; session:
            optional cancellation owner.
        Returns: None; fills folders, columns, key metadata, themes and a variable cache.
        Details: Used when analysis begins and when rendering assets. Reads actual SQL metadata
            and raises if the chosen scope has no published dataset.
        """
        self.schema = state['id']
        self.session = session
        self.folders, self.columns = {}, defaultdict(dict)
        self._codebook_by_column = defaultdict(dict)
        with read_atlas(session=self.session) as (_, cursor):
            cursor.execute('SELECT publication FROM public.atlas_projects WHERE project_id = %s', (self.schema,))
            row = cursor.fetchone()
            publication = (row and row[0]) or {}
            for item in publication.get('data', []):
                folder = item['folder']
                if selected_folder and folder != selected_folder:
                    continue
                self.folders.setdefault(folder, {'name': item['dataset_name'], 'dataset_id': item['dataset_id'],
                                                  'tables': [], 'themes': [], 'has_codebook': False})
                self.folders[folder]['tables'].append({'name': item['name'], 'rows': item['row_count'], 'primary_key': []})
            if not self.folders:
                raise ValueError('There is no published dataset in the selected scope.')
            tables = {item['name']: item for f in self.folders.values() for item in f['tables']}
            cursor.execute('''SELECT table_name, column_name, data_type FROM information_schema.columns
                WHERE table_schema=%s AND table_name=ANY(%s) ORDER BY table_name, ordinal_position''',
                           (self.schema, list(tables)))
            for table, column, kind in cursor.fetchall():
                self.columns[table][column] = kind
            cursor.execute('''SELECT t.relname, a.attname FROM pg_index i
                JOIN pg_class t ON t.oid = i.indrelid
                JOIN pg_namespace s ON s.oid = t.relnamespace
                JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(i.indkey)
                WHERE s.nspname=%s AND t.relname=ANY(%s) AND i.indisprimary
                ORDER BY t.relname, array_position(i.indkey::smallint[], a.attnum)''', (self.schema, list(tables)))
            for table, column in cursor.fetchall():
                tables[table]['primary_key'].append(column)
            for name, table in tables.items():
                table['column_count'] = len(self.columns[name])
                table['synthetic_primary_key'] = [key.casefold() for key in table['primary_key']] == ['ow_id']
            themes = codebook_themes(cursor, self.schema, [folder['dataset_id'] for folder in self.folders.values()])
            for folder in self.folders.values():
                dataset_id = folder['dataset_id']
                folder['has_codebook'] = dataset_id in themes
                folder['themes'] = themes.get(dataset_id, [])

    def context(self):
        """Expose the catalog structure needed by analysis agents.

        Inputs: Uses the loaded catalog; no arguments.
        Returns: Dict with schema, folders and available codebook field names.
        Details: Sent to the analyst and SQL reviewer; provides structure without loading data
            rows.
        """
        fields = ['dataset_id', *CODEBOOK_FIELDS, 'embedding'] if any(f['has_codebook'] for f in self.folders.values()) else []
        return {'schema': self.schema, 'folders': self.folders, 'codebook_fields': fields}

    def subthemes(self, folder, theme):
        """List subthemes under one dataset theme.

        Inputs: folder: scoped key; theme: selected theme.
        Returns: Sorted list of nonempty subtheme strings.
        Details: VariableSearch.get_subthemes calls this before ordinary theme-constrained
            search.
        """
        with read_atlas(session=self.session) as (_, cursor):
            cursor.execute(sql.SQL("""SELECT DISTINCT subtheme FROM {}
                WHERE dataset_id=%s AND theme=%s AND subtheme <> '' ORDER BY subtheme""")
                           .format(project_table(self.schema, CORE)), (self.folders[folder]['dataset_id'], theme))
            return [subtheme for subtheme, in cursor.fetchall()]

    def battery_topics(self, folder, theme):
        """Find themes containing documented battery variables.

        Inputs: folder: scoped key; theme: optional theme filter.
        Returns: Theme-to-subtheme-list dict, or empty when no codebook exists.
        Details: Used for battery-only discovery; filters on nonempty array-valued
            battery_members.
        """
        data = self.folders[folder]
        if not data['has_codebook']:
            return {}
        with read_atlas(session=self.session) as (_, cursor):
            cursor.execute(sql.SQL('''SELECT DISTINCT theme, subtheme FROM {}
                WHERE dataset_id=%s AND theme <> '' AND (%s='' OR theme=%s) AND {}
                ORDER BY theme, subtheme''').format(project_table(self.schema, CORE), BATTERY_FILTER),
                           (data['dataset_id'], theme, theme))
            topics = {}
            for name, subtheme in cursor.fetchall():
                subthemes = topics.setdefault(name, [])
                if subtheme:
                    subthemes.append(subtheme)
        return topics

    def artifact_tree(self):
        """Describe the published assets shown in the browser tree.

        Inputs: Uses scoped folders and database metadata; no arguments.
        Returns: Folder-keyed dict of row/column counts and codebook/unmatched/information/guide
            availability.
        Details: Called by the analysis tree route. Counts logical variables separately from
            split table parts.
        """
        references = [CORE, UNMATCHED, SOFT_MATCHED, INFO, GUIDE]
        counts = {}
        with read_atlas(session=self.session) as (_, cursor):
            cursor.execute('SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_name=ANY(%s)',
                           (self.schema, references))
            available = {name for name, in cursor.fetchall()}
            for name in (CORE, UNMATCHED, SOFT_MATCHED, INFO):
                if name in available:
                    cursor.execute(sql.SQL('SELECT dataset_id, count(*) FROM {} GROUP BY dataset_id')
                                   .format(project_table(self.schema, name)))
                    counts[name] = dict(cursor.fetchall())
        tree = {}
        for key, folder in self.folders.items():
            dataset_id = folder['dataset_id']
            tables = folder['tables']
            logical_columns = {name.casefold() for table in tables for name in self.columns[table['name']]}
            tree[key] = {
                'row_count': tables[0]['rows'],
                'column_count': counts.get(CORE, {}).get(dataset_id, len(logical_columns)),
                'codebook': bool(counts.get(CORE, {}).get(dataset_id)),
                'unmatched': bool(counts.get(UNMATCHED, {}).get(dataset_id)),
                'soft_matched': bool(counts.get(SOFT_MATCHED, {}).get(dataset_id)),
                'information': bool(counts.get(INFO, {}).get(dataset_id)),
                'guide': GUIDE in available,
            }
        return tree

    def column_matches(self, folder, keyword, *, exact=False):
        """Find variables by their actual data-column names.

        Inputs: folder: scoped key; keyword: search text; exact: require a full name match.
        Returns: List of variable/column/type records with all containing table names.
        Details: Fallback when codebook evidence is absent. Searches case-insensitively and
            coalesces repeated keys across table parts.
        """
        data, seen = self.folders[folder], {}
        keyword = keyword.casefold().strip()
        for table in data['tables']:
            for name, kind in self.columns[table['name']].items():
                key = name.casefold()
                if key == keyword or (not exact and keyword in key):
                    item = seen.setdefault(key, {'variable_name': key, 'column_name': name, 'variable_type': kind,
                                                  'table_name': table['name'], 'table_names': []})
                    item['table_names'].append(table['name'])
        return list(seen.values())

    def codebook(self, folder, condition, parameters, suffix=sql.SQL(''), *, vector=False):
        """Query scoped codebook records and cache their column definitions.

        Inputs: folder: dataset; condition/suffix: trusted SQL fragments; parameters: bound
            values; vector: extension access flag.
        Returns: List of VARIABLE_FIELDS dicts, or empty when no codebook exists.
        Details: Shared by exact lookup, semantic search and reviewer evidence. Always
            constrains dataset_id and updates the per-column cache.
        """
        data = self.folders[folder]
        if not data['has_codebook']:
            return []
        fields = sql.SQL(', ').join(map(identifier, VARIABLE_FIELDS))
        statement = sql.SQL('SELECT {} FROM {} WHERE dataset_id=%s AND ({}) {}').format(
            fields, project_table(self.schema, CORE), condition, suffix)
        with read_atlas(vector=vector, session=self.session) as (connection, _), connection.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(statement, (data['dataset_id'], *parameters))
            rows = [dict(row) for row in cursor.fetchall()]
        self._codebook_by_column[folder].update((row['column_name'], row) for row in rows if row['column_name'])
        return rows

    def review_variables(self, folder, columns_by_table):
        """Collect definitions for the columns a SQL query uses.

        Inputs: folder: scoped key; columns_by_table: table-to-column-list mapping.
        Returns: List of table/column/type records with available names, labels, descriptions
            and categories.
        Details: Supplies analysis subagent sql_reviewer. Loads missing codebook entries once
            and omits not in source placeholders.
        """
        known = self._codebook_by_column[folder]
        names = {column for columns in columns_by_table.values() for column in columns}
        missing = sorted(names - known.keys())
        if missing:
            self.codebook(folder, sql.SQL('column_name = ANY(%s)'), (missing,))
            for column in missing:
                known.setdefault(column, {})
        variables = []
        for table, columns in columns_by_table.items():
            for column in columns:
                entry = {'table_name': table, 'column_name': column, 'variable_type': self.columns[table][column]}
                row = known[column]
                for field in ('variable_name', 'variable_label', 'variable_description', 'categories'):
                    value = row.get(field)
                    if value and value.strip().casefold() != 'not in source':
                        entry[field] = value
                variables.append(entry)
        return variables

    def exact(self, folder, name):
        """Resolve a variable by documented or prepared name.

        Inputs: folder: scoped key; name: normalized exact name.
        Returns: Codebook matches, or actual column matches with empty battery membership.
        Details: Used for explicit variables, weights and Python frame extraction before falling
            back to name-only metadata.
        """
        rows = self.codebook(folder, sql.SQL('variable_name = %s OR lower(column_name) = %s OR lower(original_variable_name) = %s'),
                             (name, name, name))
        return rows or [{**row, 'battery_members': []} for row in self.column_matches(folder, name, exact=True)]

    def sample_columns(self, folder):
        """Sample column names when a folder has no codebook.

        Inputs: folder: scoped key.
        Returns: Reproducible list of at most fifteen column names; raises if a codebook exists.
        Details: Analyst discovery fallback; deduplicates columns across table parts before
            sampling.
        """
        if self.folders[folder]['has_codebook']:
            raise ValueError('This folder has a codebook. Search that first.')
        names = list(dict.fromkeys(name for table in self.folders[folder]['tables'] for name in self.columns[table['name']]))
        return Random(17).sample(names, min(15, len(names)))

    def label_tables(self, tables, folders):
        """Replace categorical result codes with unambiguous labels.

        Inputs: tables: mutable columns/rows result dicts; folders: datasets used by the
            calculation.
        Returns: None; updates table cells in place where a matching label exists.
        Details: Runs before the final analyst handoff. Matches headers to documented variables
            and only applies mappings that agree across sources.
        """
        headers = list({header.strip().casefold() for table in tables for header in table['columns']})
        if not headers:
            return
        mappings = defaultdict(list)
        for folder in folders:
            rows = self.codebook(folder, sql.SQL('variable_name=ANY(%s) OR lower(column_name)=ANY(%s) OR lower(variable_label)=ANY(%s)'),
                                 (headers, headers, headers))
            for row in rows:
                labels = category_labels(row['categories'])
                if labels:
                    for key in ('variable_name', 'column_name', 'variable_label'):
                        if row[key]:
                            mappings[row[key].strip().casefold()].append(labels)
        for table in tables:
            for index, header in enumerate(table['columns']):
                choices = mappings[header.strip().casefold()]
                # A shared header may match several datasets; do not relabel codes when their definitions disagree.
                if choices and all(choice == choices[0] for choice in choices):
                    for row in table['rows']:
                        value = row[index]
                        code = str(value)
                        if isinstance(value, float) and value.is_integer():
                            code = str(int(value))
                        row[index] = choices[0].get(code, value)


class VariableSearch:
    def __init__(self, catalog, session):
        """Create per-question variable-search state.

        Inputs: catalog: scoped metadata; session: API/logging owner.
        Returns: None; initializes searched terms, weight/vector caches and selected themes.
        Details: Created by Analyst. Caches and search allowances apply to this analyst
            instance.
        """
        self.catalog, self.session = catalog, session
        self.searched = defaultdict(set)
        self.weights, self.vectors = {}, {}
        self.selected_themes = {}

    def show_all_battery(self, folder, groups):
        """Expand grouped battery candidates into their individual definitions.

        Inputs: folder: one scoped key or blank; groups: lists of member variable names.
        Returns: List of folder/battery_members/variables bundles.
        Details: Used after exact or grouped discovery. Queries full definitions rather than
            reusing the condensed battery summary.
        """
        if folder and folder not in self.catalog.folders:
            raise ValueError('That folder is outside the selected scope.')
        names = list(dict.fromkeys(name for members in groups for name in members))
        if not names:
            return []
        results = []
        for key in ([folder] if folder else self.catalog.folders):
            rows = self.catalog.codebook(key, sql.SQL('variable_name = ANY(%s)'), (names,))
            by_name = {row['variable_name']: {field: value for field, value in row.items()
                       if field not in {'battery_members', 'battery_summary'}} for row in rows}
            for members in groups:
                variables = [by_name[name] for name in members if name in by_name]
                if variables:
                    results.append({'folder': key, 'battery_members': members, 'variables': variables})
        return results

    def get_subthemes(self, selections):
        """Validate theme choices and expose their subthemes.

        Inputs: selections: folder/theme records with optional battery_only flag.
        Returns: List of selection results containing subthemes, battery themes or an error.
        Details: Used before ordinary variable search. Caches accepted theme selections and
            supports a separate battery-only path.
        """
        results = []
        for folder, theme, battery_only in dict.fromkeys(
                (item['folder'], item['theme'], item.get('battery_only', False)) for item in selections):
            selection = {'folder': folder, 'theme': theme}
            if folder not in self.catalog.folders:
                results.append({**selection, 'error': 'The folder is outside the selected scope.'})
                continue
            if battery_only:
                topics = self.catalog.battery_topics(folder, theme)
                selection['battery_only'] = True
                if theme:
                    selection['subthemes'] = topics.get(theme, [])
                else:
                    selection['themes'] = topics
                results.append(selection)
                continue
            if theme not in self.catalog.folders[folder]['themes']:
                results.append({**selection, 'error': 'This theme is absent in the selected folder.'})
                continue
            key = (folder, theme)
            if key not in self.selected_themes:
                self.selected_themes[key] = self.catalog.subthemes(folder, theme)
            results.append({**selection, 'subthemes': self.selected_themes[key]})
        return results

    def vector(self, keyword):
        """Embed a search phrase once per analyst instance.

        Inputs: keyword: normalized search text.
        Returns: JSON-encoded embedding vector string.
        Details: matches uses this for nearest-description search; the cancellable API call and
            usage are recorded through the session.
        """
        if keyword not in self.vectors:
            result = self.session.analysis_request(lambda client: client.embeddings.create(
                model=EMBEDDING_MODEL, dimensions=DIMENSIONS, input=keyword))
            self.vectors[keyword] = json.dumps(result.data[0].embedding)
            self.session.record('search_embedding', 'variable_picker', {'keyword': keyword, 'usage': result.usage.model_dump()})
        return self.vectors[keyword]

    def matches(self, folder, keyword, theme='', subtheme='', *, battery_only=False):
        """Search variable definitions within optional topic filters.

        Inputs: folder/keyword: search target; theme/subtheme: optional filters; battery_only:
            require battery membership.
        Returns: Condensed candidate records, at most ten.
        Details: Uses embedding similarity first, text fallback if no rows are returned, and
            column-name matching when no codebook exists.
        """
        if not self.catalog.folders[folder]['has_codebook']:
            return [] if battery_only else self.catalog.column_matches(folder, keyword)[:10]
        scope = sql.SQL("(%s='' OR theme=%s) AND (%s='' OR subtheme=%s)")
        if battery_only:
            scope += sql.SQL(' AND ') + BATTERY_FILTER
        scope_values = (theme, theme, subtheme, subtheme)
        vector = self.vector(keyword)
        rows = self.catalog.codebook(folder, scope + sql.SQL(' AND embedding IS NOT NULL'), (*scope_values, vector),
                                    sql.SQL('ORDER BY embedding <=> %s::vector LIMIT 30'), vector=True)
        if not rows:
            pattern = '%' + keyword.replace('_', ' ') + '%'
            rows = self.catalog.codebook(folder, scope + sql.SQL(" AND replace(concat_ws(' ',variable_name,variable_label,variable_description),'_',' ') ILIKE %s"),
                                         (*scope_values, pattern), sql.SQL('ORDER BY variable_name LIMIT 30'))
        return candidate_rows(rows)

    def weight_candidates(self, folder):
        """Find and cache plausible survey-weight variables.

        Inputs: folder: scoped key.
        Returns: List of at most ten candidate variable records.
        Details: Used before calculation. Tries exact and semantic survey_weights lookup, or
            weight/wgt column names without a codebook.
        """
        if folder not in self.weights:
            if self.catalog.folders[folder]['has_codebook']:
                exact = candidate_rows(self.catalog.exact(folder, 'survey_weights'))
                candidates = exact + self.matches(folder, 'survey_weights')
                self.weights[folder] = list({row['variable_name']: row for row in candidates}.values())[:10]
            else:
                self.weights[folder] = [row for word in ('weight', 'wgt') for row in self.catalog.column_matches(folder, word)][:10]
        return self.weights[folder]

    def rank_folders(self, term):
        """Order folders by how closely their metadata fits a search.

        Inputs: term: folder, theme, keyword and need fields.
        Returns: Folder keys ordered by descending relevance score.
        Details: Used when search does not name a folder; prefers the requested folder/theme and
            then shared words.
        """
        def score(folder):
            """Score one folder against the pending search request.

            Inputs: folder: candidate key; captures term and catalog.
            Returns: Tuple of explicit-folder match, theme match and shared-word count.
            Details: Sorting helper for rank_folders; larger tuple values come first.
            """
            data = self.catalog.folders[folder]
            words = set(re.findall(r'\w+', ' '.join([folder, data['name'], *data['themes']]).casefold()))
            terms = set(re.findall(r'\w+', f"{term['keyword']} {term['need']}".casefold()))
            return (folder == term['folder'], term['theme'] in data['themes'], len(words & terms))
        return sorted(self.catalog.folders, key=score, reverse=True)

    def search(self, terms):
        """Search for variables while enforcing scope and discovery limits.

        When: Analysis main agent requests search; this is a deterministic tool, not a separate
            model agent.
        Inputs: terms: keyword/need/folder/theme/subtheme records, with optional battery_only.
        Returns: Result list with candidates, weight_candidates and searches_left, or
            per-request errors.
        Flow: Validates prior theme selection, limits unique keywords per folder/theme, ranks
            unspecified folders and stops each request at its first useful candidate set.
        """
        results = []
        for term in terms:
            keyword = ' '.join(term['keyword'].casefold().split())
            if not keyword:
                continue
            folders = [term['folder']] if term['folder'] else self.rank_folders(term)
            for folder in folders:
                if folder not in self.catalog.folders:
                    results.append({'need': term['need'], 'error': 'The folder is outside the selected scope.'})
                    continue
                themes = self.catalog.folders[folder]['themes']
                theme, subtheme = term['theme'], term['subtheme']
                battery_only = term.get('battery_only', False)
                weight_search = keyword == 'survey_weights' and not theme and not subtheme and not battery_only
                error = ''
                if themes and not weight_search and not battery_only:
                    if theme not in themes:
                        error = 'Choose an available theme in this folder and request its subthemes first.'
                    elif (folder, theme) not in self.selected_themes:
                        error = 'Request the subthemes for this folder and theme before searching.'
                    else:
                        choices = self.selected_themes[folder, theme]
                        if (choices and subtheme not in choices) or (not choices and subtheme):
                            error = 'Choose a subtheme returned for the selected theme. Use a blank subtheme only if none were returned.'
                elif (theme or subtheme) and not themes and not battery_only:
                    error = 'This folder has no theme or subtheme to filter by. Search by keyword.'
                if error:
                    results.append({'folder': folder, 'need': term['need'], 'error': error})
                    continue
                seen = self.searched[folder, theme]
                # Track battery and ordinary requests separately, but count their shared keyword only once toward the limit.
                term_key = keyword, battery_only
                if term_key in seen:
                    results.append({'folder': folder, 'need': term['need'], 'error': 'That keyword was already searched in this folder and theme. Use a new keyword.'})
                    continue
                keywords = {word for word, _ in seen}
                keywords.add(keyword)
                if len(keywords) > 5:
                    results.append({'folder': folder, 'need': term['need'], 'error': 'Five unique searches used for this folder and theme.'})
                    continue
                seen.add(term_key)
                weights = self.weight_candidates(folder)
                candidates = weights if weight_search else self.matches(folder, keyword, theme, subtheme, battery_only=battery_only)
                results.append({'folder': folder, 'need': term['need'], 'candidates': candidates,
                                'weight_candidates': weights, 'searches_left': 5 - len(keywords)})
                if candidates:
                    break
        return results
