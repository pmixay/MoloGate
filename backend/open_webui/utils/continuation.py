"""Model-input deltas for provider-anchored tool continuations."""


def stateful_tool_messages(output: list[dict], start: int, converter, **kwargs) -> list[dict]:
    """Convert only new tool results/agent inputs, not provider-owned output.

    The ordinary converter deliberately drops orphan results. Supply matching
    calls for conversion, then discard the generated assistant messages: those
    calls are already present in the response identified by previous_response_id.
    """
    delta = [item for item in output[start:] if item.get('type') in {'function_call_output', 'agent_message'}]
    result_ids = {item.get('call_id') for item in delta if item.get('type') == 'function_call_output'}
    calls = [item for item in output if item.get('type') == 'function_call' and item.get('call_id') in result_ids]
    return [message for message in converter([*calls, *delta], **kwargs) if message.get('role') != 'assistant']
