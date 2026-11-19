import json
from copy import deepcopy
import pandas as pd
from pathlib import Path
from uuid import uuid4
from v_5.ingest.codebook_ingestion.agent import (
    FINAL_CODEBOOK_BATCH, FINAL_CODEBOOK_PROMPT, FINAL_CODEBOOK_REVIEW, FINAL_CODEBOOK_UNMATCHED,
    FinalCodebookReply,
)
from v_5.ingest.codebook_ingestion.sessions import agent_turn
from v_5.ingest.codebook_ingestion.checkpoint import ReconciliationCheckpoint
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json, save_json
from v_5.ingest.codebook_ingestion.sub_agents.overview import planning_overview
from v_5.ingest.codebook_ingestion.tables import COLUMNS, MASTER_FIELDS
from v_5.ingest.manifest import manifest_columns, work_path
from v_5.main_functions.session import write_json


FINAL_FIELDS = ['variable_name', '% missing', 'variable_type', 'variable_label', 'variable_description',
                'other_information', 'battery_root', 'battery_members', 'battery_summary',
                'categories', 'theme', 'subtheme', 'original_variable_name', 'table_names']


REVIEW_FIELDS = {'variable_label': 'variable_label', 'variable_description': 'variable_description',
                 'other_information': 'other_information_override', 'categories': 'categories'}


THEME_LIMIT, SUBTHEME_LIMIT = 10, 15


BATCH_SIZE, MAX_BATTERY_ROWS = 50, 50


def read_csv(path):
    """Read a codebook CSV without converting blanks into missing values.

    Inputs: path: saved CSV.
    Returns: DataFrame of strings.
    Details: Used during phase 4 reconciliation so empty evidence stays distinct from the
        literal not in source marker.
    """
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def known(value):
    """Turn an absent source value into an empty string.

    Inputs: value: source text or an empty value.
    Returns: Original meaningful value, otherwise an empty string.
    Details: source_entries uses this to keep placeholder text out of reconciliation
        evidence.
    """
    return value if value and value.strip().lower() != 'not in source' else ''


def source_entries(mapping):
    """Collect each variable's definitions from all source tables.

    Inputs: mapping: source ID to prefixed CSV metadata.
    Returns: Dict mapping variable names to lists of source_id and definition fields.
    Details: Used by finalization and planning_overview; strips source suffixes and
        normalizes absent values through known.
    """
    entries = {}
    for key, source in mapping.items():
        table = read_csv(source['table'])
        for row in table.to_dict('records'):
            entry = {'source_id': key}
            for field in COLUMNS[1:-1]:
                entry[field] = known(row[f'{field}_{key}'])
            entries.setdefault(row['variable_name'], []).append(entry)
    return entries


def add_theme(taxonomy, row):
    """Validate and reuse a variable's theme and subtheme.

    Inputs: taxonomy: mutable theme-to-subthemes dict; row: proposed variable definition.
    Returns: None; updates taxonomy and row labels, or raises on missing/excess categories.
    Details: Phase 4 batch validation reuses labels case-insensitively and enforces the
        existing 10-theme/15-subtheme limits.
    """
    theme, subtheme = row['theme'].strip(), row['subtheme'].strip()
    if not theme or not subtheme:
        raise ValueError('Every variable needs a theme and subtheme.')
    theme = next((label for label in taxonomy if label.casefold() == theme.casefold()), theme)
    if theme not in taxonomy:
        if len(taxonomy) >= THEME_LIMIT:
            raise ValueError('Ten themes already exist. Choose an existing theme.')
        taxonomy[theme] = []
    choices = taxonomy[theme]
    subtheme = next((label for label in choices if label.casefold() == subtheme.casefold()), subtheme)
    if subtheme not in choices:
        if len(choices) >= SUBTHEME_LIMIT:
            raise ValueError(f'{theme} already has 15 subthemes. Choose an existing subtheme.')
        choices.append(subtheme)
    row.update(theme=theme, subtheme=subtheme)


def align_batteries(rows):
    """Make related battery variables agree on membership and grouping.

    Inputs: rows: mutable variable-name-to-definition dict.
    Returns: None; updates battery fields and shared theme/subtheme in place.
    Details: Runs after finalization batches. Keeps groups with at least two compatible
        members and clears incomplete singleton assignments.
    """
    groups = {}
    assigned = set()
    for name, row in rows.items():
        root = row['battery_root']
        if root:
            group = groups.setdefault(root, {'names': set(), 'row': row})
            group['names'].add(name)
            group['names'].update(peer for peer in row['battery_members'] if peer in rows and
                                  rows[peer]['battery_root'] in ('', root))
    for root, group in groups.items():
        anchor = group['row']
        members = [name for name in rows if name in group['names'] and rows[name]['battery_root'] in ('', root)]
        if len(members) < 2:
            continue
        title = ' '.join(anchor['battery_title'].split()[:2]) or root
        for name in members:
            rows[name].update(battery_root=root, battery_title=title,
                              battery_members=[peer for peer in members if peer != name],
                              theme=anchor['theme'], subtheme=anchor['subtheme'])
            assigned.add(name)
    for name, row in rows.items():
        if name not in assigned:
            row.update(battery_root='', battery_members=[], battery_title='')


def final_table(master, rows, entries):
    """Combine prepared-data facts with reconciled definitions.

    Inputs: master: data roster; rows: accepted variable definitions; entries: source
        evidence by variable.
    Returns: DataFrame in FINAL_FIELDS order.
    Details: Used for user review and final CSVs. Preserves source notes unless overridden
        and merges definitions one-to-one onto prepared variables.
    """
    definitions = []
    for name, row in rows.items():
        definition = {key: row[key] for key in ('variable_label', 'variable_description', 'battery_root',
                                                'battery_members', 'categories', 'theme', 'subtheme')}
        notes = dict.fromkeys(entry['other_information'] for entry in entries.get(name, []) if entry['other_information'])
        definition['other_information'] = row['other_information_override'] or '\n\n'.join(notes) or 'not in source'
        peers = row['battery_members']
        definition['battery_summary'] = f"{row['battery_title']} battery with {len(peers) + 1} items." if peers else ''
        definitions.append({'variable_name': name, **definition})
    frame = pd.DataFrame(definitions, columns=[col for col in FINAL_FIELDS if col not in MASTER_FIELDS[1:]])
    return master.merge(frame, on='variable_name', how='left', validate='one_to_one').reindex(columns=FINAL_FIELDS)


def save_final_table(path, master, rows, entries):
    """Write a final codebook with JSON-encoded battery members.

    Inputs: path: destination; master/rows/entries: inputs for final_table.
    Returns: None; writes the final CSV.
    Details: Phase 4 uses the same serializer for matched and unmatched variables so later
        SQL publication can read battery membership consistently.
    """
    frame = final_table(master, rows, entries)
    frame['battery_members'] = frame['battery_members'].map(json.dumps)
    frame.to_csv(path, index=False)


def finalize_codebook(session, context, prepared, workspace, book):
    """Reconcile source definitions into the final dataset codebook.

    When: Phase 4 main final_codebook_creator, after source extraction; uses
        codebook_overview_agent for planning.
    Inputs: session: interactions; context: folder evidence; prepared: loader report;
        workspace: work root; book: mutable source/output registry.
    Returns: None; writes core, unmatched and soft-matched CSVs, taxonomy and planning/review files,
        and adds their paths to book.
    Flow: Each fresh batch has saved input/output flags and its API thread. Accepted rows,
        taxonomy and the next batch cursor are committed together in one overwritten file.
        Lookahead stays in the same thread; saved user answers survive retries. Final CSVs
        are cheap exports and are rewritten if saving them fails.
    """
    directory = Path(book['combined']).parent
    checkpoint = ReconciliationCheckpoint(directory, session)
    progress = checkpoint.state
    combined = read_csv(book['combined'])
    if 'variable_type' not in combined:
        # Older saved joins can recover the prepared types from the loader manifest.
        manifest = json.loads(work_path(workspace, prepared['manifest_path']).read_text(encoding='utf-8'))
        types = {}
        for table in manifest['tables']:
            for name, column in manifest_columns(table).items():
                original = column.get('original_name') or name
                types.setdefault(original.lower(), column.get('type') or column.get('sql_type', ''))
        combined['variable_type'] = combined['variable_name'].map(types).fillna('')
    master = combined[MASTER_FIELDS]
    types = master.set_index('variable_name')['variable_type'].to_dict()
    names = master['variable_name'].tolist()
    unmatched_names = read_csv(book['unmatched'])['variable_name'].drop_duplicates().tolist()
    mapping = book['source_tables']
    entries = source_entries(mapping)
    hierarchy = {}
    for key, source in mapping.items():
        origin = source.get('embedded_for')
        hierarchy[key] = {'filename': source['filename'], 'priority': 1 if origin else 2 if source.get('primary') else 3,
                          'embedded_data_file': context['files'].get(origin, {}).get('filename')}

    session.message('final_codebook_creator', 'I’ll reconcile the source definitions and group the variables by theme.')
    plan = planning_overview(session, names, entries, hierarchy, types,
                             destination=directory / 'planning_overview.json')
    summary = (context.get('ingestion_notes', {}).get('summary') or context.get('project_notes')
               or context.get('folder_description', {}).get('description') or context.get('project', {}).get('description', ''))
    metadata = {
        'dataset_summary': summary,
        'column_key': {'sources_by_id': hierarchy, 'source_columns': COLUMNS,
                       'value_field': 'values holds documented categories, types and special missing codes'},
        'source_hierarchy': '1 = embedded for the variable, 2 = selected external primary, 3 = supplementary',
        'planning_hint': plan['overview'],
        'planning_warning': f"This is a preliminary planning note from Luna, a smaller model. It saw {plan['variables_seen']} of {len(names)} data variables and may be incomplete or wrong. Use the source rows and your own judgment. The hint is not authoritative.",
    }
    taxonomy = progress['taxonomy']
    saved_rows = {**progress['matched'], **progress['unmatched']}
    recent = [saved_rows[name] for name in progress['recent']]
    batch_number = 0

    def evidence(name):
        """Package the evidence for one codebook variable.

        Inputs: name: variable key; captures prepared types and source entries.
        Returns: Dict with variable_name, prepared_type and source_rows.
        Details: Used by batch and review payloads for final_codebook_creator.
        """
        return {'variable_name': name, 'prepared_type': types.get(name, ''), 'source_rows': entries.get(name, [])}

    def new_batch():
        """Start a fresh final-codebook conversation budget.

        Inputs: Uses and increments the enclosing batch_number.
        Returns: (history, counter): empty message list and used/thread budget dict.
        Details: Called for each matched, unmatched or review batch; cross-batch context is
            supplied explicitly by ask.
        """
        nonlocal batch_number
        batch_number += 1
        return [], {'used': 0, 'thread': batch_number}

    def ask(instructions, payload, history, counter):
        """Add common dataset context to a final-codebook request.

        Inputs: instructions: task-specific prompt; payload: target/review rows;
            history/counter: current batch conversation and budget.
        Returns: Validated FinalCodebookReply dict.
        Details: Phase 4 final_codebook_creator receives source hierarchy, taxonomy and recent
            rows through checkpointed agent_turn; automatic question forms are disabled.
        """
        return agent_turn(session, 'final_codebook_creator', FINAL_CODEBOOK_PROMPT + '\n' + instructions,
                          history, {**metadata, 'taxonomy': taxonomy, 'recent_rows': recent, **payload},
                          FinalCodebookReply, counter, allow_user_questions=False, checkpoint=checkpoint)

    def process(ordered_names, unmatched=False):
        """Reconcile one ordered list of codebook variables in batches.

        When: Phase 4 main final_codebook_creator, for matched data variables and then unmatched
            source variables.
        Inputs: ordered_names: target order; unmatched: use unmatched rules; captures evidence,
            taxonomy and output directory.
        Returns: Variable-name-to-definition dict; extends the shared taxonomy and saves
            accepted rows with the next cursor. Earlier batches do not run again.
        Flow: Allows bounded battery lookahead, validates exact target membership and conflict
            evidence, retries rejected batches, then aligns battery groups through
            align_batteries.
        """
        phase = 'unmatched' if unmatched else 'matched'
        rows = progress[phase]
        if progress['phase'] != phase:
            return rows
        for start in range(progress['start'], len(ordered_names), BATCH_SIZE):
            end = start + BATCH_SIZE
            targets, following = ordered_names[start:end], ordered_names[end:end + MAX_BATTERY_ROWS]
            target_rows = [evidence(name) for name in targets]
            history, counter = new_batch()
            payload = deepcopy((progress['agent'] or {}).get('input') or {
                'target_rows': target_rows, 'lookahead_rows': [],
                'can_request_more_battery_rows': bool(following), 'remaining_variables': len(ordered_names) - start,
                'unmatched_variables_after_matched': 0 if unmatched else len(unmatched_names), 'feedback': ''})
            rejections = (progress['agent'] or {}).get('rejections', 0)
            while True:
                added = len(payload['lookahead_rows'])
                can_extend = added < len(following)
                reply = ask(FINAL_CODEBOOK_BATCH + (FINAL_CODEBOOK_UNMATCHED if unmatched else ''), payload, history, counter)
                if reply['require_further_battery_rows'] and can_extend and not reply['rows']:
                    added += 10
                    payload.update(lookahead_rows=[evidence(name) for name in following[:added]],
                                   can_request_more_battery_rows=added < len(following), feedback='')
                    checkpoint.next_input(payload)
                    continue
                try:
                    received = reply['rows']
                    if reply['require_further_battery_rows'] or len(received) != len(targets) or {r['variable_name'] for r in received} != set(targets):
                        raise ValueError('Return exactly the target rows, with no lookahead rows. Respect the lookahead allowance.')
                    # Validate against a copy so a rejected batch cannot consume theme or subtheme slots.
                    candidate_themes = {theme: labels[:] for theme, labels in taxonomy.items()}
                    for row in received:
                        if not row['variable_label'].strip() or not row['variable_description'].strip():
                            raise ValueError('Every row needs a usable label and description.')
                        if unmatched and row['conflicts']:
                            raise ValueError('Resolve unmatched variables without human review flags.')
                        source_ids = {entry['source_id'] for entry in entries.get(row['variable_name'], [])}
                        for conflict in row['conflicts']:
                            claims = conflict['evidence']
                            if len(claims) < 2 or any(c['source_id'] not in source_ids or not c['excerpt'].strip() for c in claims):
                                raise ValueError('Each review flag needs two competing excerpts from the supplied sources.')
                        row['categories'] = row['categories'].strip() or 'not in source'
                        add_theme(candidate_themes, row)
                    break
                except ValueError as error:
                    rejections += 1
                    payload['feedback'] = str(error)
                    checkpoint.next_input(payload, rejected=True)
                    if rejections == 3:
                        raise ValueError(f'Final codebook could not accept this {phase} batch: {error}') from error
            taxonomy.update(candidate_themes)
            by_name = {row['variable_name']: row for row in received}
            rows.update({name: by_name[name] for name in targets})
            recent[:] = (recent + [by_name[name] for name in targets])[-10:]
            # Saving accepted rows and advancing their cursor is one operation.
            progress.update(start=end, recent=[row['variable_name'] for row in recent], agent=None)
            checkpoint.save('batch')
            session.message('final_codebook_creator', f'Processed {len(rows)} of {len(ordered_names)} {phase} variables.', 'thinking')
        align_batteries(rows)
        progress.update(phase='soft_match' if unmatched else 'review', start=0, agent=None)
        if not unmatched:
            progress['review_targets'] = [name for name in rows if rows[name]['conflicts']]
        checkpoint.save('phase')
        return rows

    matched = process(names)
    flagged = progress.get('review_targets', [])
    if progress['phase'] == 'review' and flagged:
        draft = final_table(master, matched, entries).fillna('')
        display = []
        for record in draft[draft.variable_name.isin(flagged)].to_dict('records'):
            conflicts = matched[record['variable_name']]['conflicts']
            display.append({**record, 'conflicts': [{**conflict, 'evidence': [
                {**claim, 'source': hierarchy.get(claim['source_id'], {}).get('filename', claim['source_id'])}
                for claim in conflict['evidence']]} for conflict in conflicts]})
        instructions = read_json(directory / 'review_answers.json')
        if instructions is None:
            form = {'id': uuid4().hex, 'kind': 'codebook_review', 'rows': display,
                    'questions': [{'id': name, 'question': name} for name in flagged]}
            write_json(directory / 'review_input.json', display)
            session.state['question_form'] = form
            session.record('question_form', 'final_codebook_creator', form)
            instructions = session.answer('Please review the conflicting definitions in the table.')
            save_json(directory / 'review_answers.json', instructions)
            checkpoint.hook('after_review_answers', progress)
        for start in range(progress['start'], len(flagged), BATCH_SIZE):
            targets = flagged[start:start + BATCH_SIZE]
            history, counter = new_batch()
            payload = deepcopy((progress['agent'] or {}).get('input') or {'target_rows': [{**evidence(name), 'provisional_row': matched[name],
                                        'user_instruction': instructions[name]} for name in targets],
                       'remaining_variables': len(flagged) - start, 'can_request_more_battery_rows': False})
            for attempt in range((progress['agent'] or {}).get('rejections', 0), 3):
                reply = ask(FINAL_CODEBOOK_REVIEW, payload, history, counter)
                revised = {row['variable_name']: row for row in reply['rows']}
                if (not reply['require_further_battery_rows'] and set(revised) == set(targets)
                    and len(reply['rows']) == len(targets) and not any(row['conflicts'] for row in revised.values())):
                    break
                payload['feedback'] = 'Return each requested reviewed row once, with no further flags.'
                checkpoint.next_input(payload, rejected=True)
            else:
                raise ValueError('Final codebook could not reconcile the reviewed rows. The review instructions are saved.')
            for name in targets:
                # Apply reviewed fields only; retain the accepted metadata and grouping from the original row.
                for conflict in matched[name]['conflicts']:
                    field = REVIEW_FIELDS[conflict['field']]
                    matched[name][field] = revised[name][field] or ('not in source' if field == 'categories' else matched[name][field])
                matched[name]['conflicts'] = []
            recent[:] = (recent + [matched[name] for name in targets])[-10:]
            progress.update(start=start + BATCH_SIZE, recent=[row['variable_name'] for row in recent], agent=None)
            checkpoint.save('review_batch')

    if progress['phase'] == 'review':
        progress.update(phase='unmatched', start=0, agent=None)
        progress.pop('review_targets', None)
        checkpoint.save('phase')

    unmatched = process(unmatched_names, unmatched=True)
    from .sub_agents.master_to_codebook import reconcile_master_names
    core, remaining, soft_matched = reconcile_master_names(
        session, master, matched, unmatched, entries, taxonomy, metadata, checkpoint=checkpoint)
    for filename, frame in [('final_codebook.csv', core), ('final_unmatched_codebook.csv', remaining),
                            ('soft_matched_codebook.csv', soft_matched)]:
        for field in ('battery_members', 'soft_match'):
            if field in frame:
                frame[field] = frame[field].map(json.dumps)
        target = directory / filename
        temporary = target.with_suffix('.csv.tmp')
        frame.to_csv(temporary, index=False)
        temporary.replace(target)
    write_json(directory / 'taxonomy.json', taxonomy)
    book.update(final=str(directory / 'final_codebook.csv'), final_unmatched=str(directory / 'final_unmatched_codebook.csv'),
                soft_matched=str(directory / 'soft_matched_codebook.csv'),
                taxonomy=str(directory / 'taxonomy.json'), overview=str(directory / 'planning_overview.json'))
    session.record('final_codebook_complete', 'final_codebook_creator', book)
    session.message('final_codebook_creator', f'Final codebook saved for {len(matched)} data variables. {len(remaining)} unmatched source variables and {len(soft_matched)} soft-matched source variables are documented separately.')
    progress.update(phase='complete', start=0, agent=None)
    checkpoint.save('complete')
