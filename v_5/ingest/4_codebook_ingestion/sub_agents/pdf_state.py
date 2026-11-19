"""Current-page recovery for PDF readers and their mergers; no local chat archive."""
import contextlib
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import threading
from openai.lib._parsing._responses import type_to_text_format_param
from v_5.ingest.codebook_ingestion.tables import normalize_rows

FLAGS = ('sent', 'output_received', 'table_created', 'merged')
SESSION_BUDGET = 250_000

def read_json(path, default=None):
    """Read saved state, returning default only when the file does not exist.

    Inputs: path: JSON file; default: missing-file value.
    Returns: Decoded JSON; malformed saved state raises instead of being ignored.
    Details: Recovery distinguishes absent files from damaged authoritative state.
    """
    path = Path(path)
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def atomic_text(path, text, before_replace=None):
    """Replace a text file after its complete contents have reached disk.

    Inputs: path: destination; text: complete UTF-8 contents.
    Returns: None; an interrupted temporary write is overwritten on the next save.
    Details: Checkpoint data and its success flags share one replacement.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Each destination has one writer; reuse its short temporary name after a crash.
    temporary = path.with_name('.tmp-' + hashlib.sha256(path.name.encode()).hexdigest()[:12])
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    if before_replace:
        before_replace()
    os.replace(temporary, path)


def save_json(path, value):
    """Save a complete JSON record with atomic replacement.

    Inputs: path: destination; value: JSON-compatible data.
    Returns: None.
    Details: The PDF keeps one checkpoint containing both its tables and progress flags.
    """
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def digest(value):
    """Identify source text and settings before reusing a PDF checkpoint.

    Inputs: value: JSON-compatible data.
    Returns: SHA-256 hexadecimal string.
    Details: Prevents changed source text, prompts or settings from reusing saved results.
    """
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@contextlib.contextmanager
def file_lock(path):
    """Allow only one process to own a run or edit its shared spending ledger.

    Inputs: path: lock-file location.
    Returns: Context manager holding an OS lock, released even after process death.
    Details: Worker threads use a separate RLock inside the run.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def fresh_cycle(*, child=False):
    """Reset the current input's stages, using three flags for a within-reader child.

    Inputs: child: whether the parent owns the final master update. Returns: fresh flags
    and current output fields. The next cycle starts only after the parent master saves.
    Thread identity and cumulative usage live outside this dictionary and are retained.
    """
    return dict.fromkeys(FLAGS[:-1] if child else FLAGS, 0) | {
        'response_id': None, 'reserved_tokens': 0, 'output': None, 'table': None}


def new_agent(agent_id, parent_id='000'):
    """Create one worker identity; API conversations hold its chat history."""
    return {'agent_id': agent_id, 'parent_id': parent_id, 'thread_id': None, 'used_tokens': 0,
            **fresh_cycle(child=parent_id != '000')}


class Checkpoints:
    """One current checkpoint; outputs, tables and their flags are saved together."""

    def __init__(self, root, provider, hook=None):
        """Open an isolated run. One source directory belongs to one PDF and its saved settings."""
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / 'checkpoint.json'
        self.state = read_json(self.path, {})
        if self.state and 'readers' not in self.state:
            raise ValueError('This folder contains an older checkpoint format; use a fresh preparation run.')
        self.provider = provider
        self.hook = hook or (lambda point, unit: None)
        self.lock = threading.RLock()

    def get(self, path=()):
        """Read a consistent copy while other reader workers may save their progress."""
        with self.lock:
            item = self.state
            for key in path:
                item = item[key]
            return deepcopy(item)

    def commit(self, path, changes, unit='', stage='state'):
        """Save changed data and flags in one file replacement, then expose them in memory.

        Inputs: path selects the reader/child/root; changes contains the new values.
        A failed write leaves the preceding checkpoint intact. All four workers share
        this short lock, but their model calls run concurrently outside it.
        """
        with self.lock:
            updated = deepcopy(self.state)
            target = updated
            for key in path:
                target = target[key]
            target.update(deepcopy(changes))
            self.hook('before_' + stage + '_save', unit)
            atomic_text(self.path, json.dumps(updated, ensure_ascii=False, indent=2),
                        lambda: self.hook('during_' + stage + '_save', unit))
            self.state = updated
            self.hook('after_' + stage + '_save', unit)

    def invoke(self, path, unit, role, prompt, payload, contract, expected_names=None):
        """Resume a PDF reader/merger call from its current flags and response ID.

        Inputs: path selects the worker; prompt/payload/contract are unchanged agent inputs.
        Returns: Validated output, retrieved from the API or reused from this checkpoint.
        Known requests are retrieved. Unknown submissions retry in the same conversation.
        The reserved tokens count against the existing 250,000-token agent budget until
        a known reply supplies its actual usage; no chat history is copied to disk.
        """
        self.provider.check_stop()
        agent = self.get(path)
        if agent['output_received']:
            return agent['output']
        name = {'reader': 'pdf_codebook_agent', 'merge': 'pdf_codebook_merge_agent',
                'reconcile': 'pdf_codebook_reconcile_agent'}[role] + ' — ' + agent['agent_id']
        if not agent['thread_id']:
            self.commit(path, {'thread_id': self.provider.new_thread()})
        agent = self.get(path)
        if not agent['sent']:
            request = {'model': self.provider.model,
                       'reasoning': {'effort': 'high' if self.provider.model == 'gpt-6-luna' else 'medium'},
                       'instructions': prompt,
                       'input': [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                       'conversation': agent['thread_id'],
                       'text': {'format': type_to_text_format_param(contract)},
                       'max_output_tokens': self.provider.output_limit,
                       'background': True, 'store': True}
            self.hook('before_submit', unit)
            tokens = self.provider.count_input(request, name)
            reserve = tokens + self.provider.output_limit
            if agent['used_tokens'] + reserve + 512 > SESSION_BUDGET:
                raise RuntimeError(f'{name}: the next call would exceed the {SESSION_BUDGET:,}-token session budget. Saved work is retained.')
            # Save the allowance before submission. An unknown request keeps its charge.
            self.commit(path, {'used_tokens': agent['used_tokens'] + reserve,
                               'reserved_tokens': reserve})
            receipt = self.provider.submit(request, name)
            self.hook('after_submit_before_save', unit)
            self.commit(path, {'response_id': receipt['id'], 'sent': 1}, unit, 'sent')
        agent = self.get(path)
        self.hook('before_retrieve', unit)
        try:
            response = self.provider.retrieve(agent['response_id'])
        except self.provider.missing_response_error:
            # A provider-expired response is no longer recoverable; retry the same input.
            self.commit(path, fresh_cycle(child=agent['parent_id'] != '000'))
            raise
        self.hook('after_reply_before_save', unit)
        usage = response.get('usage')
        used = agent['used_tokens']
        if usage:
            used += usage['input_tokens'] + usage['output_tokens'] - agent['reserved_tokens']
        try:
            if response['status'] != 'completed':
                raise ValueError('Model response ended with status ' + response['status'])
            result = contract.model_validate_json(response['output_text']).model_dump()
            frame = normalize_rows(result['rows'])
            if expected_names is not None and set(frame.variable_name) != set(expected_names):
                raise ValueError('Merger must return each supplied variable exactly once.')
        except (ValueError, KeyError, TypeError):
            self.commit(path, fresh_cycle(child=agent['parent_id'] != '000') | {'used_tokens': used})
            raise
        self.commit(path, {'output': result, 'output_received': 1,
                           'used_tokens': used, 'reserved_tokens': 0}, unit, 'output')
        self.provider.progress(name, payload, result, usage)
        self.provider.check_stop()  # Received output is durable; merging can resume on Retry.
        return result

    def table(self, path, unit):
        """Normalize the current reply and save its rows with table_created=1.

        Inputs: path selects the reader/merger; unit names the page or final batch.
        Returns: Current table rows. A retry uses these rows without regenerating output.
        """
        agent = self.get(path)
        if not agent['table_created']:
            rows = normalize_rows(agent['output']['rows']).to_dict('records')
            self.commit(path, {'table': rows, 'table_created': 1}, unit, 'table')
        return self.get(path)['table']
