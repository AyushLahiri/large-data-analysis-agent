import json
from pathlib import Path
from v_5.app.docker import run_code
from v_5.ingest.codebook_ingestion.sub_agents.agent import CODEBOOK_TABLE_PROMPT
from v_5.ingest.codebook_ingestion.sub_agents.structured_state import StructuredCalls, StructuredRunner


def structured_codebook(session, file, files, context, prepared, workspace, directory, environment):
    """Extract or resume one non-PDF codebook through saved current-turn flags.

    When: Phase 4 codebook_table_agent, selected by ingest_codebooks.
    Inputs: session/file/files: services and sources; context/prepared: folder decisions and
        loader report; workspace/directory: output paths; environment: installed packages.
        source_file also carries this codebook's ingestion finding (with parser_rules) and
        reviewer preview, when the saved context has them.
    Returns: Normalized source DataFrame, with notes and verification saved alongside it.
    Flow: StructuredRunner retains the thread, pending reply, code result and glitch report.
        Code runs in the existing read-only sandbox; a partial final export is overwritten.
        Prompt, reply contract, Sol/medium settings and the 250k rollover budget are unchanged.
    """
    # The ingestion reviewer's finding (with parser_rules) and preview sit beside the file itself.
    initial = {'source_file': {**file, 'path': f"/input/{file['file_id']}{Path(file['path']).suffix}",
                               'finding': context.get('files', {}).get(file['file_id'], {}).get('finding', {}),
                               'ingestion_preview': context.get('initial_file_context', {}).get(file['file_id'])},
        'folder_context': context, 'loader_report': prepared,
        'saved_turns': '/work/' + directory.relative_to(workspace).as_posix(),
        'continuation': 'Read saved prior feedback/code when needed; reuse completed work.'}

    def execute(code, current):
        """Return raw results so the runner saves failures before calling glitch."""
        session.check_stop()
        return run_code(code, files, workspace, current, read_only=True, cancelled=session.cancelled)

    def observe(event, state):
        """Show extraction progress while recovery remains owned by the runner."""
        if event == 'reply':
            session.message('codebook_table_agent', state['reply']['message'], 'thinking')
        elif event == 'execution':
            result = state['result']
            session.message('execution', 'Code finished successfully.' if result.get('exit_code') == 0
                            else 'The code encountered an error.', 'thinking',
                            {'code': state['reply']['code'], 'result': result})
        elif event == 'glitch':
            session.present_reply('glitch_agent', json.dumps(state['glitch']['response']))

    return StructuredRunner(directory, workspace, initial,
        CODEBOOK_TABLE_PROMPT + '\nInstalled packages:\n' + environment,
        StructuredCalls(session), execute, observer=observe, check_stop=session.check_stop).run()
