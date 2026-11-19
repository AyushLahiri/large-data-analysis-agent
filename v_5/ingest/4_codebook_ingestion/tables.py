import json
import pandas as pd
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pathlib import Path
from v_5.ingest.manifest import manifest_columns, work_path
from v_5.main_functions.session import write_json
from werkzeug.utils import secure_filename


COLUMNS = ['variable_name', 'variable_label', 'variable_description', 'other_information',
           'values', 'source_location', 'source_presence']


MASTER_FIELDS = ['variable_name', '% missing', 'variable_type', 'original_variable_name', 'table_names']


def save_csv(path, frame):
    """Replace a derived table completely; assembly retries use the same output paths."""
    from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import atomic_text
    atomic_text(path, frame.to_csv(index=False))


def normalize_rows(rows):
    """Standardize extracted codebook rows.

    Inputs: rows: records with the COLUMNS fields.
    Returns: DataFrame with ordered text columns, lowercase unique variable names and
        source_presence set.
    Details: Used by phase 4 workers. Fills absent definitions with not in source and
        rejects missing or duplicate names.
    """
    frame = pd.DataFrame(rows, columns=COLUMNS).fillna('not in source')
    for col in COLUMNS:
        frame[col] = frame[col].astype(str).replace('', 'not in source')
    frame['variable_name'] = frame['variable_name'].str.lower()
    if frame.variable_name.isin(['', 'not in source']).any() or frame.variable_name.duplicated().any():
        raise ValueError('Codebook variable names must be nonblank and unique after lowercasing.')
    frame['source_presence'] = 'in source'
    return frame


def create_master(workspace, prepared):
    """Build the prepared-data variable roster and missingness counts.

    Inputs: workspace: work root; prepared: loader report with manifest_path.
    Returns: DataFrame of MASTER_FIELDS: variable name, missing percentage, type, original
        name and table names.
    Details: Phase 4 reads Parquet batches, not source codebook claims. Shared variables
        across table parts keep one roster row and accumulate their locations.
    """
    manifest = json.loads(work_path(workspace, prepared['manifest_path']).read_text(encoding='utf-8'))
    rows = {}
    for table in manifest['tables']:
        path = table.get('file_path') or table.get('path') or table.get('parquet_path')
        parquet = pq.ParquetFile(work_path(workspace, path))
        schema = parquet.schema_arrow
        row_count = parquet.metadata.num_rows
        missing_counts = dict.fromkeys(schema.names, 0)
        for batch in parquet.iter_batches(batch_size=8192):
            for column, values in zip(batch.schema.names, batch.columns):
                missing_counts[column] += pc.sum(pc.is_null(values, nan_is_null=True)).as_py()
        mappings = manifest_columns(table)
        seen = set()
        for column, missing_count in missing_counts.items():
            metadata = mappings.get(column, {})
            original = metadata.get('original_name') or column
            name = original.lower()
            if name in seen or (name in rows and rows[name]['original_variable_name'] != original):
                raise ValueError(f'Variable names collide after lowercasing: {original}.')
            seen.add(name)
            row = rows.setdefault(name, {
                'variable_name': name,
                '% missing': 100 * missing_count / row_count if row_count else None,
                'variable_type': metadata.get('type') or metadata.get('sql_type') or str(schema.field(column).type),
                'original_variable_name': original,
                'table_names': [],
            })
            row['table_names'].append(table['name'])
    return pd.DataFrame([{**row, 'table_names': '\n'.join(row['table_names'])} for row in rows.values()],
                        columns=MASTER_FIELDS)


def join_codebooks(master, sources):
    """Join source definitions onto the prepared variable roster.

    Inputs: master: variable roster DataFrame; sources: (prefix, filename, normalized frame)
        tuples.
    Returns: (combined, unmatched): wide per-source definitions for data variables, plus
        source rows absent from the data.
    Details: Used by finish_codebooks and PDF block reconciliation. Prefixes keep each
        source's evidence separate; joins must be one-to-one.
    """
    combined = master.copy()
    unmatched = []
    for prefix, filename, frame in sources:
        extras = frame[~frame.variable_name.isin(master.variable_name)].copy()
        extras.insert(0, 'source_filename', filename)
        unmatched.append(extras)
        # Only variable_name is shared; source-prefixed fields preserve competing definitions side by side.
        renamed = frame.rename(columns={c: c + '_' + prefix for c in COLUMNS if c != 'variable_name'})
        combined = combined.merge(renamed, on='variable_name', how='left', validate='one_to_one')
        source_columns = renamed.columns.drop('variable_name')
        combined[source_columns] = combined[source_columns].fillna('not in source')
    return combined, pd.concat(unmatched, ignore_index=True) if unmatched else pd.DataFrame(columns=['source_filename', *COLUMNS])


def save_source_table(directory, source_dir, filename, frame, mapping):
    """Save a source codebook with a unique column prefix.

    Inputs: directory: combined output root; source_dir: worker outputs; filename: source
        label; frame: normalized rows; mapping: mutable source registry.
    Returns: Chosen prefix string; writes a prefixed CSV and updates source_mapping.json.
    Details: Phase 4 connects wide-table columns to filenames, notes and verification.
        Filenames depend on this pass's sources, so retries overwrite the same assets.
    """
    stem = secure_filename(Path(filename).stem) or source_dir.name
    base = prefix = stem[:3].lower()
    index = 2
    while prefix in mapping:
        prefix = f'{base}{index}'
        index += 1
    output = directory / f'{stem}_codebook.csv'
    if str(output) in {source['table'] for source in mapping.values()}:
        output = directory / f'{stem}_{prefix}_codebook.csv'
    save_csv(output, frame.rename(columns={c: c + '_' + prefix for c in COLUMNS if c != 'variable_name'}))
    source = {'filename': filename, 'table': str(output), 'notes': str(source_dir / 'general_information.md')}
    if Path(filename).suffix.lower() == '.pdf':
        verification = source_dir / 'verification.json'
        report = json.loads(verification.read_text(encoding='utf-8'))
        source.update(verification=str(verification), issues=len(report['issues']))
    mapping[prefix] = source
    write_json(directory / 'source_mapping.json', mapping)
    return prefix


def finish_codebooks(session, folder, directory, master, sources, mapping):
    """Save the joined source evidence and register the codebook outputs.

    Inputs: session/folder: project owner; directory: output root; master: roster; sources:
        source tuples; mapping: source registry.
    Returns: Book dict with counts and master/combined/unmatched/mapping paths; also updates
        session.state.
    Details: The single merged_with_master flag covers source-column renaming, the master
        join and unmatched table. It becomes True only after all assembly assets are saved;
        an unfinished pass rewrites those assets through ingest_codebooks.
    """
    combined, unmatched = join_codebooks(master, sources)
    save_csv(directory / 'combined_codebook.csv', combined)
    save_csv(directory / 'unmatched_variables.csv', unmatched)
    write_json(directory / 'source_mapping.json', mapping)
    result = {'folder': folder, 'variables': len(master), 'sources': len(sources), 'unmatched_rows': len(unmatched),
              'master': str(directory / 'data_variables.csv'), 'combined': str(directory / 'combined_codebook.csv'),
              'unmatched': str(directory / 'unmatched_variables.csv'), 'source_mapping': str(directory / 'source_mapping.json'),
              'source_tables': mapping, 'merged_with_master': True}
    session.state.setdefault('codebooks', {})[folder] = result
    session.record('codebooks_complete', 'codebook_agent', result)
    warnings = [f"{source['filename']}: see the verification report for extraction warnings."
                for source in mapping.values() if source.get('issues')]
    summary = f"Codebooks organized: {len(master)} data variables, {len(sources)} source codebooks and {len(unmatched)} unmatched source variables."
    session.message('codebook_agent', '\n'.join([summary, *warnings]))
    return result
