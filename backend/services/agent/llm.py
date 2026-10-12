"""Shared OAuth model transport, reusing flight-agent's tested streaming node."""
from services.agent.nodes.call_model import call_model


def generate(model: str, instructions: str, context: str) -> str:
    # Keep credentials entirely outside graph state.
    result = call_model({"model": model, "input": context, "output": "",
                         "instructions": instructions})
    text = result["output"].strip()
    if not text:
        raise RuntimeError("Model returned an empty response.")
    return text
