"""Recover supported name variations for master variables with no exact codebook match."""
from copy import deepcopy
import json
import re

from v_5.ingest.codebook_ingestion.sessions import agent_turn
from .agent import MASTER_TO_CODEBOOK_PROMPT, MasterCodebookSearchReply, MasterCodebookUpdateReply


BATCH_SIZE = 50


def compile_candidate(pattern):
    """Compile a whole-name search with literals and one optional separator.

    Input: agent-written pattern. Returns: case-insensitive regex, or raises ValueError.
    The small grammar supports name variations without shell execution or broad scans.
    """
    if not pattern or len(pattern) > 120:
        raise ValueError('Use a nonempty name pattern of at most 120 characters.')
    body = pattern.removeprefix('^').removesuffix('$')
    index, separator = 0, False
    while index < len(body):
        if body[index].isalnum() or body[index] in '_-':
            index += 1
        elif body[index:index + 2] in (r'\.', r'\-', r'\ '):
            index += 2
        elif body.startswith('[_-]?', index) and not separator:
            separator = True
            index += 5
        else:
            raise ValueError('Use a literal name, escaped punctuation, or one [_-]? separator.')
    if not any(character.isalnum() for character in body):
        raise ValueError('A search must contain a variable name.')
    return re.compile(pattern, re.IGNORECASE)


def search_candidates(candidates, targets, source_rows):
    """Run each target's candidates once and return complete, deduplicated source rows.

    Inputs: validated search reply, current target names and finalized unmatched rows.
    Returns: per-target candidates with their pattern and full matching rows (possibly []).
    Invalid patterns return an error and no rows; they do not trigger another search call.
    """
    names = [item['variable_name'] for item in candidates]
    if len(names) != len(targets) or set(names) != set(targets):
        raise ValueError('Search candidates must name each current target exactly once.')
    results = []
    for item in candidates:
        if len(item['patterns']) > 3:
            raise ValueError('At most three search patterns are allowed per variable.')
        queries = []
        for pattern in dict.fromkeys(item['patterns']):
            try:
                query = compile_candidate(pattern)
                queries.append({'pattern': pattern, 'rows': [deepcopy(row) for row in source_rows
                                if query.fullmatch(row['variable_name'])]})
            except (ValueError, re.error) as error:
                queries.append({'pattern': pattern, 'rows': [], 'error': str(error)})
        results.append({'variable_name': item['variable_name'], 'candidates': queries})
    return results


def known_batteries(rows):
    """Describe accepted battery groups so a fresh batch can preserve their decisions."""
    groups = {}
    for name, row in rows.items():
        root = row['battery_root']
        if root:
            group = groups.setdefault(root, {key: row[key] for key in ('battery_title', 'theme', 'subtheme')})
            group.setdefault('members', []).append(name)
    return groups


def reconcile_master_names(session, master, matched, unmatched, entries, taxonomy, metadata, *, checkpoint=None):
    """Search unmatched source rows after normal finalization, then apply justified updates.

    When: Phase 4 master_to_codebook_reconciler, fresh conversation per 50 missing names.
    Inputs: master facts, final definitions, exact source index, taxonomy and dataset context;
        checkpoint: finalizer's shared current-agent and accepted-row state.
    Returns: (core, still_unmatched, soft_matched) final tables. Only zero-exact-match targets
    may change. A batch uses one search call and, if hits exist, one decision call in that
    same conversation. No-hit or declined targets retain all their previous fields.
    Saved search output can rerun the cheap name lookup; a pending decision keeps its
    search results in that input. Accepted changes and the next cursor save together.
    """
    import pandas as pd
    from v_5.ingest.codebook_ingestion.finalize import add_theme, align_batteries, final_table
    from v_5.ingest.codebook_ingestion.tables import MASTER_FIELDS

    eligible = [name for name in master['variable_name'] if not entries.get(name)]
    source_master = pd.DataFrame({'variable_name': list(unmatched)}, dtype=object).reindex(columns=MASTER_FIELDS)
    source_frame = final_table(source_master, unmatched, entries).fillna('')
    source_rows = source_frame.to_dict('records')
    annotations = checkpoint.state['annotations'] if checkpoint else {}
    first = checkpoint.state['start'] if checkpoint else 0
    pending = checkpoint is None or checkpoint.state['phase'] == 'soft_match'

    def completed_batch(next_start):
        """Commit accepted rows/reasons with the next cursor, then discard this agent."""
        if checkpoint:
            checkpoint.state.update(start=next_start, agent=None)
            checkpoint.save('soft_batch')

    for start in range(first, len(eligible) if source_rows and pending else 0, BATCH_SIZE):
        targets = eligible[start:start + BATCH_SIZE]
        target_frame = final_table(master[master.variable_name.isin(targets)],
                                   {name: matched[name] for name in targets}, entries).fillna('')
        history, counter = [], {'used': 0, 'thread': 1 + start // BATCH_SIZE}
        common = {**metadata, 'taxonomy': deepcopy(taxonomy), 'known_batteries': known_batteries(matched),
                  'master_variable_names': master['variable_name'].tolist(),
                  'target_columns': list(target_frame.columns), 'source_columns': list(source_frame.columns)}
        saved_input = ((checkpoint.state['agent'] or {}).get('input', {}) if checkpoint else {})
        if saved_input.get('stage') == 'reconcile':
            searches = saved_input['search_results']
        else:
            reply = agent_turn(session, 'master_to_codebook_reconciler', MASTER_TO_CODEBOOK_PROMPT, history,
                               {**common, 'stage': 'search', 'target_rows': target_frame.to_dict('records')},
                               MasterCodebookSearchReply, counter, allow_user_questions=False, checkpoint=checkpoint)
            try:
                searches = search_candidates(reply['candidates'], targets, source_rows)
            except ValueError as error:
                if checkpoint:
                    checkpoint.next_input({'feedback': str(error)})
                raise
            session.record('master_codebook_search', 'master_to_codebook_reconciler', searches)
        hits = {item['variable_name']: {row['variable_name'] for query in item['candidates'] for row in query['rows']}
                for item in searches}
        hit_names = {name for name, found in hits.items() if found}
        if not hit_names:
            completed_batch(start + BATCH_SIZE)
            session.message('master_to_codebook_reconciler', f'No name candidates found for this batch of {len(targets)} variables; leaving them unchanged.', 'thinking')
            continue
        decision = {**common, 'stage': 'reconcile', 'target_rows': target_frame[
                        target_frame.variable_name.isin(hit_names)].to_dict('records'),
                    'search_results': [item for item in searches if item['variable_name'] in hit_names]}
        if checkpoint and saved_input.get('stage') != 'reconcile':
            checkpoint.next_input(decision)
        reply = agent_turn(session, 'master_to_codebook_reconciler', MASTER_TO_CODEBOOK_PROMPT, history,
                           decision, MasterCodebookUpdateReply, counter,
                           allow_user_questions=False, checkpoint=checkpoint)
        updates, reasons = {}, {}
        candidate_taxonomy, batteries = deepcopy(taxonomy), known_batteries(matched)
        try:
            for update in reply['updates']:
                row = deepcopy(update['row'])
                name, sources, reason = row['variable_name'], update['source_variables'], update['reason'].strip()
                if name not in hit_names or name in updates:
                    raise ValueError('Soft-match updates must name distinct current targets with search hits.')
                if not sources or len(sources) != len(set(sources)) or not set(sources) <= hits[name]:
                    raise ValueError('A soft match must cite rows returned by that variable’s own searches.')
                if not 0 < len(reason.split()) < 30:
                    raise ValueError('Explain each accepted soft match in fewer than 30 words.')
                if row['conflicts'] or not row['variable_label'].strip() or not row['variable_description'].strip():
                    raise ValueError('A soft match needs a useful definition with no unresolved conflict.')
                if (name in row['battery_members'] or len(set(row['battery_members'])) != len(row['battery_members'])
                        or not set(row['battery_members']) <= set(matched)):
                    raise ValueError('Battery peers must be distinct other master-data variables.')
                if row['battery_root'] in batteries:
                    row.update({key: batteries[row['battery_root']][key] for key in ('battery_title', 'theme', 'subtheme')})
                add_theme(candidate_taxonomy, row)
                row['categories'] = row['categories'].strip() or 'not in source'
                updates[name] = row
                reasons[name] = (sources, reason)
        except ValueError as error:
            if checkpoint:
                checkpoint.next_input({'feedback': str(error)})
            raise
        # Align proposed battery fields using the full roster, but only replace returned targets.
        aligned = {**deepcopy(matched), **updates}
        align_batteries(aligned)
        for name in updates:
            matched[name] = aligned[name]
            sources, reason = reasons[name]
            for source in sources:
                annotations.setdefault(source, {})[name] = reason
        taxonomy.update(candidate_taxonomy)
        completed_batch(start + BATCH_SIZE)
        session.message('master_to_codebook_reconciler', f'Soft-matched {len(updates)} of {len(targets)} variables in this batch; all others stay unchanged.', 'thinking')

    if checkpoint and checkpoint.state['phase'] == 'soft_match':
        checkpoint.state.update(phase='export', start=0, agent=None)
        checkpoint.save('phase')

    source_frame['soft_match'] = [[] for _ in range(len(source_frame))]
    for index, row in source_frame.iterrows():
        reasons = annotations.get(row['variable_name'])
        if reasons:
            source_frame.at[index, 'soft_match'] = list(reasons)
            source_frame.at[index, 'other_information'] = '\n\n'.join(filter(None, [
                row['other_information'], 'Soft matches: ' + json.dumps(reasons, ensure_ascii=False)]))
    used = source_frame.variable_name.isin(annotations)
    return final_table(master, matched, entries), source_frame[~used].copy(), source_frame[used].copy()
