"""Small setup endpoints for per-file tagging and explicit exact-copy choices."""

from copy import deepcopy
import json
from flask import jsonify, request
from v_5.main_functions.assets import (
    ADMIN_USER_ID, archive_project, locked_setup_folders, reuse_options, source_project,
)
from v_5.main_functions.sql import delete_atlas_folder, published_folders


def register_reuse(app, read, editable, save, lock, check_context):
    """Attach the folder wizard routes to the existing project lifecycle."""

    def editable_folder(state, folder):
        """Require the current unfinished folder before accepting changed file answers."""
        if folder not in state['folders']:
            raise ValueError('This folder no longer exists.')
        if folder in published_folders(state) or folder in locked_setup_folders(state):
            raise ValueError('Restart this folder’s ingestion before changing its files or answers.')
        if state.get('active_folder') and state['active_folder'] != folder:
            raise ValueError('Finish the current folder first.')

    @app.get('/api/projects/<project_id>/reuse')
    def options(project_id):
        """Return exact-copy provenance and completed sources for the current folder."""
        with lock:
            path, state = read(project_id)
            folder = request.args.get('folder', '')
            if folder not in state['folders']:
                raise ValueError('Choose an existing folder.')
            return jsonify(reuse_options(path, state, folder))

    @app.post('/api/projects/<project_id>/file-details')
    def file_details(project_id):
        """Save one file's tags/answers; validate a requested source against exact bytes.

        Blank dataset answers can wait for the folder copy choice, which fills them from
        the source. Ordinary ingestion still requires them before setup is submitted.
        """
        with lock:
            path, state = editable(project_id)
            body = request.json
            file = state['files'].get(body.get('file_id'))
            if not file or file.get('embedded_for'):
                raise ValueError('Choose an uploaded file.')
            editable_folder(state, file['folder'])
            role = body.get('role')
            if role not in {'primary', 'continuation', 'asset'}:
                raise ValueError('Choose a file role.')
            if (file['role'] == 'primary') != (role == 'primary'):
                raise ValueError('The first file remains the main dataset.')
            if role == 'continuation' and not body.get('continuation'):
                raise ValueError('Choose remaining rows, columns or both.')
            if role != 'asset' and file['format'] in {'pdf', 'md', 'dct', 'dcf', 'map'}:
                raise ValueError('This format is supporting material, not a data continuation.')
            for key in ('role', 'asset_kind', 'note', 'dataset_context', 'continuation'):
                if key in body:
                    file[key] = deepcopy(body[key])
            choice = body.get('reuse_codebook', file.get('reuse_codebook'))
            if 'reuse_codebook' in body or role != 'asset' or file.get('asset_kind') != 'codebook':
                file['reuse_codebook'] = choice or False
            if role != 'asset' or file.get('asset_kind') != 'codebook':
                if body.get('reuse_codebook'):
                    raise ValueError('Only files tagged as codebooks can reuse a codebook.')
                choice = None
                file['reuse_codebook'] = False
            if choice:
                if role != 'asset' or file.get('asset_kind') != 'codebook':
                    raise ValueError('Only files tagged as codebooks can reuse a codebook.')
                matches = reuse_options(path, state, file['folder'])['codebook_matches'].get(file['file_id'], [])
                selected = next((m for m in matches if all(m[key] == choice.get(key)
                                for key in ('project_id', 'folder', 'file_id'))), None)
                if not selected:
                    raise ValueError('No completed, identical source codebook is available.')
                file['reuse_codebook'] = {key: selected[key] for key in ('project_id', 'folder', 'file_id')}
            file['tagged'] = True
            state['active_folder'] = file['folder']
            state['folders'][file['folder']].pop('reuse_dataset', None)
            state['folders'][file['folder']]['setup_reviewed'] = False
            save(path, state, 'file_details')
            return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/folder-setup')
    def folder_setup(project_id):
        """Save this folder's reviewed form and unlock uploads without starting ingestion.

        Inputs: folder answers and an optional exact-copy choice. Returns: saved state;
        the separate project start endpoint begins work across all completed forms.
        """
        with lock:
            path, state = editable(project_id)
            body, folder = request.json, request.json.get('folder')
            editable_folder(state, folder)
            files = [f for f in state['files'].values() if f['folder'] == folder and not f.get('embedded_for')]
            if any(not f.get('tagged') for f in files):
                raise ValueError('Review each file before finishing the folder form.')
            target = state['folders'][folder]
            target.update({key: body.get(key, target.get(key, '')) for key in ('description', 'relationship')})
            target.pop('reuse_dataset', None)
            choice = body.get('reuse_dataset')
            if choice:
                matches = reuse_options(path, state, folder)['dataset_matches']
                selected = next((m for m in matches if (m['project_id'], m['folder']) ==
                                 (choice.get('project_id'), choice.get('folder'))), None)
                if not selected:
                    raise ValueError('The entire data set must match one completed source folder.')
                _, source = source_project(path, selected['project_id'], state.get('user_id', ADMIN_USER_ID))
                metadata = path.parent.parent / 'users' / str(state.get('user_id', ADMIN_USER_ID)) / 'projects' / selected['project_id'] / 'assets.json'
                originals = json.loads(metadata.read_text(encoding='utf-8'))
                original = originals['folders'][selected['folder']]
                for key in ('description', 'relationship'):
                    target[key] = deepcopy(original.get(key, ''))
                for target_id, source_id in selected['files'].items():
                    for key in ('note', 'dataset_context', 'continuation'):
                        state['files'][target_id][key] = deepcopy(originals['files'][source_id].get(key, {} if key == 'dataset_context' else [] if key == 'continuation' else ''))
                target['reuse_dataset'] = {key: selected[key] for key in ('project_id', 'folder', 'files')}
                # A full-folder choice uses that folder's identical books as well, unless
                # the user explicitly chose independent ingestion for a particular book.
                options = reuse_options(path, state, folder)['codebook_matches']
                for file in files:
                    if file.get('asset_kind') != 'codebook' or file.get('reuse_codebook') is False:
                        continue
                    same_folder = next((m for m in options.get(file['file_id'], []) if
                                        (m['project_id'], m['folder']) == (selected['project_id'], selected['folder'])), None)
                    if same_folder:
                        file['reuse_codebook'] = {key: same_folder[key] for key in ('project_id', 'folder', 'file_id')}
            state['active_folder'] = folder
            check_context(state)
            target['setup_reviewed'] = True
            state['active_folder'] = next((name for name, details in state['folders'].items()
                                           if not details.get('setup_reviewed') and name not in published_folders(state)), None)
            save(path, state, 'folder_setup', edited_folder=folder)
            return jsonify(ok=True, state=state)

    @app.post('/api/projects/<project_id>/finish-folder')
    def finish_folder(project_id):
        """Unlock the next upload only after this folder has a committed SQL publication."""
        with lock:
            path, state = editable(project_id)
            folder = request.json.get('folder')
            if folder not in published_folders(state):
                raise ValueError('Finish ingestion and publication before moving to a new folder.')
            if state.get('active_folder') not in (None, folder):
                raise ValueError('Finish the current folder first.')
            state['folders'][folder]['setup_complete'] = True
            names = list(state['folders'])
            state['active_folder'] = next((name for name in names[names.index(folder) + 1:]
                                           if not state['folders'][name].get('setup_reviewed')
                                           and not state['folders'][name].get('setup_complete')), None)
            save(path, state, 'finish_folder')
            return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/restart-ingestion')
    def restart(project_id):
        """Reset this folder and later folders from material review; retain all original files.

        The explicit restart removes their published outputs, so editing an earlier
        folder cannot silently leave later copies based on its previous preparation.
        Earlier completed folders and the permanent user archive are retained.
        """
        with lock:
            path, state = editable(project_id)
            folder = request.json.get('folder')
            if request.json.get('confirmation') != 'restart' or folder not in state['folders']:
                raise ValueError('Confirm which folder to restart.')
            archive_project(path, state)
            names = list(state['folders'])
            reset = names[names.index(folder):]
            # Reverse order removes copies before their earlier source in this project.
            for name in reversed(reset):
                if name in published_folders(state):
                    publication = delete_atlas_folder(state['id'], name)
                    if publication:
                        state['atlas'] = publication
                    else:
                        state.pop('atlas', None)
            for name in reset:
                details = state['folders'][name]
                for key in ('setup_reviewed', 'setup_complete', 'reuse_dataset'):
                    details.pop(key, None)
                for key in ('preparations', 'folder_notes', 'descriptive_reviews', 'codebooks'):
                    state.get(key, {}).pop(name, None)
                for file in state['files'].values():
                    if file['folder'] == name:
                        file.pop('reuse_codebook', None)
                        state.get('findings', {}).pop(file['file_id'], None)
            for key in ('handoff_path', 'loader_resume', 'codebook_resume', 'atlas_handoff_path', 'final_summary_path'):
                state.pop(key, None)
            state.update(active_folder=folder, status='draft', question_form=None, pending='')
            save(path, state, 'restart_ingestion')
            return jsonify(ok=True)
