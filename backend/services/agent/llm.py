"""Shared model call for the agent: Claude through the Anthropic API.

The API key comes from settings (backend/.env) and never enters graph state.
"""
import anthropic
from anthropic import Anthropic

from core.config import settings

# Extraction is short and well specified, so low effort keeps it fast and cheap.
# Raise to "medium" if replies come back wrong on harder requests.
EFFORT = "low"
MAX_TOKENS = 16000


def generate(model: str, instructions: str, context: str) -> str:
    """Send one request to Claude and return its text reply.

    Raises RuntimeError with a short message (never the API key) when the key is
    missing, the request fails, or the reply was cut off or declined.
    """
    if not settings.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY is not set in backend/.env.")
    client = Anthropic(api_key=settings.ANTHROPIC_API_KEY, timeout=60)
    try:
        response = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=instructions,
            messages=[{"role": "user", "content": context}],
            output_config={"effort": EFFORT},
        )
    except anthropic.AuthenticationError:
        raise RuntimeError("The Anthropic API key was rejected. Check ANTHROPIC_API_KEY.") from None
    except anthropic.NotFoundError:
        raise RuntimeError(f"Model {model!r} was not found. Check LLM_MODEL.") from None
    except anthropic.RateLimitError:
        raise RuntimeError("Rate limited by the Anthropic API. Try again shortly.") from None
    except anthropic.APIStatusError as e:
        raise RuntimeError(f"The Anthropic API returned an error ({e.status_code}).") from None
    except anthropic.APIConnectionError:
        raise RuntimeError("Could not reach the Anthropic API. Check your connection.") from None

    if response.stop_reason == "refusal":
        raise RuntimeError("The model declined this request.")
    if response.stop_reason == "max_tokens":
        raise RuntimeError("The model's reply was cut off.")
    # Thinking blocks may come first; only text blocks are the answer
    text = "".join(block.text for block in response.content if block.type == "text").strip()
    if not text:
        raise RuntimeError("The model returned an empty response.")
    return text
