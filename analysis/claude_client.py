import os
import anthropic
import logging
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
_client = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def analyze(prompt: str, max_tokens: int = 500) -> str:
    client = get_client()
    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=(
                "Eres un analista de divisas especializado en Venezuela para una empresa hotelera. "
                "Tus respuestas son siempre concisas, directas y en español."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except anthropic.APIError as e:
        logger.error(f"Claude API error: {e}")
        raise
