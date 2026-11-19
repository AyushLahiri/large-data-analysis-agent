"""Keep fresh ingestion, retry and user material edits separate."""
import json
from pathlib import Path
import shutil

from v_5.main_functions.sql import published_folders


RETRY_MESSAGE = 'Ingestion materials have been updated. Please restart ingestion again.'


def material_inputs(state):
    """Select user-editable inputs for comparison, excluding agent results and UI flags."""
    return {
        'context': state['context'],
        'folders': {name: {key: folder.get(key) for key in ('description', 'relationship', 'display_name', 'reuse_dataset')}
                    for name, folder in state['folders'].items()},
        'files': {key: {field: file.get(field) for field in ('filename', 'folder', 'role', 'asset_kind', 'note',
                  'dataset_context', 'continuation', 'sha256', 'bytes', 'reuse_codebook')}
                  for key, file in state['files'].items() if not file.get('embedded_for')},
    }


def mark_material_changes(path, state, action):
    """Invalidate retry only when a user save actually changes the previous run's inputs.

    Inputs: project path, edited state and save action. Returns: None; sets one flag used
    by Retry. Unchanged Save notes requests and background extraction leave retries intact.
    """
    marker = Path(path) / 'project.json'
    if action == 'extract_embedded_codebook' or not marker.is_file():
        return
    previous = json.loads(marker.read_text(encoding='utf-8'))
    if previous.get('log_directory') and material_inputs(previous) != material_inputs(state):
        state['ingestion_changed'] = True


def reset_ingestion(path, state):
    """Clear the previous ingestion conversation and disposable work before a fresh start.

    Inputs: project path and state. Returns: None; clears retry markers and pending-folder
    results. Uploaded originals, analysis chats and workspaces backing published datasets
    stay available. Only paths within this project's runs directory may be removed.
    """
    path = Path(path).resolve()
    runs = path / 'runs'
    if runs.resolve() != runs:
        raise ValueError('Invalid ingestion run directory.')
    if state.get('log_directory'):
        from v_5.main_functions.session import write_json
        state['ingestion_changed'] = True
        write_json(path / 'project.json', state)  # Partial cleanup must never leave Retry enabled.
    published = published_folders(state)
    protected = set()
    for folder in published:
        final = state.get('codebooks', {}).get(folder, {}).get('final')
        if final:
            workspace = Path(final).resolve().parent.parent
            if workspace.is_relative_to(runs):
                protected.add(workspace)

    def remove_work(target):
        """Remove disposable work while leaving published workspaces and their parents."""
        resolved = target.resolve()
        if not resolved.is_relative_to(runs):
            raise ValueError('Invalid ingestion asset path.')
        if target in protected:
            return
        if any(kept.is_relative_to(target) for kept in protected):
            for child in target.iterdir():
                remove_work(child)
        elif target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()

    if runs.exists():
        for child in runs.iterdir():
            remove_work(child)
    (path / 'inputs.jsonl').unlink(missing_ok=True)  # Previous input snapshots can contain old chat/results.
    for key in ('preparations', 'folder_notes', 'descriptive_reviews', 'codebooks'):
        state[key] = {folder: value for folder, value in state.get(key, {}).items() if folder in published}
    state['findings'] = {key: value for key, value in state.get('findings', {}).items()
                         if state['files'].get(key, {}).get('folder') in published}
    for key in ('handoff_path', 'codebook_resume', 'loader_resume', 'atlas_handoff_path', 'loader_budget',
                'log_directory', 'project_notes', 'error', 'activity', 'ingestion_changed'):
        state.pop(key, None)
    for name, folder in state['folders'].items():
        if name not in published:
            folder.pop('summary', None)
    for file in state['files'].values():
        if file['folder'] not in published:
            file.pop('placement_note', None)
    state.update(messages=[], question_form=None, pending='')
