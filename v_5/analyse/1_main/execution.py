import pyarrow as pa
import pyarrow.parquet as pq
from collections import defaultdict
from decimal import Decimal
from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream
from psycopg2 import sql
from uuid import uuid4
from v_5.analyse.general import (
    FRAME_ROWS, JoinCardinalityError, MAX_COLUMNS, MAX_ROWS, ResultLimit, result_table,
)
from v_5.analyse.sql_assets.catalog import read_atlas
from v_5.main_functions.sql import identifier, project_table


SAFE_FUNCTIONS = set('count sum avg min max stddev stddev_pop stddev_samp var_pop var_samp round abs sqrt power lower upper length trim btrim substr substring date_trunc extract date_part floor ceil coalesce nullif greatest least percentile_cont percentile_disc bool_and bool_or row_number rank dense_rank lag lead'.split())


SAFE_TYPES = set('text varchar bpchar numeric int2 int4 int8 float4 float8 bool date timestamp timestamptz'.split())


SAFE_OPERATORS = set('= <> != > < >= <= + - * / % ^ ~~ !~~ ~~* !~~* ||'.split())


def nodes(value):
    """Walk the nested representation of a parsed SQL statement.

    Inputs: value: nested dict/list/tuple from the SQL parser.
    Returns: Iterator yielding dicts marked as SQL nodes by their @ field.
    Details: Used by SQL validation and join analysis to inspect every nested construct.
    """
    if isinstance(value, dict):
        if '@' in value:
            yield value
        for member in value.values():
            yield from nodes(member)
    elif isinstance(value, (tuple, list)):
        for member in value:
            yield from nodes(member)


def strings(items):
    """Read text values from SQL parser string nodes.

    Inputs: items: string-node records, or None.
    Returns: List of sval strings.
    Details: Small parser adapter used for function names, operators and column references.
    """
    return [item['sval'] for item in items or ()]


def check_join(node, tables):
    """Require explicit equality keys for a parsed join.

    Inputs: node: JoinExpr dict; tables: scoped table metadata with primary_key lists.
    Returns: Set of paired (alias, column) keys; raises when join conditions are unsupported
        or incomplete.
    Details: Used by checked_sql and cross-folder checks. Flattens AND terms and requires
        all primary-key pairs when joining direct table parts.
    """
    terms = [node['quals']] if node['quals'] else []
    while any(term['@'] == 'BoolExpr' and term['boolop']['name'] == 'AND_EXPR' for term in terms):
        terms = [child for term in terms for child in (
            term['args'] if term['@'] == 'BoolExpr' and term['boolop']['name'] == 'AND_EXPR' else [term]
        )]
    if node['isNatural'] or node['usingClause'] or any(n['@'] == 'BoolExpr' and n['boolop']['name'] != 'AND_EXPR' for n in terms):
        raise ValueError('Join with explicit key equalities connected by AND.')
    pairs = set()
    for term in terms:
        if term['@'] == 'A_Expr' and term['kind']['name'] == 'AEXPR_OP' and strings(term['name']) == ['=']:
            left, right = term.get('lexpr') or {}, term.get('rexpr') or {}
            if left.get('@') == right.get('@') == 'ColumnRef':
                a, b = strings(left['fields']), strings(right['fields'])
                if len(a) == len(b) == 2 and a[0] != b[0]:
                    pairs.add(frozenset((tuple(a), tuple(b))))
    left, right = node['larg'], node['rarg']

    def aliases(branch):
        """Collect the aliases represented by one side of a join.

        Inputs: branch: table or nested join node.
        Returns: Set of alias/table names.
        Details: Private recursive helper for check_join; keeps equality pairs only when they
            connect opposite sides.
        """
        if branch.get('alias'):
            return {branch['alias']['aliasname']}
        if branch['@'] == 'JoinExpr':
            return aliases(branch['larg']) | aliases(branch['rarg'])
        return {branch.get('relname')}

    left_aliases, right_aliases = aliases(left), aliases(right)
    pairs = {pair for pair in pairs if {key[0] for key in pair} & left_aliases and {key[0] for key in pair} & right_aliases}
    if not pairs:
        raise ValueError('Use an explicit JOIN ON with equality between key columns from two distinct tables.')
    if left['@'] == right['@'] == 'RangeVar' and left['relname'] in tables and right['relname'] in tables:
        left_keys = tables[left['relname']].get('primary_key', [])
        right_keys = tables[right['relname']].get('primary_key', [])
        a = (left.get('alias') or {}).get('aliasname') or left['relname']
        b = (right.get('alias') or {}).get('aliasname') or right['relname']
        if (not left_keys or len(left_keys) != len(right_keys) or any(
                frozenset(((a, key), (b, other))) not in pairs for key, other in zip(left_keys, right_keys))):
            raise ValueError('Join the table parts on every primary-key column using both aliases.')
    return pairs


def cross_folder_join_checks(statement, allowed):
    """Build duplicate-key checks for joins between datasets.

    Inputs: statement: parsed SELECT; allowed: table-to-folder mapping.
    Returns: List of (left source, key columns, check SQL) tuples for execute_sql.
    Details: Called after structural validation. Traces top-level CTEs to their folders and
        requires named CTEs for cross-folder joins before generating left-key uniqueness
        queries.
    """
    ctes = statement.withClause.ctes if statement.withClause else ()
    main = statement()
    main['withClause'] = None
    prior, folder_sources, checks = [], {}, []

    def folders_in(branch):
        """Find the dataset folders feeding a query branch.

        Inputs: branch: parsed SQL subtree; captures allowed tables and earlier CTE source
            mappings.
        Returns: Set of folder keys.
        Details: Used while preparing cross-folder checks to distinguish joins inside a dataset
            from joins between datasets.
        """
        folders = set()
        for node in nodes(branch):
            if node['@'] == 'RangeVar':
                name = node['relname']
                folders.update({allowed[name]} if node['schemaname'] else folder_sources.get(name, set()))
        return folders

    for cte, query in [*((cte, cte.ctequery()) for cte in ctes), (None, main)]:
        for node in nodes(query):
            if node['@'] == 'WithClause':
                raise ValueError('Use one top-level WITH clause for cross-folder SQL.')
            if node['@'] != 'JoinExpr':
                continue
            left, right = node['larg'], node['rarg']
            left_folders, right_folders = folders_in(left), folders_in(right)
            if not left_folders or not right_folders or len(left_folders | right_folders) == 1:
                continue
            for side in (left, right):
                if (side['@'] != 'RangeVar' or side['schemaname'] or side['relname'] not in folder_sources
                        or (side['alias'] or {}).get('colnames')):
                    raise ValueError('For each cross-folder join, project the key columns into two named CTEs first.')
            left_alias = (left['alias'] or {}).get('aliasname', left['relname'])
            pairs = check_join(node, {})
            keys = sorted({key for pair in pairs for alias, key in pair if alias == left_alias})
            join_keys = [key for pair in pairs for _, key in pair]
            if any(key.casefold() == 'ow_id' for key in join_keys):
                raise ValueError('Synthetic ow_id values cannot link records across folders.')
            check = sql.SQL('WITH {} SELECT 1 FROM {} WHERE {} GROUP BY {} HAVING count(*) > 1 LIMIT 1').format(
                sql.SQL(', ').join(prior), identifier(left['relname']),
                sql.SQL(' AND ').join(sql.SQL('{} IS NOT NULL').format(identifier(key)) for key in keys),
                sql.SQL(', ').join(map(identifier, keys)),
            )
            checks.append((left['relname'], keys, check))
        if cte is not None:
            folder_sources[cte.ctename] = folders_in(query)
            prior.append(sql.SQL(RawStream()(cte)))
    return checks


def checked_sql(code, catalog, allow_preview, *, join_checks=None):
    """Validate an analyst query against the selected data and SQL rules.

    Inputs: code: SQL; catalog: allowed schema/tables/columns; allow_preview: LIMIT
        permission; join_checks: optional list enabling cross-folder checks.
    Returns: (folders, columns_by_table): referenced folders and columns for weight checks
        and sql_reviewer; extends join_checks when needed.
    Details: Parses one SELECT, rejects unsupported statements/functions/operators/sources,
        checks joins and scope, then identifies referenced variables without executing the
        query.
    """
    try:
        statements = parse_sql(code)
    except ParseError as error:
        raise ValueError(str(error)) from error
    if len(statements) != 1 or not isinstance(statements[0].stmt, ast.SelectStmt):
        raise ValueError('Only a single SELECT statement is allowed.')
    tree = list(nodes(statements[0].stmt()))
    ctes = {n['ctename'] for n in tree if n['@'] == 'CommonTableExpr'}
    aliases = {(n.get('alias') or {}).get('aliasname') or n.get('relname') for n in tree
               if n['@'] in {'RangeVar', 'RangeSubselect'}}
    allowed = {table['name']: folder for folder, data in catalog.folders.items() for table in data['tables']}
    tables = {table['name']: table for data in catalog.folders.values() for table in data['tables']}
    sources = defaultdict(set)
    references = set()
    for node in tree:
        tag = node['@']
        if tag.endswith('Stmt') and tag != 'SelectStmt':
            raise ValueError('SQL writes and administrative statements are blocked.')
        if tag == 'SelectStmt':
            if node['intoClause'] or node['lockingClause'] or len(node['fromClause'] or ()) > 1:
                raise ValueError('SELECT INTO, row locking, and comma joins are blocked.')
            if (node['limitCount'] or node['limitOffset']) and not allow_preview:
                raise ValueError('Return the whole aggregate result so it can be checked for size. LIMIT is for explicitly requested previews.')
        elif tag == 'WithClause' and (node['recursive'] or any(not name.startswith('q_') for name in ctes)):
            raise ValueError('Use nonrecursive CTEs with names starting with q_.')
        elif tag == 'RangeVar':
            name = node['relname']
            if not node['schemaname'] and not node['catalogname'] and name in ctes and name.startswith('q_'):
                continue
            if node['catalogname'] or node['schemaname'] != catalog.schema or name not in allowed:
                raise ValueError('Use only explicitly qualified data tables in the selected scope.')
            sources[name].add((node.get('alias') or {}).get('aliasname') or name)
        elif tag == 'JoinExpr':
            check_join(node, tables)
        elif tag == 'ColumnRef':
            fields = node['fields']
            if any(field['@'] == 'A_Star' for field in fields) or (len(fields) == 1 and fields[0].get('sval') in aliases):
                raise ValueError('Select individual columns. COUNT(*) is allowed.')
            references.add(tuple(strings(fields)))
        elif tag == 'GroupingSet':
            kind = node['kind']['name'].removeprefix('GROUPING_SET_')
            clause = kind if kind in {'ROLLUP', 'CUBE'} else 'GROUPING SETS'
            raise ValueError(f'{clause} is unsupported. Use GROUP BY for each level and UNION ALL to combine them.')
        elif tag in {'RangeFunction', 'RangeTableSample', 'RangeTableFunc', 'JsonTable'}:
            raise ValueError(f'The FROM clause uses {tag}. Use a scoped data table, subquery, or CTE as the source.')
        elif tag in {'CollateClause', 'ParamRef', 'RowExpr', 'ArrayExpr', 'A_ArrayExpr'}:
            raise ValueError(f'The query uses the unsupported SQL construct {tag}.')
        elif tag == 'FuncCall':
            name = strings(node['funcname'])
            join_sampling = join_checks is not None and name[-1] == 'random'
            if (name[-1] not in SAFE_FUNCTIONS and not join_sampling) or name[:-1] not in ([], ['pg_catalog']):
                raise ValueError(f'Function {".".join(name)}() is outside the approved PostgreSQL analysis functions.')
        elif tag == 'TypeName':
            name = strings(node['names'])
            if name[-1] not in SAFE_TYPES or name[:-1] not in ([], ['pg_catalog']):
                raise ValueError(f'Type {".".join(name)} is outside the approved scalar cast types.')
        elif tag == 'A_Expr' and (len(strings(node['name'])) != 1 or strings(node['name'])[0] not in SAFE_OPERATORS):
            raise ValueError('Use ordinary unqualified comparison and arithmetic operators.')
        elif tag == 'SortBy' and node.get('useOp'):
            raise ValueError('Sort using ASC or DESC.')
    folders = {allowed[table] for table in sources}
    if not folders:
        raise ValueError('Query at least one scoped data table.')
    if len(folders) > 1 and join_checks is None:
        raise ValueError('Read the cross_folder_joins skill before querying multiple folders.')
    if len(folders) > 1:
        join_checks.extend(cross_folder_join_checks(statements[0].stmt, allowed))
    # Map parsed column references back to real table columns for the SQL reviewer's evidence payload.
    columns_by_table = {
        table: sorted({parts[-1] for parts in references if parts[-1] in catalog.columns[table]
                       and (len(parts) == 1 or parts[-2] in table_aliases)})
        for table, table_aliases in sources.items()
    }
    return folders, columns_by_table


def execute_sql(code, *, session=None, join_checks=()):
    """Run a checked query and return a bounded chat table.

    Inputs: code: previously validated SQL; session: optional cancellation owner;
        join_checks: left-key uniqueness checks.
    Returns: columns/rows dict with JSON-safe cells; raises ResultLimit or
        JoinCardinalityError when appropriate.
    Details: Called after sql_reviewer approval. Uses read_atlas and a streaming cursor,
        runs uniqueness checks first, and counts oversized results for a useful error.
    """
    statement = code.strip().rstrip(';')
    with read_atlas(session=session) as (connection, counter), connection.cursor(name='analysis_' + uuid4().hex[:8]) as cursor:
        for left, keys, check in join_checks:
            counter.execute(check)
            if counter.fetchone():
                raise JoinCardinalityError(f"Left key {', '.join(keys)} repeats in {left}. Only one-to-one or one-to-many joins are allowed.")
        cursor.execute(statement)
        # One extra row detects overflow without first loading the entire result into memory.
        rows = cursor.fetchmany(MAX_ROWS + 1)
        columns = [column.name for column in cursor.description]
        if len(rows) > MAX_ROWS or len(columns) > MAX_COLUMNS:
            counter.execute(sql.SQL('SELECT count(*) FROM ({}) AS q_result').format(sql.SQL(statement)))
            raise ResultLimit(counter.fetchone()[0], len(columns))
        return result_table(columns, [[json_value(cell) for cell in row] for row in rows])


def json_value(value):
    """Convert a database cell into a JSON-safe display value.

    Inputs: value: scalar from the SQL result.
    Returns: Basic JSON scalar; nonfinite numbers become None and other types become text.
    Details: Used by execute_sql, including Decimal values that should remain numeric where
        possible.
    """
    if isinstance(value, Decimal):
        return (int(value) if value == value.to_integral_value() else float(value)) if value.is_finite() else None
    if isinstance(value, float) and not (-float('inf') < value < float('inf')):
        return None
    return value if value is None or isinstance(value, (str, bool, int, float)) else str(value)


def pull_frame(catalog, folder, requested, weight, destination):
    """Export selected variables and keys to a temporary Parquet frame.

    Inputs: catalog/folder: scoped dataset; requested: variables; weight: optional weight
        variable; destination: output path.
    Returns: Frame metadata with file, folder, rows, columns and primary_key; writes the
        Parquet file.
    Details: Analyst.python uses this after choosing weights. Resolves variables, joins
        table parts on all keys and enforces row/cell limits before sandbox execution.
    """
    data = catalog.folders[folder]
    tables = {item['name']: item for item in data['tables']}
    first = data['tables'][0]
    keys = first['primary_key']
    if not keys:
        raise ValueError('The dataset has no confirmed primary key.')
    if first['rows'] > FRAME_ROWS:
        raise ValueError(f'Python frames can read up to {FRAME_ROWS:,} records per folder. Try a SQL aggregation first.')
    selected = {key.casefold(): (first['name'], key) for key in keys}
    for variable in dict.fromkeys(name.casefold() for name in [*requested, weight] if name):
        if variable in selected:
            continue
        match = catalog.exact(folder, variable)
        if len(match) != 1:
            raise ValueError(f'Choose a precise variable in {folder}: {variable}.')
        column = match[0]
        selected[variable] = (column['table_name'], column['column_name'])
    if len(selected) * first['rows'] > 3_000_000:
        raise ValueError('This Python frame is too wide for its record count. Select fewer variables or aggregate in SQL.')
    aliases = {name: sql.Identifier(f'p{index}') for index, name in enumerate([first['name'], *dict.fromkeys(table for table, _ in selected.values() if table != first['name'])])}
    qualified = lambda table, column: sql.SQL('{}.{}').format(aliases[table], identifier(column))
    projection = sql.SQL(', ').join(sql.SQL('{} AS {}').format(qualified(table, column), identifier(name))
                                     for name, (table, column) in selected.items())
    source = sql.SQL('{} AS {}').format(project_table(catalog.schema, first['name']), aliases[first['name']])
    for table in list(aliases)[1:]:
        if tables[table]['primary_key'] != keys:
            raise ValueError('Table parts do not share the same primary key.')
        comparisons = sql.SQL(' AND ').join(sql.SQL('{} = {}').format(qualified(first['name'], key), qualified(table, key)) for key in keys)
        source += sql.SQL(' JOIN {} AS {} ON {}').format(project_table(catalog.schema, table), aliases[table], comparisons)
    with read_atlas(session=catalog.session) as (connection, _), connection.cursor(name='frame_' + uuid4().hex[:8]) as cursor:
        cursor.execute(sql.SQL('SELECT {} FROM {}').format(projection, source))
        rows = cursor.fetchmany(FRAME_ROWS + 1)
        if len(rows) > FRAME_ROWS:
            raise ValueError('This Python frame is too large. Use Atlas to aggregate first.')
        columns = list(selected)
        values = {name: [row[index] for row in rows] for index, name in enumerate(columns)}
        pq.write_table(pa.table(values), destination)
    return {'file': destination.name, 'folder': folder, 'rows': len(rows), 'columns': columns, 'primary_key': [key.casefold() for key in keys]}


SAFE_OPERATORS.update({'BETWEEN', 'NOT BETWEEN', 'BETWEEN SYMMETRIC', 'NOT BETWEEN SYMMETRIC'})
