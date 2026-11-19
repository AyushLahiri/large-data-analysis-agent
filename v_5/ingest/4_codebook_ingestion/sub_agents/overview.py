from random import Random
from pathlib import Path
from v_5.ingest.codebook_ingestion.sub_agents.pdf_state import read_json, save_json
from v_5.app.config import CODEBOOK_PDF_MODEL
from v_5.ingest.codebook_ingestion.sessions import agent_turn
from v_5.ingest.codebook_ingestion.sub_agents.agent import CODEBOOK_OVERVIEW_PROMPT, CodebookOverviewReply


def planning_overview(session, names, entries, hierarchy, types, *, destination=None):
    """Summarize a sample of variable labels for codebook planning.

    When: Phase 4 subagent codebook_overview_agent, before final_codebook_creator starts
        reconciliation.
    Inputs: session: model calls; names: variable order; entries: source definitions;
        hierarchy: source priorities; types: prepared types; destination: optional final
        overview file, reused after later preparation failures.
    Returns: Dict with overview, sample_notes, variables_seen and variables_total; the
        overview is a planning hint.
    Flow: Samples leading and later variables reproducibly, chooses labels by source
        priority, summarizes samples and consolidates their notes. Only the complete
        result is saved atomically; failed attempts restart all samples without batch flags.
    """
    if destination is not None:
        destination = Path(destination)
        if destination.is_file():
            return read_json(destination)
    samples = [names[index:index + 50] for index in range(0, min(300, len(names)), 50)]
    rest = names[300:]
    random_names = Random(17).sample(rest, min(60, len(rest)))
    samples.extend(random_names[index:index + 20] for index in range(0, len(random_names), 20))
    counter = {'used': 0, 'thread': 1}

    def summarize(payload):
        """Ask for a short, bounded codebook planning note.

        Inputs: payload: sample rows or notes to consolidate; captures session and shared budget
            counter.
        Returns: Dict of categories and note, with count/word limits applied.
        Details: Phase 4 codebook_overview_agent uses a fresh history via agent_turn and never
            opens user questions.
        """
        reply = agent_turn(session, 'codebook_overview_agent', CODEBOOK_OVERVIEW_PROMPT, [], payload,
                           CodebookOverviewReply, counter, CODEBOOK_PDF_MODEL, output_limit=1500,
                           allow_user_questions=False)
        return {'categories': [' '.join(label.split()[:5]) for label in reply['categories'][:30]],
                'note': ' '.join(reply['note'].split()[:100])}

    notes = []
    for sample in samples:
        rows = []
        for name in sample:
            ranked = sorted(entries.get(name, []), key=lambda entry: hierarchy[entry['source_id']]['priority'])
            label = next((entry['variable_label'] for entry in ranked if entry['variable_label']), '')
            values = next((entry['values'] for entry in ranked if entry['values']), types.get(name, ''))
            rows.append({'variable_name': name, 'variable_label': label, 'categories_or_type': values})
        notes.append(summarize({'sample': rows}))
        session.message('codebook_overview_agent', f'Reviewed a sample of {len(rows)} labels for codebook planning.', 'thinking')
    overview = summarize({'instruction': 'Consolidate the sample notes into one overview.', 'notes': notes}) if notes else {}
    result = {'overview': overview, 'sample_notes': notes, 'variables_seen': sum(map(len, samples)), 'variables_total': len(names)}
    if destination is not None:
        save_json(destination, result)
    return result
