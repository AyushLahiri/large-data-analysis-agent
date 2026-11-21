"""Offline/capped test adapters for the real PDF checkpoint pipeline; no duplicate workflow."""
import argparse
import json
from pathlib import Path
import threading
import os
from uuid import uuid4
from v_5.ingest.codebook_ingestion.tables import COLUMNS, normalize_rows
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import (
    Checkpoints as PdfCheckpoints, FLAGS, atomic_text, digest, file_lock, fresh_cycle,
    new_agent, read_json, save_json)
from v_5.ingest.codebook_ingestion.sub_agents.pdf_calls import PdfCalls

MODEL, EFFORT, OUTPUT_LIMIT = 'gpt-6-luna', 'high', 4096
LAB_ROOT = Path(__file__).resolve().parent / 'app' / 'runtime' / 'prototype_checkpoint_lab'
NOTEBOOK = Path(__file__).resolve().parent / 'ingestion.ipynb'


def estimated_cost(usage):
    """Estimate Luna standard pricing from returned usage, including cache writes.

    Inputs: usage: response token counts and optional cache-read/write breakdown.
    Returns: Estimated USD at the published rates checked on 2026-11-17.
    Details: The separate spending guard uses higher rates and retains unknown reservations.
    """
    details = usage.get('input_tokens_details') or {}
    cached = details.get('cached_tokens', 0)
    writes = details.get('cache_write_tokens', 0)
    long_writes = (details.get('cache_write_tokens_details') or {}).get('ttl_12h_tokens', 0)
    uncached = max(0, usage['input_tokens'] - cached - writes)
    return (uncached * .1 + cached * .01 + (writes - long_writes) * .125 +
            long_writes * .25 + usage['output_tokens'] * .5) / 1_000_000


class InjectedFailure(RuntimeError):
    """A deliberate failure after a named persistence boundary."""


class Faults:
    def __init__(self, root, plan=()):
        """Remember which deliberate failures have already fired across restarts.

        Inputs: root: run directory; plan: point/unit/action records.
        Returns: Initialized injector; consumed faults persist in faults.json.
        Details: 'exit' kills the whole process; 'raise' lets other workers finish.
        """
        self.path = Path(root) / 'faults.json'
        self.plan = list(plan)
        self.lock = threading.RLock()

    def hit(self, point, unit):
        """Interrupt a selected unit once at a named boundary.

        Inputs: point: boundary label; unit: stable page or batch ID.
        Returns: None unless an injection matches; may terminate the process.
        Details: The fired marker is durable before interruption, so retry can proceed.
        """
        with self.lock:
            fired = read_json(self.path, [])
            for index, fault in enumerate(self.plan):
                key = str(index)
                if key not in fired and fault['point'] == point and fault['unit'] == unit:
                    save_json(self.path, [*fired, key])
                    if fault.get('action', 'raise') == 'exit':
                        os._exit(86)
                    if fault.get('action') == 'timeout':
                        raise TimeoutError(f'{point}: {unit}')
                    if fault.get('action') == 'disk':
                        raise OSError(f'Simulated disk write failure at {point}: {unit}')
                    raise InjectedFailure(f'{point}: {unit}')


class Spending:
    def __init__(self, path, limit=5.0):
        """Open the single shared dollar cap for all paid prototype experiments.

        Inputs: path: persistent ledger; limit: maximum authorised USD, at most five.
        Returns: Ledger wrapper; each request reserves cost before submission.
        Details: Reservations include full output and deliberately conservative input costs.
        """
        if not 0 < limit <= 5:
            raise ValueError('The authorised prototype ceiling is $5.')
        self.path, self.limit = Path(path), limit
        self.lock = threading.RLock()

    def reserve(self, key, request, history):
        """Reserve a conservative upper bound before one billable generation.

        Inputs: key: unique attempt; request/history: current input and accepted conversation.
        Returns: None; raises before submission if the $5 ceiling would be exceeded.
        Details: Counts UTF-8 bytes as tokens plus 4,096 overhead per call and includes
            earlier conversation bounds. Uses $1/$4 per million input/output, above
            published Luna standard rates. SDK automatic retries are off.
        """
        with self.lock, file_lock(self.path.with_suffix('.lock')):
            ledger = read_json(self.path, {'limit_usd': self.limit, 'calls': {}})
            # Budget for server-side conversation history without keeping local chats.
            bounds = ledger.setdefault('conversation_bounds', {})
            conversation = request.get('conversation', '')
            input_bound = bounds.get(conversation, 0) + len(json.dumps([request, history], ensure_ascii=False).encode()) + 4096
            if input_bound > 200_000:
                raise ValueError('Toy request exceeds the permitted short-context bound.')
            reserve = input_bound / 1_000_000 + OUTPUT_LIMIT * 4 / 1_000_000
            limit = min(self.limit, ledger['limit_usd'])
            if key in ledger['calls']:
                raise ValueError('An attempt ID cannot be submitted twice.')
            if sum(call['budget_charge_usd'] for call in ledger['calls'].values()) + reserve > limit:
                raise RuntimeError('Paid-call ceiling reached; no request was submitted.')
            ledger['calls'][key] = {'reserved_usd': reserve, 'budget_charge_usd': reserve,
                                    'usage': None, 'estimated_usd': None}
            bounds[conversation] = input_bound + OUTPUT_LIMIT
            save_json(self.path, ledger)

    def settle(self, key, usage):
        """Replace a reservation with conservatively charged measured usage.

        Inputs: key: attempt ID; usage: returned input/output token counts.
        Returns: None; records estimated standard cost and a larger budget charge.
        Details: Unknown submissions retain their full reservation; the estimate accounts
            for cache reads and writes. Published prices checked on 2026-11-17.
        """
        with self.lock, file_lock(self.path.with_suffix('.lock')):
            ledger = read_json(self.path)
            call = ledger['calls'][key]
            call.update(usage=usage,
                        estimated_usd=estimated_cost(usage),
                        budget_charge_usd=(usage['input_tokens'] + usage['output_tokens'] * 4) / 1_000_000)
            if call['budget_charge_usd'] > call['reserved_usd']:
                raise RuntimeError('Usage exceeded the conservative reservation; stop paid testing.')
            save_json(self.path, ledger)


def oracle(payload, role):
    """Calculate the toy truth independently of the checkpointed pipeline.

    Inputs: payload: unchanged production page/merge shape; role: reader/merge/reconcile.
    Returns: Literal extraction or lossless field-by-field union in the real response schema.
    Details: Used only for offline transport and assertions; paid replies are never substituted.
    """
    if role == 'reader':
        page = payload['current_page']
        lines = page['markdown'].splitlines()
        name, label, fact = [part.strip() for part in lines[0].split('|')]
        row = dict(zip(COLUMNS, [name.lower(), label, fact, 'not in source', 'not in source',
                                 f"Page {page['page']}", 'in source']))
        return {'rows': [row], 'general_information': [s[6:] for s in lines if s.startswith('Note: ')],
                'unfinished_entry': next((s[7:] for s in lines if s.startswith('Carry: ')), ''), 'issues': []}
    groups = {}
    sources = (payload['existing_rows'] + payload['current_page_rows'] if role == 'merge'
               else [row for target in payload['target_rows'] for row in target['source_rows']])
    for row in sources:
        groups.setdefault(row['variable_name'], []).append(row)
    rows = []
    for name, records in groups.items():
        out = {'variable_name': name, 'source_presence': 'in source'}
        for field in COLUMNS[1:-1]:
            lines = list(dict.fromkeys(line for row in records for line in row[field].splitlines()
                                      if line and line != 'not in source'))
            out[field] = '\n'.join(lines) or 'not in source'
        rows.append(out)
    return {'rows': rows}


class FakeProvider:
    """Durable test API; only tests inspect its received requests and generated replies."""
    model, output_limit = MODEL, OUTPUT_LIMIT
    missing_response_error = FileNotFoundError

    def __init__(self, root):
        """Keep simulated server records separate from the application's checkpoint."""
        self.root = Path(root) / 'simulated_api'
        self.root.mkdir(parents=True, exist_ok=True)

    def check_stop(self):
        """Offline runs have no external cancellation request."""

    def count_input(self, request, name):
        """Return a small known count for testing the real per-agent token guard."""
        return 100

    def progress(self, name, payload, result, usage):
        """Keep test output focused on failures and final assertions."""

    def rejected(self, name, problem, attempt):
        """Rejected replies are retried silently in the offline pipeline."""

    def new_thread(self):
        """Return an API-shaped identity without saving a local conversation."""
        return 'conv_fake_' + uuid4().hex

    def submit(self, request, name):
        """Calculate a literal toy answer and save it on the simulated server."""
        payload = json.loads(request['input'][0]['content'])
        role = 'reader' if 'current_page' in payload else 'merge' if 'existing_rows' in payload else 'reconcile'
        identifier = 'resp_fake_' + uuid4().hex
        result = {'id': identifier, 'status': 'completed', 'output_text': json.dumps(oracle(payload, role)),
                  'usage': {'input_tokens': 100, 'output_tokens': 100}}
        save_json(self.root / (identifier + '.json'), {'request': request, 'response': result})
        return {'id': identifier, 'status': 'queued'}

    def retrieve(self, response_id):
        """Return one already-generated result; an unknown ID raises visibly."""
        path = self.root / (response_id + '.json')
        if not path.is_file():
            raise FileNotFoundError(response_id)
        return read_json(path)['response']


class ToySession:
    """Use the app's real input counter without app/UI state for optional paid tests."""

    def check_stop(self):
        """The CLI is interrupted by terminating its process."""

    def message(self, *args):
        """Print only temporary API-conversation lock notices."""
        if len(args) > 1 and 'previous request' in args[1]:
            print(args[1], flush=True)

    def count_input(self, request, agent=None):
        """Count real conversation input through the same API used by the app."""
        from v_5.main_functions.session import count_input
        return count_input(request)


class LunaProvider(PdfCalls):
    """Exercise the production transport under the original shared five-dollar cap."""

    def __init__(self, ledger):
        """Use Luna/high with the toy's shorter output allowance."""
        super().__init__(ToySession(), model=MODEL, output_limit=OUTPUT_LIMIT)
        self.ledger = ledger

    def submit(self, request, name):
        """Reserve money before submission; keep receipt accounting outside the checkpoint."""
        key = uuid4().hex
        self.ledger.reserve(key, request, [])
        receipt = super().submit(request, name)
        with self.ledger.lock, file_lock(self.ledger.path.with_suffix('.lock')):
            state = read_json(self.ledger.path)
            state['calls'][key]['response_id'] = receipt['id']
            save_json(self.ledger.path, state)
        return receipt

    def retrieve(self, response_id):
        """Retrieve through the production adapter and settle known test usage."""
        result = super().retrieve(response_id)
        if result['usage']:
            state = read_json(self.ledger.path)
            key = next(k for k,v in state['calls'].items() if v.get('response_id') == response_id)
            self.ledger.settle(key, result['usage'])
        return result


class Checkpoints(PdfCheckpoints):
    """Compatibility for notebook/test callers; production owns the actual implementation."""

    def __init__(self, root, provider=None, plan=()):
        """Locate the source checkpoint beside its exports, with external test fault controls."""
        self.case_root = Path(root).resolve()
        self.faults = Faults(root, plan)
        super().__init__(self.case_root / 'codebook', provider or FakeProvider(root), self.faults.hit)


def run_prototype(checkpoint, batch_size=30):
    """Run production PDF ingestion on strings, changing only toy inputs/prompts/settings."""
    from unittest.mock import patch
    from v_5.ingest.codebook_ingestion.sub_agents import pdf
    from v_5.prototype_data import DOCUMENT, READ_PROMPT, MERGE_PROMPT, RECONCILE_PROMPT
    with patch.object(pdf, 'read_pdf', return_value=DOCUMENT), \
         patch.object(pdf, 'PDF_BLOCK_SIZE', 5), patch.object(pdf, 'PDF_MAX_BLOCKS', 4), \
         patch.object(pdf, 'PDF_MERGE_BATCH_SIZE', batch_size), \
         patch.object(pdf, 'PDF_CODEBOOK_PROMPT', READ_PROMPT), \
         patch.object(pdf, 'PDF_CODEBOOK_MERGE_PROMPT', MERGE_PROMPT), \
         patch.object(pdf, 'PDF_CODEBOOK_BLOCK_MERGE_PROMPT', RECONCILE_PROMPT):
        return pdf.pdf_codebook(ToySession(), {'filename': 'toy_strings', 'path': 'strings-only'},
                                checkpoint.root, provider=checkpoint.provider, hook=checkpoint.hook)


def load_pipeline():
    """Expose the one production-backed runner to the existing stress-test harness."""
    return {'run_prototype': run_prototype}


def run_case(root, *, live=False, plan=(), batch_size=30, ledger=None):
    """Run or resume the app's PDF pipeline through fake or capped real transport."""
    provider = LunaProvider(Spending(ledger or LAB_ROOT / 'spending.json')) if live else FakeProvider(root)
    return run_prototype(Checkpoints(root, provider, plan), batch_size)


def main():
    """Run one notebook case from a child process used by crash tests.

    Inputs: CLI case path, optional fault plan, live switch and reconciliation batch size.
    Returns: None; prints result counts, or exits nonzero on a real/injected failure.
    Details: All paid invocations share the default $5 ledger unless explicitly supplied.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--plan', type=Path)
    parser.add_argument('--batch-size', type=int, default=30)
    parser.add_argument('--ledger', type=Path)
    args = parser.parse_args()
    result = run_case(args.case, live=args.live, plan=read_json(args.plan, []) if args.plan else [],
                      batch_size=args.batch_size, ledger=args.ledger)
    print(f'Complete: {len(result)} variables; outputs: {args.case}')


if __name__ == '__main__':
    main()
