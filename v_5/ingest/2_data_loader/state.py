from pathlib import Path
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json


def loader_workspace(session, folder, destination):
    """Find the current folder's workspace or save a pointer before its first model call.

    Inputs: session/project, folder name and proposed new workspace. Returns: Path.
    Only this project's saved runs may be resumed. The pointer is cleared when inputs
    change or confirmed preparation passes to codebooks; it stores no duplicate payload.
    """
    saved = session.state.get('loader_resume')
    if saved and saved['folder'] == folder:
        workspace = Path(saved['workspace']).resolve()
        if not workspace.is_relative_to((session.path / 'runs').resolve()):
            raise ValueError('Saved loader workspace is outside this project.')
        if not workspace.is_dir():
            raise ValueError('Saved loader workspace is missing.')
        checkpoint = read_json(workspace / 'checkpoint.json', {})
        if checkpoint.get('context') and checkpoint['context']['folder'] != folder:
            raise ValueError('Saved loader workspace belongs to a different folder.')
        return workspace
    destination.mkdir(parents=True, exist_ok=True)
    session.state['loader_resume'] = {'folder': folder, 'workspace': str(destination)}
    session.save()
    return destination


def progress(self, folder, number, used):
    """Save the loader's current token budget for display.

    Inputs: self: session; folder: dataset; number: conversation number; used: consumed
        tokens.
    Returns: None; saves loader_budget with folder/session/used/remaining.
    Details: Phase 2 calls this through Session.progress; remaining is measured against the
        250,000-token budget.
    """
    with self.lock:
        self.state['loader_budget'] = dict(folder=folder, session=number, used=used, remaining=250000-used)
        self.save()


def phase_report(self, folder, payload):
    """Save the latest preparation report for one folder.

    Inputs: self: session; folder: dataset key; payload: loader reply including phase
        details.
    Returns: None; records a phase_report event and updates state.preparations[folder].
    Details: Phase 2 calls this through Session.phase_report so the UI and later phases see
        the same saved report.
    """
    self.record('phase_report', 'data_loader_agent', {'folder': folder, **payload})
    with self.lock:
        self.state.setdefault('preparations', {})[folder] = payload
        self.save()
