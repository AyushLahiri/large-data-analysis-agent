import json
from v_5.main_functions.session import loader_request


BUDGET = 250_000


def agent_turn(session, name, prompt, history, payload, contract, counter, model=None, *,
               output_limit=16000, allow_user_questions=True, allow_rollover=False, checkpoint=None):
    """Run one budgeted structured codebook-agent turn.

    When: Phase 4 main agent and codebook extraction, merge and overview subagents.
    Inputs: session/name/prompt: caller and role; history: mutable messages; payload:
        current task; contract: reply model; counter: used/thread; model: optional override;
        output_limit and permission flags: call controls; checkpoint: optional final-batch
        recovery owner that stores only the current exchange and keeps history remotely.
    Returns: Validated reply dict; appends history, updates counter and records
        usage/progress.
    Flow: Counts input before calling. If allowed, rollover retains only the current
        self-contained payload and resets the budget; otherwise exhaustion or incomplete
        output raises.
    """
    if checkpoint is not None:
        return checkpoint.call(name, prompt, payload, contract, counter,
                               output_limit=output_limit, model=model)
    message = {'role': 'user', 'content': json.dumps(payload, ensure_ascii=False, default=str)}
    history.append(message)
    while True:
        request = loader_request(prompt, history, contract)
        if model:
            request['model'] = model
            # Apply the effort after the override so Luna does not inherit Sol's setting.
            request['reasoning'] = {'effort': 'high' if model == 'gpt-6-luna' else 'medium'}
        tokens = session.count_input(request, agent=name)
        if counter['used'] + tokens + output_limit + 512 > BUDGET:
            if not allow_rollover or (len(history) == 1 and not counter['used']):
                raise RuntimeError(f'{name}: the next call would exceed the {BUDGET:,}-token session budget. Saved work is retained.')
            # Rollover is safe only for callers whose latest payload points to all necessary saved work.
            history[:] = [message]
            counter.update(used=0, thread=counter['thread'] + 1)
            session.record('codebook_thread_rollover', name, dict(counter))
            continue
        request['input'].append({'role': 'user', 'content': f"Thread {counter['thread']}: {BUDGET-counter['used']-tokens} tokens remain after this input; output includes reasoning."})
        options = {} if allow_user_questions else {'allow_user_questions': False}
        text, usage, status = session.call_loader(name, request, output_limit, **options)
        counter['used'] += usage['input_tokens'] + usage['output_tokens']
        if status != 'completed':
            raise RuntimeError(f'{name} returned an incomplete response; saved work is retained.')
        result = contract.model_validate_json(text).model_dump()
        history.append({'role': 'assistant', 'content': text})
        session.record('codebook_token_usage', name, {**counter, 'usage': usage})
        if name.startswith('pdf_codebook'):
            filename = name.split(' — ', 1)[-1]
            page = payload.get('current_page', {}).get('page')
            progress = (f"{filename} · Page {page}: extracted {len(result['rows'])} variable rows and "
                        f"{len(result['general_information'])} general-information entries."
                        if page else f"{filename}: combined {len(result['rows'])} matching variable rows with their existing definitions.")
            session.message(name, progress, 'thinking', {'observations': [
                f"Input tokens: {usage['input_tokens']:,}; output tokens: {usage['output_tokens']:,} (includes reasoning).",
                *result.get('issues', [])]})
        return result
