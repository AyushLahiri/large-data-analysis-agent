"""PDF ingestion with current-page checkpoints, shared by the app and toy notebook."""
from concurrent.futures import ThreadPoolExecutor
import pandas as pd
from v_5.ingest.readers import read_pdf
from v_5.ingest.codebook_ingestion.tables import COLUMNS, join_codebooks, normalize_rows
from .agent import (PDF_CODEBOOK_PROMPT, PDF_CODEBOOK_MERGE_PROMPT, PDF_CODEBOOK_BLOCK_MERGE_PROMPT,
                    PdfCodebookReply, PdfCodebookMergeReply)
from .pdf_calls import PdfCalls
from .pdf_state import Checkpoints, atomic_text, digest, file_lock, fresh_cycle, new_agent, save_json

# Five pages per reader keeps each Luna conversation short; 100 pages need 20 readers.
PDF_BLOCK_SIZE, PDF_MAX_BLOCKS, PDF_MERGE_BATCH_SIZE = 5, 20, 30


def extract_pdf_block(checkpoint, number, pages, filename, block_size, read_prompt, merge_prompt):
    """Continue one reader at its saved page; its child reconciles repeated variables.

    Inputs: checkpoint: current state; number/pages/filename/block_size: source block;
        read_prompt/merge_prompt: unchanged production instructions.
    Returns: The accumulated block table. Completed pages are never replayed.
    The reply, normalized table and master each advance their own saved flag.
    """
    start, stop = (number - 1) * block_size, min(number * block_size, len(pages))
    path = ('readers', f'reader_{number:03d}')
    while True:
        checkpoint.provider.check_stop()
        reader = checkpoint.get(path)
        page_number = reader['current_pages'][0]
        if reader['merged']:
            if page_number == stop:
                return normalize_rows(reader['master_table'])
            page_number += 1
            child = reader['sub_agent'] | fresh_cycle(child=True)
            # Advancing the cursor and resetting both cycles is one save, before sending.
            checkpoint.commit(path, fresh_cycle() | {
                'current_pages': [page_number] + ([page_number + 1] if page_number < len(pages) else []),
                'sub_agent': child}, f'read_b{number:02d}_p{page_number:03d}', 'advance')
            reader = checkpoint.get(path)
        unit = f'read_b{number:02d}_p{page_number:03d}'
        index = page_number - 1
        payload = {'filename': filename, 'page_range': [start + 1, stop],
                   'current_page': pages[index],
                   'previous_page_lookbehind': pages[start - 1] if index == start and start else None,
                   'next_page_lookahead': pages[index + 1] if index + 1 < len(pages) else None,
                   'unfinished_entry': reader['unfinished_entry']}
        checkpoint.invoke(path, unit, 'reader', read_prompt, payload, PdfCodebookReply)
        incoming = checkpoint.table(path, unit)
        reader = checkpoint.get(path)
        master = {row['variable_name']: row for row in reader['master_table']}
        existing = [master[row['variable_name']] for row in incoming if row['variable_name'] in master]
        child = reader['sub_agent']
        if existing:
            matching = {row['variable_name'] for row in existing}
            child_path = (*path, 'sub_agent')
            merge_unit = f'merge_b{number:02d}_p{page_number:03d}'
            checkpoint.invoke(child_path, merge_unit, 'merge', merge_prompt,
                {'current_page_rows': [row for row in incoming if row['variable_name'] in matching],
                 'existing_rows': existing}, PdfCodebookMergeReply, matching)
            replacements = checkpoint.table(child_path, merge_unit)
            replaced = {row['variable_name']: row for row in replacements}
            incoming = [replaced.get(row['variable_name'], row) for row in incoming]
            child = checkpoint.get(child_path)
        master.update({row['variable_name']: row for row in incoming})
        output = reader['output']
        issues = reader['issues'] + [f'Page {page_number}: {x}' for x in output['issues']]
        if pages[index].get('extracted_text_empty') and pages[index].get('image_count'):
            issues.append(f'Page {page_number}: images exist but no text was extracted; needs visual review or OCR.')
        notes = reader['notes'] + '\n## Page ' + str(page_number) + '\n\n' + ''.join(
            entry + '\n\n' for entry in output['general_information'])
        # The parent owns completion: its master, notes and merged flag save together.
        checkpoint.commit(path, {'master_table': list(master.values()), 'notes': notes,
            'unfinished_entry': output['unfinished_entry'],
            'issues': issues,
            'merged': 1, 'sub_agent': child}, unit, 'merge')


def reconcile_pdf_blocks(checkpoint, filename, batch_size, prompt):
    """Continue final reconciliation at its saved row range using finished block masters.

    Inputs: checkpoint: current state; filename: source label; batch_size: normally 30;
        prompt: production cross-block reconciliation instructions.
    Returns: Final normalized table. Each completed batch replaces its matching rows.
    Only the current batch identity, reply and table are retained. Each batch owns its
    master update, so it has a merged flag; within-reader children do not.
    """
    readers = checkpoint.get(('readers',))
    tables = [(f'table{number}', filename, normalize_rows(readers[f'reader_{number:03d}']['master_table']))
              for number in range(1, len(readers) + 1)]
    by_name = {}
    for source, _, table in tables:
        for row in table.to_dict('records'):
            by_name.setdefault(row['variable_name'], []).append({'source': source, **row})
    roster = pd.DataFrame({'variable_name': pd.Series(list(by_name), dtype=str)})
    joined, _ = join_codebooks(roster, tables)
    atomic_text(checkpoint.root / 'block_tables_joined.csv', joined.to_csv(index=False, lineterminator='\n'))
    fields = ('variable_label', 'variable_description', 'other_information', 'values')
    initial_master, targets = {}, []
    for name, rows in by_name.items():
        useful = [row for row in rows if any(row[field].strip().lower() not in ('', 'not in source') for field in fields)]
        first = (useful or rows)[0]
        initial_master[name] = {field: first[field] for field in COLUMNS}
        locations = dict.fromkeys(row['source_location'] for row in rows if row['source_location'] != 'not in source')
        initial_master[name]['source_location'] = '\n'.join(locations) or 'not in source'
        if len({tuple(row[field] for field in fields) for row in useful}) > 1:
            targets.append({'variable_name': name, 'source_rows': rows})
    if checkpoint.get()['reconciler'] is None:
        reconciler = {'master_table': list(initial_master.values())}
        if targets:
            reconciler.update(new_agent('reconciler_001'),
                              current_rows=[1, min(batch_size, len(targets))])
        # A single block or identical definitions need no final agent or batch flags.
        checkpoint.commit((), {'reconciler': reconciler})
    path = ('reconciler',)
    while targets:
        checkpoint.provider.check_stop()
        agent = checkpoint.get(path)
        first_row, last_row = agent['current_rows']
        if agent['merged']:
            if last_row == len(targets):
                break
            first_row = last_row + 1
            last_row = min(last_row + batch_size, len(targets))
            number = (first_row - 1) // batch_size + 1
            checkpoint.commit(path, new_agent(f'reconciler_{number:03d}') | {
                'current_rows': [first_row, last_row]}, f'reconcile_{number:03d}', 'advance')
        batch = targets[first_row - 1:last_row]
        unit = f'reconcile_{(first_row - 1) // batch_size + 1:03d}'
        matching = {row['variable_name'] for row in batch}
        checkpoint.invoke(path, unit, 'reconcile', prompt,
                          {'target_rows': batch}, PdfCodebookMergeReply, matching)
        rows = checkpoint.table(path, unit)
        master = {row['variable_name']: row for row in checkpoint.get(path)['master_table']}
        for row in rows:
            row['source_location'] = master[row['variable_name']]['source_location']
            master[row['variable_name']] = row
        checkpoint.commit(path, {'master_table': list(master.values()), 'merged': 1}, unit, 'merge')
    return normalize_rows(checkpoint.get(path)['master_table'])


def pdf_codebook(session, file, directory, *, provider=None, hook=None):
    """Ingest or resume one PDF using the app's existing page and merge sequence.

    When: Phase 4, reached by ingest_codebooks or Prepare data after a failed attempt.
    Inputs: session/file/directory: app services, upload and stable source workspace;
        provider/hook: optional offline or capped test transport and failure injection.
    Returns: Final normalized table; exports master rows, notes, joined evidence and checks.
    Flow: Each call targets one page plus next-page context. Its merger handles all
        repeated variables in one call. After every reader finishes, final reconciliation
        runs in 30-variable batches. One checkpoint saves current data and flags together;
        API conversations hold history. Earlier pages are not replayed on retry.
    """
    directory.mkdir(parents=True, exist_ok=True)
    with file_lock(directory / 'run.lock'):
        document = read_pdf(file['path'], first=100, random_count=0)
        if document['page_count'] > 100:
            raise ValueError(f"{file['filename']}: PDFs may contain at most 100 pages.")
        pages, filename = document['pages'], file['filename']
        calls = provider or PdfCalls(session)
        checkpoint = Checkpoints(directory, calls, hook)
        block_size = max(PDF_BLOCK_SIZE, (len(pages) + PDF_MAX_BLOCKS - 1) // PDF_MAX_BLOCKS)
        prompts = (PDF_CODEBOOK_PROMPT, PDF_CODEBOOK_MERGE_PROMPT, PDF_CODEBOOK_BLOCK_MERGE_PROMPT)
        fingerprint = digest([document, filename, block_size, PDF_MERGE_BATCH_SIZE,
                              prompts, calls.model, calls.output_limit])
        state = checkpoint.get()
        if not state:
            readers = {}
            for number, start in enumerate(range(0, len(pages), block_size), 1):
                agent_id = f'reader_{number:03d}'
                readers[agent_id] = new_agent(agent_id) | {
                    'current_pages': [pages[start]['page']] + ([pages[start + 1]['page']] if start + 1 < len(pages) else []),
                    'master_table': [], 'notes': '', 'unfinished_entry': '', 'issues': [],
                    'sub_agent': new_agent(f'merger_{number:03d}', agent_id)}
            checkpoint.commit((), {'input_hash': fingerprint, 'phase': 'reading',
                                  'readers': readers, 'reconciler': None})
        elif state['input_hash'] != fingerprint:
            raise ValueError('PDF inputs or settings changed; start a fresh preparation run.')
        if checkpoint.get()['phase'] == 'reading':
            numbers = range(1, len(checkpoint.get()['readers']) + 1)
            with ThreadPoolExecutor(max_workers=PDF_MAX_BLOCKS) as workers:
                list(workers.map(lambda number: extract_pdf_block(checkpoint, number, pages,
                    filename, block_size, prompts[0], prompts[1]), numbers))
            checkpoint.commit((), {'phase': 'reconciling'}, 'workflow', 'phase')
        if checkpoint.get()['phase'] == 'reconciling':
            reconcile_pdf_blocks(checkpoint, filename, PDF_MERGE_BATCH_SIZE, prompts[2])
            checkpoint.commit((), {'phase': 'exporting'}, 'workflow', 'phase')
        state = checkpoint.get()
        frame = normalize_rows(state['reconciler']['master_table'])
        issues, notes = [], []
        for reader in state['readers'].values():
            issues.extend(reader['issues'])
            notes.append(reader['notes'])
        if state['readers']:
            last = list(state['readers'].values())[-1]
            if last['unfinished_entry']:
                issues.append('Unfinished final entry — ' + last['unfinished_entry'])
                notes.append('\n## Page ' + str(pages[-1]['page']) + '\n\nUnfinished variable entry — ' + last['unfinished_entry'] + '\n\n')
        # These existing deliverables can be recreated from the checkpoint without new calls.
        save_json(directory / 'master_rows.json', frame.to_dict('records'))
        atomic_text(directory / 'normalized.csv', frame.to_csv(index=False, lineterminator='\n'))
        checkpoint.hook('after_csv_export', 'workflow')
        atomic_text(directory / 'general_information.md', ''.join(notes))
        save_json(directory / 'verification.json', {'pages': document['page_count'],
            'blocks': len(state['readers']), 'variables': len(frame), 'issues': issues})
        if state['phase'] != 'complete':
            checkpoint.commit((), {'phase': 'complete'}, 'workflow', 'complete')
        return frame
