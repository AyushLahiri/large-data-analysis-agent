import argparse
import json
import secrets
import shutil
import threading
import traceback
from copy import deepcopy
from datetime import datetime, timezone
from flask import Flask, jsonify, request, send_file
from pathlib import Path
from psycopg2 import Error as PostgresError
from pypdf import PdfReader
from uuid import uuid4
from v_5.analyse.managers.routes import register_analysis
from v_5.app.config import FORMATS, MAX_UPLOAD_BYTES, RUNTIME
from v_5.ingest.initial_ingestion.state import extract_embedded_codebook
from v_5.ingest.publishing import publish_atlas, saved_atlas_plan
from v_5.ingest.summary import summary_ready, write_final_summary
from v_5.ingest.workflow import run_project
from v_5.ingest.lifecycle import RETRY_MESSAGE, mark_material_changes, reset_ingestion
from v_5.main_functions.session import Session, write_json
from v_5.main_functions.sql import delete_atlas_project, published_folders, rename_atlas_project
from v_5.main_functions.assets import ADMIN_USER_ID, archive_project, delete_archived_project, locked_setup_folders, matching_files
from v_5.main_functions.reuse_routes import register_reuse
from werkzeug.utils import secure_filename


def create_app(runtime=RUNTIME):
    """Build the local V5 web application and its project workflows.

    Inputs: runtime: directory holding project state, uploads and run outputs.
    Returns: Configured Flask app with project, analysis and static UI routes.
    Details: Creates per-app job maps, locks and request token. Project handlers use
        Session/run_project; register_analysis adds the separate chat endpoints.
    """
    root = (Path(runtime) / 'projects').resolve()
    root.mkdir(parents=True, exist_ok=True)
    app = Flask(__name__, static_folder='UI', static_url_path='/static')
    app.json.sort_keys = False
    app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_BYTES
    token, sessions, lock = secrets.token_urlsafe(32), {}, threading.RLock()
    extraction_jobs = {}
    analysis_jobs = {}

    def extract_in_background(project_id, path, source):
        """Create an embedded codebook without blocking the upload request.

        When: Phase 1 upload preparation for supported statistical files.
        Inputs: project_id/path: project owner; source: copied upload metadata.
        Returns: None; saves an embedded CSV asset or extraction status/error on the source
            file.
        Flow: Calls extract_embedded_codebook, then updates current project state under the app
            lock. Discards the result if the source was removed meanwhile.
        """
        file_id = 'file_' + uuid4().hex[:12]
        target = path / 'uploads' / (file_id + '.csv')
        try:
            extracted = extract_embedded_codebook(source['path'], target)
            status, error = ('complete' if extracted else 'not_available'), ''
        except Exception as exc:
            extracted, status, error = False, 'failed', str(exc)
        with lock:
            if not (path / 'project.json').exists():
                return
            session = sessions.get(project_id)
            state = session.state if session else read(project_id)[1]
            if source['file_id'] not in state['files']:
                target.unlink(missing_ok=True)
                return
            current = state['files'][source['file_id']]
            current['embedded_codebook_status'] = status
            current['embedded_codebook_error'] = error
            if extracted:
                state['files'][file_id] = dict(file_id=file_id,
                    filename=Path(source['filename']).stem + '_embedded_codebook.csv',
                    path=str(target.resolve()), format='csv', bytes=target.stat().st_size,
                    folder=current['folder'], role='asset', asset_kind='codebook',
                    embedded_for=source['file_id'], ignore=False, continuation=[],
                    agent=f'agent_embedded_csv_{file_id[5:]}',
                    note=f"Embedded dictionary for {source['filename']}.")
            save(path, state, 'extract_embedded_codebook')

    def schedule_codebooks(project_id, path, state):
        """Start pending embedded-dictionary extraction jobs.

        Inputs: project_id/path: owner; state: mutable upload metadata.
        Returns: None; marks eligible files pending and starts tracked background threads.
        Details: Used after upload and before ingestion. Skips published folders and files with
            an existing extraction status.
        """
        published = published_folders(state)
        for source in list(state['files'].values()):
            if source['folder'] in published or source['format'] not in {'spss', 'dta', 'dat'} or source.get('embedded_codebook_status'):
                continue
            source['embedded_codebook_status'] = 'pending'
            job = threading.Thread(target=extract_in_background, args=(project_id, path, dict(source)), daemon=True)
            extraction_jobs.setdefault(project_id, []).append(job)
            job.start()

    def project_directory(project_id):
        """Resolve a validated project ID under the runtime root.

        Inputs: project_id: 32 lowercase hexadecimal characters.
        Returns: Project Path; raises for invalid IDs.
        Details: Shared by project reads and deletion so route input cannot become an arbitrary
            filesystem path.
        """
        if len(project_id) != 32 or any(c not in '0123456789abcdef' for c in project_id):
            raise ValueError('Invalid project ID.')
        return root / project_id

    def read(project_id, *, allow_deleting=False):
        """Load a project's saved state.

        Inputs: project_id: validated project key; allow_deleting: show a cleanup-only state.
        Returns: (path, state): project directory and parsed project.json dict.
        Details: Used by routes without an active session. An unfinished deletion can only
            be viewed or retried, never edited or restarted.
        """
        path = project_directory(project_id)
        if not (path / 'project.json').is_file():
            staged = path.with_name(f'.deleting-{project_id}')
            if staged.exists():
                if not allow_deleting:
                    raise ValueError('Retry Delete project to finish cleanup.')
                marker = staged / 'project.json'
                state = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {'context': {'name': project_id}}
                # The project remains selectable only to retry its unfinished deletion.
                return staged, dict(id=project_id, version=2, context=state['context'],
                                    status='deleting', pending='Deletion is incomplete. Close any open project files and retry Delete project.',
                                    files={}, folders={}, messages=[], preparations={})
            raise ValueError('This project no longer exists.')
        return path, json.loads((path / 'project.json').read_text(encoding='utf-8'))

    def analysis_project(project_id):
        """Provide a stable project snapshot to analysis routes.

        Inputs: project_id: project key.
        Returns: (path, state): copied id/context/atlas fields from an active ingestion session,
            otherwise saved project state.
        Details: Passed to register_analysis. Copies live metadata under the session lock so
            analysis does not mutate ingestion state.
        """
        session = sessions.get(project_id)
        if session:
            with session.lock:
                return session.path, deepcopy({key: session.state[key] for key in ('id', 'context', 'atlas') if key in session.state})
        return read(project_id)

    def save(path, state, action, *, edited_folder=None):
        """Save project state and record the user-side change.

        Inputs: path: project directory; state: full project dict; action: change label;
            edited_folder: folder whose newly completed answers must replace its archive.
        Returns: None; replaces project.json and appends a timestamped state snapshot to
            inputs.jsonl.
        Details: Used by upload/context/project handlers; model/run events are recorded
            separately by Session.record.
        """
        for file in state['files'].values():
            file.pop('duplicates', None)  # Copy notices are derived from live files, never stale saved provenance.
        mark_material_changes(path, state, action)
        archive_project(path, state, edited_folder=edited_folder)
        write_json(path / 'project.json', state)
        with (path / 'inputs.jsonl').open('a', encoding='utf-8') as log:
            log.write(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'action': action, 'state': state}, ensure_ascii=False) + '\n')

    def editable(project_id):
        """Load a project only when ingestion is inactive.

        Inputs: project_id: project key.
        Returns: (path, state) from read; raises if a session is active.
        Details: Used before changing uploads/context or starting another workflow.
        """
        if project_id in sessions:
            raise ValueError('Stop this conversation before changing its files or context.')
        path, state = read(project_id)
        if state['status'] in {'running', 'awaiting_user', 'ready', 'stopping'}:
            state['status'] = 'interrupted'  # A server restart has no live worker to stop.
        return path, state

    def load_ingestion_handoff(path, state):
        """Read the saved material-review handoff for preparation.

        Inputs: path: project directory; state: handoff_path reference.
        Returns: Parsed handoff dict; raises if the file is missing or outside the project.
        Details: Used by preparation-only starts and publication retries that continue to later
            folders.
        """
        handoff = Path(state.get('handoff_path', '')).resolve()
        if not handoff.is_relative_to(path.resolve()) or not handoff.is_file():
            raise ValueError('No current saved ingestion handoff is available.')
        return json.loads(handoff.read_text(encoding='utf-8'))

    def delete_materials(path, state, folder, file_ids, action):
        """Remove unpublished uploads and invalidate their derived preparation state.

        Inputs: path/state: project; folder: owner key; file_ids: mutable removal set; action:
            input-log label.
        Returns: None; deletes uploads/embedded companions and saves the updated state.
        Details: Used by file/folder deletion routes. Rejects published folders and paths
            outside uploads, removes empty folders and clears stale handoffs.
        """
        before_ingestion = not state.get('log_directory')
        if folder in published_folders(state) or (folder in locked_setup_folders(state)
                and not (action == 'delete_folder' and before_ingestion)):
            raise ValueError('Published folders and their files are read-only.')
        archive_project(path, state)
        files = state['files']
        file_ids.update(key for key, file in files.items() if file.get('embedded_for') in file_ids)
        uploads = (path / 'uploads').resolve()
        targets = {key: Path(files[key]['path']).resolve() for key in file_ids}
        if any(not target.is_relative_to(uploads) for target in targets.values()):
            raise ValueError('Invalid upload path.')
        remove_folder = not any(key not in file_ids and file['folder'] == folder for key, file in files.items())
        if remove_folder:
            state['folders'].pop(folder, None)
            if state.get('active_folder') == folder:
                state['active_folder'] = next((name for name, details in state['folders'].items()
                                               if not details.get('setup_reviewed')), None)
        for key in ('preparations', 'folder_notes', 'descriptive_reviews', 'codebooks'):
            state.get(key, {}).pop(folder, None)
        if not published_folders(state):
            state.pop('atlas', None)
            state['status'] = 'draft'
        for key, target in targets.items():
            target.unlink(missing_ok=True)
            del files[key]
            state.get('findings', {}).pop(key, None)
        state.pop('handoff_path', None)
        state.pop('codebook_resume', None)
        state.pop('loader_resume', None)
        state.pop('atlas_handoff_path', None)
        state.update(question_form=None, pending='')
        save(path, state, action)

    def launch(project_id, session, run):
        """Register and start a project workflow in the background.

        Inputs: project_id: job key; session: runtime state; run: zero-argument callable
            returning a status.
        Returns: None; starts the work thread and records the active session.
        Details: Used by ingestion, preparation and publication routes so HTTP requests can
            return immediately.
        """
        sessions[project_id] = session

        def work():
            """Finish a background project job and clear its active entry.

            Inputs: Captures run, session and project_id from launch.
            Returns: None; saves final status or failure information and removes the job.
            Details: Stores run's status, records exceptions and distinguishes stopped work from
                failures; cleanup runs even after an error.
            """
            try:
                result = run()
                session.check_stop()
                session.state['status'] = result
            except InterruptedError:
                session.state.update(status='stopped', pending='Ingestion stopped. Choose Retry ingestion to continue saved work.')
            except Exception as error:
                session.record('error', 'workflow', {'error': str(error), 'traceback': traceback.format_exc()})
                session.message('app', str(error))
                session.state['status'] = 'stopped' if session.cancelled.is_set() else 'failed'
            finally:
                session.save()
                with lock:
                    sessions.pop(project_id, None)

        threading.Thread(target=work, daemon=True).start()

    def check_context(state):
        """Require the dataset context needed before material review.

        Inputs: state: uploads, folder descriptions and relationships.
        Returns: None on success; raises a user-facing ValueError for missing context.
        Details: The start route calls this for fresh ingestion. Published folders and
            ignored/supporting files are exempt from dataset questions.
        """
        required = ('description', 'time_geography', 'source', 'unit')
        published = published_folders(state)
        for file in state['files'].values():
            if (file['folder'] not in published and file['role'] != 'asset' and not file.get('ignore')
                    and state.get('active_folder') in (None, file['folder'])
                    and not state['folders'][file['folder']].get('reuse_dataset')):
                if any(not file.get('dataset_context', {}).get(key, '').strip() for key in required):
                    raise ValueError(f"Complete the four dataset questions for {file['filename']}.")
        for name, folder in state['folders'].items():
            if name in published:
                continue
            if state.get('active_folder') not in (None, name):
                continue
            if folder.get('reuse_dataset'):
                continue  # A confirmed exact copy keeps its original answers, including optional blanks.
            datasets = [f for f in state['files'].values() if f['folder'] == name and f['role'] != 'asset' and not f.get('ignore')]
            if len(datasets) >= 2 and not folder.get('relationship', '').strip():
                raise ValueError(f"Explain how the data files in {folder.get('display_name', name)} fit together.")
            if name != next(iter(state['folders'])) and not folder.get('description', '').strip():
                raise ValueError(f"Describe the folder {folder.get('display_name', name)}.")

    @app.before_request
    def local_only():
        """Reject nonlocal requests and stale write tokens.

        When: Flask before-request hook for every endpoint.
        Inputs: Reads request host/method and X-V5-Token; captures the app token.
        Returns: JSON error/403 on rejection; None lets the request continue.
        Flow: Requires localhost access and a constant-time token match for POST requests.
        """
        if request.host.split(':')[0] not in {'127.0.0.1', 'localhost'}:
            return jsonify(error='Local access only.'), 403
        if request.method == 'POST' and not secrets.compare_digest(request.headers.get('X-V5-Token', ''), token):
            return jsonify(error='Refresh the page before submitting.'), 403
        project_id = (request.view_args or {}).get('project_id')
        if project_id and request.endpoint not in {'project', 'delete_project'}:
            target = project_directory(project_id)
            if target.with_name(f'.deleting-{project_id}').exists():
                return jsonify(error='This project is being deleted. Retry Delete project to finish cleanup.'), 409

    @app.after_request
    def headers(response):
        """Apply the app's cache and browser content policies.

        Inputs: response: outgoing Flask response.
        Returns: The same response with security/cache headers set.
        Details: Flask after-request hook; applies consistently to API and static content.
        """
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        """Turn a validation exception into an API error.

        Inputs: error: ValueError raised by a handler/helper.
        Returns: (JSON error response, 400).
        Details: Registered Flask error handler; exposes the already user-facing validation
            message.
        """
        return jsonify(error=str(error)), 400

    @app.errorhandler(413)
    def too_large(error):
        """Explain a request that exceeds the upload size limit.

        Inputs: error: Flask request-size exception.
        Returns: (JSON error response, 413).
        Details: Registered handler for the configured 512 MiB request limit.
        """
        return jsonify(error='Upload exceeds 512 MiB.'), 413

    @app.errorhandler(PostgresError)
    def atlas_error(error):
        """Report a database failure without returning its raw details.

        Inputs: error: PostgreSQL exception.
        Returns: (JSON error response, 503); logs the original error.
        Details: Registered Flask database-error handler for project and analysis routes.
        """
        app.logger.error('Atlas project update failed: %s', error)
        return jsonify(error='Could not update Atlas. Check the connection and try again.'), 503

    @app.get('/')
    def home():
        """Serve the browser application page.

        When: GET /.
        Inputs: No route/body inputs.
        Returns: The static index.html response.
        Flow: Uses Flask's configured UI directory; scripts obtain state through the API.
        """
        return app.send_static_file('index.html')

    @app.get('/api/status')
    def status():
        """Supply the token used by browser write requests.

        When: GET /api/status.
        Inputs: Uses the token created with this app instance.
        Returns: JSON object containing token.
        Flow: The UI api helper sends this value as X-V5-Token on POST requests.
        """
        return jsonify(token=token)

    @app.get('/api/projects')
    def projects():
        """List current local projects for the selector.

        When: GET /api/projects.
        Inputs: Uses runtime project.json files; no request payload.
        Returns: JSON list of id/name records.
        Flow: Scans supported version-2 projects under the app lock. Unfinished deletions
            remain selectable so the user can retry cleanup after a reload.
        """
        result = []
        with lock:
            for path in root.glob('*/project.json'):
                p = json.loads(path.read_text(encoding='utf-8'))
                if p.get('version') == 2:
                    name = p['context']['name']
                    if path.parent.name.startswith('.deleting-'):
                        name += ' (deletion incomplete)'
                    result.append({'id': p['id'], 'name': name})
            # A crash after removing the final marker can leave an empty staging folder.
            for staged in root.glob('.deleting-*'):
                if staged.is_dir() and not (staged / 'project.json').exists():
                    project_id = staged.name.removeprefix('.deleting-')
                    project_directory(project_id)
                    result.append({'id': project_id, 'name': project_id + ' (deletion incomplete)'})
        return jsonify(result)

    @app.post('/api/projects')
    def create():
        """Create an empty project locally and in the Atlas registry.

        When: POST /api/projects.
        Inputs: JSON name: required display name.
        Returns: JSON initial project state with a new ID.
        Flow: Validates the name, initializes version-2 state, calls rename_atlas_project and
            saves the create event.
        """
        name = str(request.json.get('name', '')).strip()
        if not name:
            raise ValueError('Give the project a name.')
        state = dict(version=2, id=uuid4().hex, user_id=ADMIN_USER_ID, active_folder=None,
                     context={'name': name, 'description': '', 'folder_relationships': ''},
                     folders={}, files={}, messages=[], status='draft', pending='', preparations={})
        with lock:
            rename_atlas_project(state['id'], name)
            save(root / state['id'], state, 'create_project')
        return jsonify(state)

    @app.get('/api/projects/<project_id>')
    def project(project_id):
        """Return a project's live or saved state.

        When: GET /api/projects/<project_id>.
        Inputs: project_id: route key.
        Returns: JSON project state; stale saved active states are presented as interrupted.
        Flow: Prefers the in-memory Session under its lock, otherwise loads project.json without
            resuming work.
        """
        with lock:
            if project_id in sessions:
                s = sessions[project_id]
                with s.lock:
                    state = deepcopy(s.state)
                    if s.cancelled.is_set():
                        state.update(status='stopping', pending='Waiting for current agents to finish before stopping.')
                    duplicates = matching_files(s.path, state, refresh=False)
                    for key, file in state['files'].items():
                        file['duplicates'] = duplicates.get(key, [])
                    return jsonify(state)
            path, state = read(project_id, allow_deleting=True)
            if state['status'] == 'deleting':
                return jsonify(state)
            duplicates = matching_files(path, state, refresh=False)
            for key, file in state['files'].items():
                file['duplicates'] = duplicates.get(key, [])
            if state['status'] in {'running', 'awaiting_user', 'ready', 'stopping'}:
                state.update(status='interrupted', pending=(
                    'Ingestion interrupted. Choose Retry ingestion to continue saved work.'))
            return jsonify(state)

    @app.post('/api/projects/<project_id>/context')
    def context(project_id):
        """Save edited project, folder and file descriptions.

        When: POST .../<project_id>/context.
        Inputs: project_id and JSON context, folders, files and dataset_contexts mappings.
        Returns: JSON ok; saves updated state and invalidates old handoffs.
        Flow: Requires inactive ingestion and preserves published-folder metadata. Project
            naming uses its separate route.
        """
        with lock:
            path, state = editable(project_id)
            protected = published_folders(state) | locked_setup_folders(state)
            body = request.json
            # Unchanged fields sent by an older UI are harmless; edited locked answers are not.
            for name, details in body.get('folders', {}).items():
                values = details if isinstance(details, dict) else {'description': details}
                if name in protected and any(state['folders'][name].get(k, '') != v for k, v in values.items()):
                    raise ValueError('Restart the folder before changing its answers.')
            for field, key in (('files', 'note'), ('dataset_contexts', 'dataset_context')):
                for file_id, value in body.get(field, {}).items():
                    file = state['files'][file_id]
                    if file['folder'] in protected and file.get(key, '' if key == 'note' else {}) != value:
                        raise ValueError('Restart the folder before changing its answers.')
            state['context'].update({key: value for key, value in request.json['context'].items() if key != 'name'})
            published = protected
            for name, details in request.json.get('folders', {}).items():
                if name not in published:
                    values = details if isinstance(details, dict) else {'description': details}
                    state['folders'][name].update({key: value for key, value in values.items()
                                                   if key in {'description', 'relationship'}})
            for file_id, note in request.json.get('files', {}).items():
                if state['files'][file_id]['folder'] not in published:
                    state['files'][file_id]['note'] = note
            for file_id, details in request.json.get('dataset_contexts', {}).items():
                if state['files'][file_id]['folder'] not in published:
                    state['files'][file_id]['dataset_context'] = details
            save(path, state, 'edit_context')
            return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/rename')
    def rename(project_id):
        """Change a project display name locally and in Atlas.

        When: POST .../<project_id>/rename.
        Inputs: project_id and JSON name.
        Returns: JSON ok; rejects an empty name.
        Flow: Requires an inactive ingestion run, then updates the registry and saved state.
        """
        name = str(request.json.get('name', '')).strip()
        if not name:
            raise ValueError('Enter a project name.')
        with lock:
            path, state = editable(project_id)
            rename_atlas_project(project_id, name)
            state['context']['name'] = name
            save(path, state, 'rename_project')
        return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/rename-folder')
    def rename_folder(project_id):
        """Change a folder's display label without changing its key.

        When: POST .../<project_id>/rename-folder.
        Inputs: project_id and JSON folder/name.
        Returns: JSON ok; saves or records the rename.
        Flow: Updates display_name while ingestion is inactive, preserving the stable folder key.
        """
        name, folder = str(request.json.get('name', '')).strip(), request.json.get('folder')
        if not name:
            raise ValueError('Enter a folder name.')
        with lock:
            path, state = editable(project_id)
            if folder in locked_setup_folders(state) or folder in published_folders(state):
                raise ValueError('Restart the folder before changing its name.')
            state['folders'][folder]['display_name'] = name
            save(path, state, 'rename_folder')
        return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/delete')
    def delete_project(project_id):
        """Delete a confirmed project, including published data and analysis assets.

        When: POST .../<project_id>/delete.
        Inputs: project_id and JSON confirmation=delete.
        Returns: JSON ok, or error/409 for locked files; rejects active project workers.
        Flow: Renames the local directory into a deletion staging location, removes SQL records,
            restores on SQL failure and supports retrying interrupted cleanup.
        """
        with lock:
            if request.json.get('confirmation') != 'delete':
                raise ValueError('Confirm deletion first.')
            if project_id in sessions:
                raise ValueError('Stop ingestion before deleting this project.')
            if any(owner == project_id for owner, _ in analysis_jobs):
                raise ValueError('Stop all running analysis chats before deleting this project.')
            if any(job.is_alive() for job in extraction_jobs.get(project_id, [])):
                raise ValueError('Wait for the current file extraction to finish before deleting this project.')
            target = project_directory(project_id)
            staged = target.with_name(f'.deleting-{project_id}')
            if target.resolve() != target or staged.resolve() != staged:
                raise ValueError('Invalid project directory.')
            if not target.exists() and not staged.exists():
                # Retrying still removes SQL/originals if local working files are already gone.
                delete_atlas_project(project_id)
                delete_archived_project(target, {'id': project_id, 'user_id': ADMIN_USER_ID})
                return jsonify(ok=True)
            if target.exists():
                _, state = editable(project_id)
                try:
                    # Windows blocks this move if a workbook inside the project is open.
                    target.rename(staged)
                except PermissionError:
                    return jsonify(error='A project file is in use. Close it in Excel or any other app and try deleting again.'), 409
                try:
                    delete_atlas_project(project_id)
                except Exception:
                    staged.rename(target)
                    raise
            else:
                # Resume a confirmed deletion if the server stopped during cleanup.
                delete_atlas_project(project_id)
            marker = staged / 'project.json'
            archived_state = json.loads(marker.read_text(encoding='utf-8')) if marker.is_file() else {
                'id': project_id, 'user_id': ADMIN_USER_ID, 'version': 2, 'context': {'name': project_id}}
            try:
                delete_archived_project(target, archived_state)
                # Keep project.json until last so even a stopped server can resume cleanup.
                for child in staged.iterdir():
                    if child != marker:
                        if child.is_dir() and not child.is_symlink():
                            shutil.rmtree(child)
                        else:
                            child.unlink()
                marker.unlink(missing_ok=True)
                staged.rmdir()
            except OSError as error:
                if not marker.exists():
                    write_json(marker, archived_state)
                app.logger.warning('Project %s local cleanup blocked: %s', project_id, error)
                return jsonify(error='Atlas deletion is complete. Close any open project files and try again to finish local cleanup.'), 409
            extraction_jobs.pop(project_id, None)
        return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/delete-folder')
    def delete_folder(project_id):
        """Delete a confirmed unpublished folder and its materials.

        When: POST .../<project_id>/delete-folder.
        Inputs: project_id and JSON folder/confirmation=delete.
        Returns: JSON ok; rejects missing, active or published data.
        Flow: Collects that folder's file IDs and delegates removal/state invalidation to
            delete_materials.
        """
        with lock:
            path, state = editable(project_id)
            if request.json.get('confirmation') != 'delete':
                raise ValueError('Confirm deletion first.')
            folder = request.json.get('folder')
            if folder not in state['folders']:
                raise ValueError('This folder no longer exists.')
            files = {key for key, file in state['files'].items() if file['folder'] == folder}
            delete_materials(path, state, folder, files, 'delete_folder')
        return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/delete-file')
    def delete_file(project_id):
        """Delete a confirmed unpublished file and its embedded companion.

        When: POST .../<project_id>/delete-file.
        Inputs: project_id and JSON file_id/confirmation=delete.
        Returns: JSON ok; rejects missing, active or published data.
        Flow: Resolves the file's folder and delegates safe removal and handoff invalidation to
            delete_materials.
        """
        with lock:
            path, state = editable(project_id)
            if request.json.get('confirmation') != 'delete':
                raise ValueError('Confirm deletion first.')
            file_id = request.json.get('file_id')
            file = state['files'].get(file_id)
            if not file:
                raise ValueError('This file no longer exists.')
            delete_materials(path, state, file['folder'], {file_id}, 'delete_file')
        return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/upload')
    def upload(project_id):
        """Store uploaded files with their dataset roles and context.

        When: POST .../<project_id>/upload.
        Inputs: project_id; multipart files and folder/role fields, with JSON
            file_details/dataset_context and continuation choices.
        Returns: JSON ok; writes uploads and file metadata, resets preparation handoffs and
            schedules embedded extraction.
        Flow: Validates formats, PDF length and folder permissions before saving. Questions
            are checked only after the folder's files are uploaded and tagged.
        """
        with lock:
            path, state = editable(project_id)
            folder = request.form.get('folder', '').strip()
            role = request.form.get('role', 'primary')
            uploads = request.files.getlist('files')
            active = state.get('active_folder')
            if role == 'primary' and active and not state['folders'][active].get('setup_reviewed'):
                raise ValueError('Finish the current folder’s form before adding another dataset folder.')
            if role == 'primary' and not folder and uploads:
                folder = Path(uploads[0].filename).stem.replace('_', ' ').strip() or 'Dataset'
                base, number = folder, 2
                while folder in state['folders']:
                    folder = f'{base} {number}'
                    number += 1
            if not folder or role not in {'primary', 'continuation', 'asset', 'mixed'}:
                raise ValueError('Choose a folder and file role.')
            if role == 'primary' and (folder in state['folders'] or len(uploads) != 1):
                raise ValueError('Start a new folder with one dataset.')
            if role != 'primary' and folder not in state['folders']:
                raise ValueError('Upload the first dataset before adding files.')
            if folder in published_folders(state) or folder in locked_setup_folders(state):
                raise ValueError('Add a new folder for new data. Published folders are read-only.')
            if role != 'primary' and state.get('active_folder') not in (None, folder):
                raise ValueError('Add materials to the current folder first.')
            if any(Path(f.filename).suffix.lower() not in FORMATS for f in uploads):
                raise ValueError('Unsupported file extension.')
            if role == 'primary' and any(FORMATS[Path(f.filename).suffix.lower()] in {'pdf', 'md', 'dct', 'dcf'} for f in uploads):
                raise ValueError('Start with a data file. Add codebooks, reports and notes afterward.')
            if not uploads:
                raise ValueError('Choose at least one file.')
            for file in uploads:
                if Path(file.filename).suffix.lower() == '.pdf':
                    try:
                        pages = len(PdfReader(file.stream).pages)
                    except Exception as error:
                        raise ValueError(f'Could not read PDF {file.filename}: {error}') from error
                    finally:
                        file.stream.seek(0)
                    if pages > 100:
                        raise ValueError(f'{file.filename} has {pages} pages. PDFs may contain at most 100 pages.')
            descriptions = json.loads(request.form.get('file_details', '[]'))
            primary_context = json.loads(request.form.get('dataset_context', '{}'))
            if descriptions:
                if len(descriptions) != len(uploads):
                    raise ValueError('Describe each selected file.')
                for detail in descriptions:
                    if detail.get('role') not in ({'primary'} if role == 'primary' else {'asset', 'continuation'}):
                        raise ValueError('Choose a valid role for each file.')
                    if detail['role'] == 'continuation' and not detail.get('continuation'):
                        raise ValueError('Choose remaining rows, columns or both for each continuation.')
            if any(Path(file.filename).suffix.lower() == '.map' and
                   (descriptions[index]['role'] if descriptions else 'asset' if role == 'mixed' else role) != 'asset'
                   for index, file in enumerate(uploads)):
                raise ValueError('Add MAP files as supporting materials.')
            state['folders'].setdefault(folder, {'description': request.form.get('folder_description', '')})
            state['active_folder'] = folder
            state['folders'][folder]['setup_reviewed'] = False
            state['folders'][folder].pop('reuse_dataset', None)
            folder_number = list(state['folders']).index(folder) + 1
            file_ids = []
            for index, file in enumerate(uploads):
                detail = descriptions[index] if descriptions else {}
                file_role = detail.get('role', 'asset' if role == 'mixed' else role)
                file_id = 'file_' + uuid4().hex[:12]
                file_ids.append(file_id)
                name = Path(file.filename.replace('\\', '/')).name
                suffix = Path(name).suffix.lower()
                target = path / 'uploads' / (file_id + suffix)
                target.parent.mkdir(exist_ok=True)
                file.save(target)
                # Data files in one folder share a reviewer conversation; each supporting asset gets its own.
                agent = f'agent_folder_{folder_number:03d}_data' if file_role != 'asset' else f'agent_{secure_filename(Path(name).stem)[:5]}_{FORMATS[suffix]}_{len(state["files"])+1:03d}'
                state['files'][file_id] = dict(file_id=file_id, filename=name, path=str(target.resolve()), format=FORMATS[suffix],
                    bytes=target.stat().st_size, folder=folder, role=file_role, note=detail.get('note', request.form.get('note', '')),
                    dataset_context=detail.get('dataset_context', primary_context if file_role == 'primary' else {}),
                    asset_kind=detail.get('asset_kind', ''),
                    continuation=detail.get('continuation', request.form.getlist('continuation')), agent=agent, ignore=False,
                    tagged=file_role == 'primary' or bool(descriptions))
            state.pop('handoff_path', None)
            state.pop('codebook_resume', None)
            state.pop('loader_resume', None)
            state.pop('atlas_handoff_path', None)
            state['status'] = 'draft'
            schedule_codebooks(project_id, path, state)
            save(path, state, 'upload')
            matching_files(path, state)  # Bring earlier local projects into the exact-file index once on upload.
            return jsonify(ok=True, file_ids=file_ids)

    @app.post('/api/projects/<project_id>/start')
    def start(project_id):
        """Start material review or resume directly from its handoff.

        When: POST .../<project_id>/start.
        Inputs: project_id; retry=true resumes saved work, while a plain request starts fresh.
        Returns: JSON ok; launches a logged ingestion Session.
        Flow: Requires completed forms for all pending folders, checks fresh context or
            loads the saved handoff, then starts one workflow across the unpublished folders.
        """
        with lock:
            path, state = editable(project_id)
            retry = bool((request.get_json() or {}).get('retry') or (request.get_json() or {}).get('prepare_only'))
            if retry and state.get('ingestion_changed'):
                raise ValueError(RETRY_MESSAGE)
            if retry and (state.get('atlas_handoff_path') or summary_ready(state)):
                return publish(project_id)
            if not state['files']:
                raise ValueError('Upload a dataset first.')
            active = state.get('active_folder')
            if active and not state['folders'][active].get('setup_reviewed'):
                raise ValueError('Review the file and folder forms before starting ingestion.')
            pending_folders = {file['folder'] for file in state['files'].values()
                               if not file.get('ignore') and file['role'] in {'primary', 'continuation'}} - published_folders(state)
            if any(not state['folders'][folder].get('setup_reviewed') for folder in pending_folders):
                raise ValueError('Complete every folder’s form before choosing Ingest materials.')
            state['active_folder'] = None  # The explicit project start processes all ready folders.
            if not retry:
                check_context(state)
            handoff = None
            if retry and state.get('handoff_path'):
                handoff = load_ingestion_handoff(path, state)
            if not retry:
                try:
                    reset_ingestion(path, state)
                except OSError:
                    return jsonify(error='Close any open ingestion output files, then start ingestion again.'), 409
            state.update(status='running', pending='', error=None)
            state.pop('atlas_handoff_path', None)
            if not retry:
                state.pop('codebook_resume', None)
                state.pop('loader_resume', None)
                schedule_codebooks(project_id, path, state)
            archive_project(path, state)
            session = Session(path, state)
            session.handoff = handoff
            def prepare():
                """Wait for embedded dictionaries before running ingestion.

                Inputs: Captures project extraction jobs and the new session.
                Returns: Status string from run_project.
                Details: Background callable passed to launch; ensures phase 1 sees all dictionary
                    assets before reviewing materials.
                """
                for job in extraction_jobs.get(project_id, []):
                    job.join()
                session.check_stop()
                return run_project(session)

            launch(project_id, session, prepare)
            return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/publish')
    def publish(project_id):
        """Retry Atlas publication or finish the project summary.

        When: POST .../<project_id>/publish.
        Inputs: project_id; uses saved preparation/publication state.
        Returns: JSON ok; launches background publication or summary work.
        Flow: If all folders are published, calls write_final_summary. Otherwise loads
            saved_atlas_plan and any handoff required to resume later folders.
        """
        with lock:
            path, state = editable(project_id)
            if state.get('ingestion_changed'):
                raise ValueError(RETRY_MESSAGE)
            if summary_ready(state):
                state.update(status='running', activity='Writing the project summary', pending='', error=None)
                session = Session(path, state)
                launch(project_id, session, lambda: write_final_summary(session))
                return jsonify(ok=True)
            plan = saved_atlas_plan(path, state)
            handoff = load_ingestion_handoff(path, state) if plan.get('next_folder') else None
            state.update(status='running', activity='Publishing to Atlas', pending='', error=None)
            session = Session(path, state)

            def resume_upload():
                """Complete a publication retry and continue the remaining workflow.

                Inputs: Captures session, saved plan and optional ingestion handoff.
                Returns: Status from publication, resumed run_project or final summary.
                Details: Calls publish_atlas first; resumes the next folder when a handoff exists,
                    otherwise writes the summary once all data is published.
                """
                result = publish_atlas(session, plan)
                if handoff:
                    session.handoff = handoff
                    return run_project(session)
                if summary_ready(session.state):
                    return write_final_summary(session)
                return result

            launch(project_id, session, resume_upload)
            return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/answer')
    def answer(project_id):
        """Deliver a user reply to a waiting ingestion conversation.

        When: POST .../<project_id>/answer.
        Inputs: project_id; JSON text or form_id/answers, with optional additional context.
        Returns: JSON ok; queues text, or a variable-to-instruction dict for codebook review.
        Flow: Validates the current form and required review instructions, records the displayed
            answer, saves running state and wakes Session.answer.
        """
        with lock:
            session = sessions.get(project_id)
            if not session or session.state['status'] not in {'awaiting_user', 'ready'}:
                raise ValueError('There is no question waiting for a reply.')
            text = str(request.json.get('text', '')).strip()
            form = session.state.get('question_form')
            codebook_review = form and form.get('kind') == 'codebook_review'
            review_answers = None
            if codebook_review and 'answers' not in request.json:
                raise ValueError('Use the codebook review table to send your instructions.')
            if 'answers' in request.json:
                if not form or request.json.get('form_id') != form['id']:
                    raise ValueError('This question has changed. Please use the current form.')
                answers = request.json['answers']
                if codebook_review:
                    if not isinstance(answers, dict):
                        raise ValueError('Use the codebook review table to send your instructions.')
                    review_answers = {q['id']: str(answers.get(q['id']) or '').strip() for q in form['questions']}
                    if not all(review_answers.values()):
                        raise ValueError('Add an instruction for each flagged variable, or say to use best judgment.')
                text = '\n'.join(q['question'] + '\n' + str(answers.get(q['id'], '')).strip() for q in form['questions'])
                extra = str(request.json.get('text', '')).strip()
                if extra:
                    text += '\nAdditional context: ' + extra
                session.record('user_form_answer', 'user', {'form': form, 'answers': answers, 'additional_context': extra})
            if not text:
                raise ValueError('Enter a reply.')
            session.message('You', text)
            session.state['status'] = 'running'
            session.save()
            # Ordinary questions resume with text; final-codebook review expects instructions keyed by variable name.
            session.answers.put(review_answers if codebook_review else text)
            return jsonify(ok=True)

    @app.post('/api/projects/<project_id>/stop')
    def stop(project_id):
        """Let current agent replies finish, then stop before the next workflow action.

        When: POST .../<project_id>/stop.
        Inputs: project_id: route key.
        Returns: JSON ok whether or not a session exists.
        Flow: Sets stopping immediately for the UI. Accepted requests finish and checkpoint
            their replies; check_stop prevents new work and launch records the final stop.
        """
        with lock:
            if project_id in sessions:
                session = sessions[project_id]
                session.record('stop_requested', 'user', {})
                session.cancelled.set()
                session.state.update(status='stopping', pending='Waiting for current agents to finish before stopping.')
                session.save()
            return jsonify(ok=True)

    @app.get('/api/projects/<project_id>/download/<path:filename>')
    def download(project_id, filename):
        """Download a saved file from within a project.

        When: GET .../<project_id>/download/<filename>.
        Inputs: project_id: owner; filename: project-relative path.
        Returns: Attachment response; raises if missing or outside the project.
        Flow: Resolves the full path before send_file so download links cannot escape the
            project directory.
        """
        path, _ = read(project_id)
        target = (path / filename).resolve()
        if not target.is_relative_to(path.resolve()) or not target.is_file():
            raise ValueError('File not found in this project.')
        return send_file(target, as_attachment=True)

    register_reuse(app, read, editable, save, lock, check_context)
    register_analysis(app, analysis_project, lock, analysis_jobs)
    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--runtime', type=Path, default=RUNTIME)
    args = parser.parse_args()
    create_app(args.runtime).run(host='127.0.0.1', port=args.port, debug=False, threaded=True)
