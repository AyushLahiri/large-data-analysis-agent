import pyarrow.parquet as pq
from pydantic import ValidationError
from v_5.ingest.data_loader.agent import PreparedManifest


def work_path(workspace, path):
    """Translate a sandbox output path into a safe host path.

    Inputs: workspace: host work directory; path: relative path or /work/... path.
    Returns: Resolved Path inside workspace; raises ValueError on escape.
    Details: Shared by loader validation and codebook/publication readers so agent paths
        stay inside their run.
    """
    target = (workspace / str(path).removeprefix('/work/')).resolve()
    if not target.is_relative_to(workspace.resolve()):
        raise ValueError('Codebook output must stay inside its workspace.')
    return target


def manifest_columns(table):
    """Index saved column metadata by the prepared column name.

    Inputs: table: manifest table record; columns may be a dict or a list.
    Returns: Name-to-metadata dict; raises for missing or duplicate names in list-form
        manifests.
    Details: Used by validation, codebooks and SQL publication. Supports older final_name
        and simple type mappings.
    """
    columns = table.get('columns', [])
    if isinstance(columns, dict):
        return {name: details if isinstance(details, dict) else {'type': details}
                for name, details in columns.items()}
    by_name = {}
    for column in columns:
        # Older manifests can use name for the source field and final_name for the Parquet column.
        name = column.get('final_name') or column.get('name')
        if not name or name in by_name:
            raise ValueError(f"Table {table.get('name', '')!r} has a missing or duplicate column name {name!r}.")
        by_name[name] = column
    return by_name


def validate_prepared_manifest(workspace, path):
    """Check that a loader manifest describes its saved Parquet files.

    Inputs: workspace: work root; path: manifest path understood by work_path.
    Returns: None on success; raises a readable validation/file error on mismatch.
    Details: Runs before descriptives review. Validates the response schema, column names,
        row counts and primary-key membership against saved outputs.
    """
    try:
        manifest = PreparedManifest.model_validate_json(work_path(workspace, path).read_text(encoding='utf-8'))
    except ValidationError as error:
        first = error.errors(include_input=False, include_context=False, include_url=False)[0]
        field = '.'.join(map(str, first['loc'])) or 'JSON'
        raise ValueError(f"Manifest {field} — {first['msg']} ({error.error_count()} validation errors).") from None

    for table in manifest.tables:
        columns = manifest_columns(table.model_dump())
        if any(name != column['name'] for name, column in columns.items()):
            raise ValueError(f"Table {table.name!r} must use 'name' for each saved Parquet column.")
        parquet = pq.ParquetFile(work_path(workspace, table.path))
        if set(columns) != set(parquet.schema_arrow.names):
            raise ValueError(f"Table {table.name!r} has column names that differ between the manifest and Parquet.")
        if parquet.metadata.num_rows != table.row_count:
            raise ValueError(f"Table {table.name!r} has a row_count that differs from the Parquet.")
        if not set(table.primary_key) <= columns.keys():
            raise ValueError(f"Table {table.name!r} has a primary_key outside its saved columns.")
