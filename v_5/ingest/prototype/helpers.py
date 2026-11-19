"""Only the shared API call and readable printing live here."""

import json
from typing import Any
from threading import Lock
from openai import OpenAI
from v_5.app.config import MODEL, api_key

PRINT_LOCK = Lock()


def show(name, value):
    """Print a readable notebook result without interleaving threads.

    Inputs: name: heading; value: result payload.
    Returns: None; prints a labeled JSON block.
    Details: Prototype file reviewers use a shared print lock when independent agents finish
        together.
    """
    with PRINT_LOCK:
        print('\n' + '=' * 70)
        print(name)
        print('-' * 70)
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def ask_agent(name, prompt, history, contract):
    """Call a structured notebook agent and display its reply.

    Inputs: name/prompt: role and instructions; history: mutable messages; contract:
        response model.
    Returns: Parsed reply dict; appends its assistant message and prints reply/usage.
    Details: Prototype helper using the SDK parse call; the live app uses Session.ask_agent
        instead.
    """
    # The SDK generates the API schema from the adjacent Python contract.
    response = OpenAI(api_key=api_key()).responses.parse(
        model=MODEL,
        input=[{'role': 'system', 'content': prompt}, *history],
        text_format=contract,
        reasoning={'effort': 'medium'},
        store=False,
    )
    reply = response.output_parsed.model_dump()
    history.append({'role': 'assistant', 'content': json.dumps(reply)})
    show(name, reply)
    show(name + ' — tokens', response.usage.model_dump())
    return reply


def loader_request(prompt, history, contract=None):
    """Build the notebook's shared count-and-call request.

    Inputs: prompt: system text; history: messages; contract: optional response model.
    Returns: Request dict with model, messages, reasoning and optional schema.
    Details: Prototype loader passes the same shape to count_input and call_loader.
    """
    from openai.lib._parsing._responses import type_to_text_format_param
    request = dict(model=MODEL, input=[{'role': 'system', 'content': prompt}, *history],
                   reasoning={'effort': 'medium'})
    if contract is not None:
        request['text'] = {'format': type_to_text_format_param(contract)}
    return request


def count_input(request):
    """Count the notebook request's input tokens through the API.

    Inputs: request: prepared model payload.
    Returns: Integer input-token count.
    Details: Used by prototype budget checks; calls the endpoint directly for SDK
        compatibility.
    """
    # Direct endpoint also works with SDK versions predating input_tokens.count.
    return OpenAI(api_key=api_key()).post(
        '/responses/input_tokens', body=request, cast_to=dict[str, Any])['input_tokens']


def call_loader(name, request, max_output_tokens):
    """Call the notebook loader and print its output and usage.

    Inputs: name: display label; request: API payload; max_output_tokens: output limit.
    Returns: (text, usage, status): raw output, usage dict and completion state.
    Details: Prototype loader/summarizer helper. JSON is parsed only for display; callers
        receive the original text.
    """
    response = OpenAI(api_key=api_key()).responses.create(
        **request, max_output_tokens=max_output_tokens, store=False)
    usage = response.usage.model_dump()
    try:
        display = json.loads(response.output_text)
    except json.JSONDecodeError:
        display = response.output_text
    show(name, display)
    show(name + ' — tokens', usage)
    return response.output_text, usage, response.status
