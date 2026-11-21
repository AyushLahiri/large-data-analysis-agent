import json
import psycopg2
import re
import shutil
import threading
import traceback
from contextlib import nullcontext
from flask import jsonify, request
from pathlib import Path
from uuid import uuid4
from v_5.analyse.main.memory import AnalysisContextLimit, rollover_finished_contexts
from v_5.analyse.main.sub_agents.errors import AnalysisExecutionLimit
from v_5.analyse.main.workflow import analyse
from v_5.analyse.managers.chats import (
    archive_chat, chat_title, create_chat, delete_chat, list_chats, restore_chat, save_turn_feedback,
)
from v_5.analyse.managers.turns import complete_turn, initialize_turns, new_turn, write_transcript
from v_5.analyse.managers.tables import save_tables, selected_table
from v_5.analyse.sql_assets.catalog import AtlasCatalog
from v_5.main_functions.session import Session, write_json
from v_5.main_functions.sql import published_folders


ANSWER_AGENTS = {'analysis_orchestrator', 'formatter', 'analysis_agent', 'visualizer'}


def register_analysis(app, read_project, lock, analysis_jobs):
    """Attach the analysis chat endpoints to the Flask app.

    Inputs: app: Flask instance; read_project: project loader; lock: shared app lock;
        analysis_jobs: active-session map.
    Returns: None; registers routes and their shared local helpers.
    Details: Called by create_app. Routes coordinate local state, SQL chat persistence and
        background analyse calls.
    """
    def directory(path, chat_id):
        """Resolve a valid chat ID to its local archive directory.

        Inputs: path: project directory; chat_id: 32-character hexadecimal key.
        Returns: Path to legacy analysis/ or analysis_chats/<id>; raises for invalid IDs.
        Details: Shared by chat routes. A chat whose ID equals the project ID uses the older
            single-chat location.
        """
        if not re.fullmatch('[0-9a-f]{32}', chat_id):
            raise ValueError('Invalid chat ID.')
        return path / 'analysis' if chat_id == path.name else path / 'analysis_chats' / chat_id

    def sync_chat(path, chat_id, state):
        """Push completed local turns to Atlas and save the checkpoint.

        Inputs: path: project directory; chat_id: key; state: mutable local chat.
        Returns: None; updates atlas_archived_through when archival advances.
        Details: Uses archive_chat and registers legacy chats as needed. Database errors are
            logged so local work can be retried later.
        """
        register = chat_id == path.name and 'atlas_archived_through' not in state
        try:
            through = archive_chat(path.name, chat_id, state, register=register)
        except psycopg2.Error as error:
            app.logger.warning('Chat %s is saved locally and will retry Atlas: %s', chat_id, error)
            return
        if state.get('atlas_archived_through') != through:
            state['atlas_archived_through'] = through
            write_json(directory(path, chat_id) / 'session.json', state)

    def read_state(path, chat_id, *, sync=False):
        """Load or restore a chat's local session state.

        Inputs: path/chat_id: archive location; sync: whether to archive completed turns now.
        Returns: Chat state dict; raises if deletion is underway or the chat is absent.
        Details: Restores missing state from Atlas, migrates legacy turns/transcripts and treats
            stale saved running states as idle. Active sessions are handled separately by
            routes.
        """
        archive = directory(path, chat_id)
        target = archive / 'session.json'
        if not target.is_file():
            if archive.with_name(f'.deleting-{chat_id}').exists():
                raise ValueError('This chat is being deleted.')
            state = restore_chat(path.name, chat_id)
            if state is None:
                raise ValueError('This chat no longer exists.')
            initialize_turns(state)
            save_tables(archive, chat_id, state, restore=True)
            write_json(target, state)
            return state
        state = json.loads(target.read_text(encoding='utf-8'))
        legacy_chat = 'turns' not in state
        migrated = initialize_turns(state)
        if save_tables(archive, chat_id, state) or migrated:
            if legacy_chat:
                for turn in state['turns']:
                    if turn['answer_id']:
                        write_transcript(path, state, turn)
                        state['transcript_path'] = 'analysis/transcript.txt'
            write_json(target, state)
        if state['status'] == 'running':
            state['status'] = 'idle'
        if sync:
            sync_chat(path, chat_id, state)
        return state

    def visible_state(path, state):
        """Prepare chat state for browser display.

        Inputs: path: project root; state: current or saved chat.
        Returns: State dict with title and safe relative log_path, omitting the chat-wide
            variable registry.
        Details: Used by get_analysis; preserves per-turn data while keeping internal
            accumulation out of the top-level response.
        """
        run = state.get('log_directory')
        log = Path(run) / 'timeline.md' if run else None
        visible = {key: value for key, value in state.items() if key != 'variables_used'}
        if 'analysis_context' in visible:
            visible['analysis_context'] = {name: {key: value for key, value in budget.items()
                                                  if key in ('generation', 'used_tokens', 'limit_hit')}
                                           for name, budget in state['analysis_context'].items()}
        return {**visible, 'title': chat_title(state), 'log_path':
                log.relative_to(path).as_posix() if log and log.is_relative_to(path) and log.is_file() else None}

    @app.get('/api/projects/<project_id>/analysis/tree')
    def get_analysis_tree(project_id):
        """Return the published analysis asset tree.

        When: GET /api/projects/<project_id>/analysis/tree.
        Inputs: project_id: route key; captures the project reader.
        Returns: JSON folder asset mapping, or an empty object when nothing is published.
        Flow: Reads project state under the app lock, then asks AtlasCatalog.artifact_tree for
            SQL-backed availability and counts.
        """
        with lock:
            _, project = read_project(project_id)
        if not published_folders(project):
            return jsonify({})
        return jsonify(AtlasCatalog(project, '').artifact_tree())

    @app.get('/api/projects/<project_id>/analysis/chats')
    def get_chats(project_id):
        """Return the project's available analysis chats.

        When: GET /api/projects/<project_id>/analysis/chats.
        Inputs: project_id: route key; uses local archives and active jobs.
        Returns: JSON list of id/title records.
        Flow: Synchronizes inactive local chats before listing Atlas records; if the database is
            unavailable, returns local entries when available.
        """
        with lock:
            path, _ = read_project(project_id)
            local = []
            ids = [project_id, *(entry.name for entry in (path / 'analysis_chats').glob('*')
                                 if re.fullmatch('[0-9a-f]{32}', entry.name))]
            for chat_id in ids:
                target = directory(path, chat_id) / 'session.json'
                if target.is_file():
                    live = analysis_jobs.get((project_id, chat_id))
                    with live.lock if live else nullcontext():
                        state = live.state if live else read_state(path, chat_id, sync=True)
                        local.append({'id': chat_id, 'title': chat_title(state)})
            try:
                return jsonify(list_chats(project_id))
            except psycopg2.OperationalError:
                if not local:
                    raise
                return jsonify(local)

    @app.post('/api/projects/<project_id>/analysis/chats')
    def new_chat(project_id):
        """Create an empty chat for a published project.

        When: POST /api/projects/<project_id>/analysis/chats.
        Inputs: project_id: route key.
        Returns: JSON id/title for the new chat; rejects an unpublished project.
        Flow: Allocates a chat ID and calls create_chat while holding the app lock.
        """
        with lock:
            _, project = read_project(project_id)
            if not published_folders(project):
                raise ValueError('Choose a published project first.')
            chat_id = uuid4().hex
            create_chat(project_id, chat_id)
            return jsonify(id=chat_id, title='New chat')

    @app.post('/api/projects/<project_id>/analysis/chats/<chat_id>/delete')
    def remove_chat(project_id, chat_id):
        """Delete an inactive chat from SQL and local storage.

        When: POST .../analysis/chats/<chat_id>/delete.
        Inputs: project_id/chat_id: route keys.
        Returns: JSON ok on success, or error/409 for active work or locked files.
        Flow: Stages the local directory rename, deletes SQL records, restores the directory if
            SQL fails, then removes staged files.
        """
        with lock:
            path, _ = read_project(project_id)
            archive = directory(path, chat_id)
            # Hide the local archive first, but keep a reversible rename until SQL deletion succeeds.
            staged = archive.with_name(f'.deleting-{chat_id}')
            if any(target.resolve() != target or not target.is_relative_to(path) for target in (archive, staged)):
                raise ValueError('Invalid chat directory.')
            if (project_id, chat_id) in analysis_jobs:
                return jsonify(error='Wait until the current answer finishes before deleting this chat.'), 409
            moved = archive.exists()
            if moved:
                try:
                    archive.rename(staged)
                except PermissionError:
                    return jsonify(error='Close any open files from this chat, then try again.'), 409
            try:
                delete_chat(project_id, chat_id)
            except Exception:
                if moved:
                    staged.rename(archive)
                raise
            if staged.exists():
                try:
                    shutil.rmtree(staged)
                except PermissionError:
                    return jsonify(error='Close any open chat files and retry to finish local cleanup.'), 409
            return jsonify(ok=True)

    @app.get('/api/projects/<project_id>/analysis/chats/<chat_id>')
    def get_analysis(project_id, chat_id):
        """Return the current visible state of one chat.

        When: GET .../analysis/chats/<chat_id>.
        Inputs: project_id/chat_id: route keys.
        Returns: JSON chat state with display title/log link and stopping status when
            cancellation is pending.
        Flow: Prefers the active Session under its lock; otherwise loads saved/restored state
            through read_state.
        """
        with lock:
            path, _ = read_project(project_id)
            session = analysis_jobs.get((project_id, chat_id))
            if session:
                with session.lock:
                    state = session.state
                    if session.cancelled.is_set():
                        state = {**state, 'status': 'stopping', 'activity': 'Stopping analysis'}
                    return jsonify(visible_state(path, state))
            return jsonify(visible_state(path, read_state(path, chat_id)))

    @app.get('/api/projects/<project_id>/analysis/chats/<chat_id>/visualizations/<turn_id>')
    def get_visualization(project_id, chat_id, turn_id):
        """Return a published chart and its original source table for the chat card.

        When: The UI first opens a visualization from a saved message.
        Inputs: project_id and chat_id identify the archive. turn_id identifies
            the turn that published this chart.
        Returns: JSON with table and views, or 404 for an unavailable publication.
        Flow: Find the message reference in live or saved state, load the local
            asset and verify its publication turn and source table IDs.
        """
        with lock:
            path, _ = read_project(project_id)
            archive = directory(path, chat_id)
            live = analysis_jobs.get((project_id, chat_id))
            with live.lock if live else nullcontext():
                state = live.state if live else read_state(path, chat_id)
                visual = next((visual for msg in state['messages'] if msg.get('turn_id') == turn_id
                               and (visual := (msg.get('details') or {}).get('visualization'))), None)
                if not visual or visual.get('turn_id') != turn_id or not re.fullmatch(r'turn_\d+', turn_id):
                    return jsonify(error='This visualization is unavailable.'), 404
                table = selected_table(archive, state, visual['table_id'])
                target = archive / 'visualizations' / (turn_id + '.json')
                if not target.is_file():
                    return jsonify(error='The local visualization file is missing. Use Visualize on the source table to recreate it.'), 404
                asset = json.loads(target.read_text(encoding='utf-8'))
                if asset['table_id'] != table['table_id'] or asset['turn_id'] != turn_id:
                    raise ValueError('The visualization does not match this source table.')
        return jsonify(table=table, views=asset['views'])

    @app.post('/api/projects/<project_id>/analysis/chats/<chat_id>/stop')
    def stop_analysis(project_id, chat_id):
        """Request cancellation of an active chat calculation.

        When: POST .../analysis/chats/<chat_id>/stop.
        Inputs: project_id/chat_id: route keys.
        Returns: JSON stopping boolean indicating whether an active session was found.
        Flow: Looks up the job under the app lock, then calls Session.stop outside that lock.
        """
        with lock:
            session = analysis_jobs.get((project_id, chat_id))
        if session:
            session.stop()
        return jsonify(stopping=session is not None)

    @app.post('/api/projects/<project_id>/analysis/chats/<chat_id>/turns/<turn_id>/feedback')
    def analysis_feedback(project_id, chat_id, turn_id):
        """Store a user rating on an analysis answer.

        When: POST .../analysis/chats/<chat_id>/turns/<turn_id>/feedback.
        Inputs: Route IDs and JSON rating (up/down), plus required comment for down.
        Returns: JSON saved feedback; rejects invalid ratings or missing answers.
        Flow: Locks the active/saved turn, writes archived feedback to SQL, updates local state
            and synchronizes newly completed records when needed.
        """
        body = request.get_json() or {}
        rating = body.get('rating')
        if rating not in ('up', 'down'):
            raise ValueError('Choose thumbs up or thumbs down.')
        feedback = {'rating': rating}
        if rating == 'down':
            comment = body.get('comment')
            if not isinstance(comment, str) or not comment.strip():
                raise ValueError('Please tell us what went wrong.')
            feedback['comment'] = comment.strip()
        with lock:
            path, _ = read_project(project_id)
            session = analysis_jobs.get((project_id, chat_id))
            with session.lock if session else nullcontext():
                state = session.state if session else read_state(path, chat_id)
                turn = next((item for item in state['turns'] if item['id'] == turn_id), None)
                if not turn or not any(message.get('turn_id') == turn_id and message['agent'] in ANSWER_AGENTS
                                       for message in state['messages']):
                    raise ValueError('This answer is no longer available.')
                archived = int(turn_id.rsplit('_', 1)[1]) <= state.get('atlas_archived_through', 0)
                if archived:
                    save_turn_feedback(project_id, chat_id, turn_id, feedback)
                turn['feedback'] = feedback
                if session:
                    session.save()
                else:
                    write_json(directory(path, chat_id) / 'session.json', state)
                    if not archived:
                        sync_chat(path, chat_id, state)
        return jsonify(feedback)

    @app.post('/api/projects/<project_id>/analysis/chats/<chat_id>')
    def post_analysis(project_id, chat_id):
        """Start a chat opening message or a new analysis question.

        When: POST .../analysis/chats/<chat_id>.
        Inputs: Route IDs, question text and optional folder scope. A Visualize
            click supplies table_id. chart_width gives the preview width in CSS pixels.
        Returns: JSON ok/turn_id, or error/409 when another question is running.
        Flow: Validate the scope and optional table selection, create the turn,
            record the question and launch analysis in a background thread.
        """
        body = request.get_json() or {}
        folder = str(body.get('folder') or '')
        text = str(body.get('text') or '').strip()
        table_id = body.get('table_id')
        if len(text) > 8000:
            raise ValueError('Keep each question under 8,000 characters.')
        with lock:
            path, project = read_project(project_id)
            archive = directory(path, chat_id)
            job = (project_id, chat_id)
            published = published_folders(project)
            if not published or (folder and folder not in published):
                raise ValueError('Choose a published project or folder first.')
            if job in analysis_jobs:
                if text or table_id is not None:
                    return jsonify(error='The current analysis is still running. Your question is still in the composer.'), 409
                return jsonify(ok=True)
            state = read_state(path, chat_id)
            if table_id is not None:
                if not isinstance(table_id, str):
                    raise ValueError('Choose a saved table.')
                table = selected_table(archive, state, table_id, folder)
                text = text or f"Visualize table {table['ordinal']} from {table['turn_id']} — {table['title']}."
            if not text and any(message['agent'] in ANSWER_AGENTS for message in state['messages']):
                return jsonify(ok=True)
            state.update(status='running', activity='Understanding your question')
            turn = new_turn(state['turns'], text, folder) if text else None
            session = Session(path, state, analysis=True, turn_id=turn['id'] if turn else None, analysis_directory=archive)
            width = body.get('chart_width')
            if type(width) is int and width > 0:
                session.chart_width = max(180, min(width, 1920))
            analysis_jobs[job] = session
            if turn:
                turn['question_id'] = session.message('You', text, details={'table_id': table_id} if table_id else None)
                session.save()

            def work():
                """Run analysis and finish its history and archive bookkeeping.

                When: Background worker launched by post_analysis.
                Inputs: Captures session, project, folder, text, turn and archive location.
                Returns: None; saves answer/errors, transcript, memory state and SQL archive, then
                    clears the active job.
                Flow: Calls analyse, distinguishes stop/error limits, completes the turn when
                    appropriate, rolls finished contexts and returns the chat to idle even on failure.
                """
                try:
                    if table_id:
                        analyse(session, project, folder, text, table_id=table_id)
                    else:
                        analyse(session, project, folder, text)
                except Exception as error:
                    if session.cancelled.is_set():
                        session.record('analysis_stopped', 'analysis', {})
                        session.message('analysis', 'Analysis stopped.')
                    elif isinstance(error, AnalysisExecutionLimit):
                        session.cancelled.set()
                        session.record('analysis_execution_limit', 'analysis', {'count': session.analysis_error_count})
                        session.message('analysis', str(error), 'error')
                    elif isinstance(error, AnalysisContextLimit):
                        session.record('analysis_context_limit', 'analysis', {'error': str(error)})
                        session.message('analysis', str(error), 'error')
                    else:
                        session.record('error', 'analysis', {'error': str(error), 'traceback': traceback.format_exc()})
                        session.message('analysis', 'I couldn’t complete the analysis. Please try again.', 'error')
                finally:
                    if turn:
                        try:
                            answered = any(message.get('turn_id') == turn['id'] and message['agent'] in ANSWER_AGENTS
                                           for message in state['messages'])
                            if answered or not session.cancelled.is_set():
                                complete_turn(session, turn)
                                rollover_finished_contexts(session)
                            with session.lock:
                                save_tables(archive, chat_id, state)
                            write_transcript(path, state, turn, session.run_path, directory=archive)
                            state['transcript_path'] = (archive / 'transcript.txt').relative_to(path).as_posix()
                        except Exception as error:
                            session.record('turn_archive_error', 'analysis', {'error': str(error), 'traceback': traceback.format_exc()})
                    with lock:
                        if not turn:
                            save_tables(archive, chat_id, state)
                        state['status'] = 'idle'
                        session.save()
                        try:
                            if turn and turn['answer_id']:
                                sync_chat(path, chat_id, state)
                        finally:
                            analysis_jobs.pop(job, None)

            threading.Thread(target=work, daemon=True).start()
            return jsonify(ok=True, turn_id=turn['id'] if turn else None)
