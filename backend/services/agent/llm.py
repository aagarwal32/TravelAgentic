'''
note from duc: im not sure whether this file will be used to store api/model calls or
to store the model that wilson will create from the flight data.
leaving model call logic here for now
'''

"""Shared model transport. Authentication stays outside graph state."""
from openai import OpenAI
from services.agent.nodes.chatgpt_auth import get_access_token


def generate(model: str, instructions: str, context: str) -> str:
    """Send a Responses request and return only successfully completed text."""
    parts = []
    completed = False
    # Replace this credential provider with API-key configuration when available.
    with OpenAI(api_key=get_access_token(), max_retries=0, timeout=60) as client:
        with client.responses.create(
            model=model,
            instructions=instructions,
            input=[{"role": "user", "content": context}],
            store=False,
            stream=True,
        ) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    parts.append(event.delta)
                elif event.type == "response.completed":
                    completed = True
                elif event.type in ("response.failed", "response.incomplete", "error"):
                    raise RuntimeError("Model request failed or did not complete.")
    if not completed:
        raise RuntimeError("Model stream ended without completion.")
    text = "".join(parts).strip()
    if not text:
        raise RuntimeError("Model returned an empty response.")
    return text
