import pyreadstat
from pathlib import Path
from v_5.main_functions.session import write_json


def extract_embedded_codebook(path, destination):
    """Export embedded statistical-file definitions as a CSV codebook.

    Inputs: path: uploaded statistical file; destination: output CSV path.
    Returns: True when metadata is exported; False for an unrecognized plain DAT file.
    Details: Phase 1 upload processing calls this in the background. Reads metadata only and
        writes one row per variable/category with labels, types and missing-value rules.
    """
    import csv
    import json
    suffix = Path(path).suffix.lower()
    readers = {'.sav': pyreadstat.read_sav, '.zsav': pyreadstat.read_sav,
               '.por': pyreadstat.read_por, '.dta': pyreadstat.read_dta}
    candidates = [readers[suffix]] if suffix in readers else [pyreadstat.read_sav, pyreadstat.read_dta]
    metadata = None
    for reader in candidates:
        try:
            options = {} if reader == pyreadstat.read_por else {'user_missing': True}
            _, metadata = reader(str(path), metadataonly=True, **options)
            break
        except pyreadstat.ReadstatError:
            if suffix != '.dat':
                raise
    if metadata is None:
        return False  # Plain DAT files have no embedded dictionary.
    with open(destination, 'w', newline='', encoding='utf-8') as output:
        writer = csv.writer(output)
        writer.writerow(['variable_name', 'variable_label', 'storage_type', 'original_format',
                         'measurement_level', 'category_code', 'category_label', 'missing_ranges', 'missing_values'])
        for name in metadata.column_names:
            labels = metadata.variable_value_labels.get(name, {})
            for code, label in (labels.items() if labels else [('', '')]):
                writer.writerow([name, metadata.column_names_to_labels.get(name, ''),
                    metadata.readstat_variable_types.get(name, ''), metadata.original_variable_types.get(name, ''),
                    metadata.variable_measure.get(name, ''), code, label,
                    json.dumps(metadata.missing_ranges.get(name, [])),
                    json.dumps(metadata.missing_user_values.get(name, []))])
    return True


def save_handoff(self, project_notes, folder_notes, files, findings, previews, user_clarifications, *, protected_folders=()):
    """Persist the reviewed materials for the loader.

    When: End of phase 1, through Session.save_handoff.
    Inputs: self: session; project_notes/folder_notes: decisions; files: metadata;
        findings/previews: evidence by file ID; user_clarifications: answers;
        protected_folders: published keys.
    Returns: None; writes ingestion_handoff.json, saves its path and updates project state.
    Flow: Removes confirmed unrelated uploads only within the project upload directory,
        removes empty folders, and preserves protected data for prepare_data.
    """
    with self.lock:
        for file_id in list(files):
            file = files[file_id]
            if (file['folder'] not in protected_folders and file.get('ignore')
                    and findings.get(file_id, {}).get('classification') == 'likely unrelated'):
                target = Path(file['path']).resolve()
                if not target.is_relative_to((self.path / 'uploads').resolve()):
                    raise ValueError('Excluded file is outside this project upload directory.')
                target.unlink(missing_ok=True)
                self.record('unrelated_file_removed', 'ingestion_agent', {
                    'filename': file['filename'], 'reason': file.get('placement_note', 'Unrelated to project')})
                files.pop(file_id)
                findings.pop(file_id, None)
                previews.pop(file_id, None)
        for name in list(self.state['folders']):
            if not any(file['folder'] == name for file in files.values()):
                self.state['folders'].pop(name)
                folder_notes.pop(name, None)
        self.state.update(files=files, project_notes=project_notes, folder_notes=folder_notes, findings=findings)
        from v_5.main_functions.assets import prune_deleted_assets
        prune_deleted_assets(self.path, self.state)
        handoff = dict(project_notes=project_notes, folder_notes=folder_notes, files=files, findings=findings, previews=previews, user_clarifications=user_clarifications)
        write_json(self.run_path / 'ingestion_handoff.json', handoff)
        self.state['handoff_path'] = str(self.run_path / 'ingestion_handoff.json')
        self.state.pop('codebook_resume', None)  # A newly reviewed handoff starts new preparation.
        self.state.pop('loader_resume', None)
        for file in files.values():
            self.state['folders'].setdefault(file['folder'], {'description': ''})
        for name, note in folder_notes.items():
            self.state['folders'].setdefault(name, {'description': ''})['summary'] = note['summary']
        self.save()
    self.record('ingestion_handoff', 'ingestion_agent', dict(project_notes=project_notes, folder_notes=folder_notes, files=files, findings=findings))
