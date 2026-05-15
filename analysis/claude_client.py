"""Claude wrapper: prompt caching, retry/backoff, per-call token logging.

`analyze()` keeps its old signature so existing callers don't break, but a new
`analyze_v2()` accepts an explicit (system, user) split so the system prompt
can be marked cacheable. Both paths log to the `claude_calls` table.
"""
import os
import time
import random
import logging
import anthropic
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
DEFAULT_SYSTEM = (
    "Eres un analista de divisas especializado en Venezuela para una empresa hotelera. "
    "Tus respuestas son siempre concisas, directas y en español."
)

# Anthropic's caching minimum is 1024 input tokens for Sonnet. Below that, the
# API silently treats cache_control as a no-op — so we mark stable content
# regardless and only actually save tokens once prompts grow large enough.
_RETRY_MAX = 3
_RETRY_BASE_SLEEP = 1.5  # seconds; exponential

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not set in environment")
        _client = anthropic.Anthropic(api_key=api_key)
    return _client


def _log_usage(prompt_type: str, response, latency_ms: int, error: str | None = None):
    """Best-effort write to claude_calls; never raises."""
    try:
        from db.db import log_claude_call
        usage = getattr(response, "usage", None) if response else None
        log_claude_call(
            prompt_type=prompt_type,
            model=MODEL,
            input_tokens=getattr(usage, "input_tokens", None) if usage else None,
            output_tokens=getattr(usage, "output_tokens", None) if usage else None,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None) if usage else None,
            cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", None) if usage else None,
            latency_ms=latency_ms,
            error=error,
        )
    except Exception as e:
        logger.warning(f"Failed to log Claude usage: {e}")


def _call_with_retry(*, system_blocks, user_text: str, max_tokens: int,
                     prompt_type: str) -> str:
    client = get_client()
    last_exc = None
    for attempt in range(_RETRY_MAX):
        t0 = time.monotonic()
        try:
            response = client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=[{"role": "user", "content": user_text}],
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            text = response.content[0].text
            _log_usage(prompt_type, response, latency_ms)
            return text
        except (anthropic.APIStatusError, anthropic.APIConnectionError, anthropic.RateLimitError) as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            last_exc = e
            transient = isinstance(e, (anthropic.APIConnectionError, anthropic.RateLimitError)) or \
                        getattr(e, "status_code", 0) in (429, 500, 502, 503, 504, 529)
            if not transient or attempt == _RETRY_MAX - 1:
                _log_usage(prompt_type, None, latency_ms, error=f"{type(e).__name__}: {e}")
                raise
            sleep_for = _RETRY_BASE_SLEEP * (2 ** attempt) + random.uniform(0, 0.5)
            logger.warning(f"Claude transient error ({e}); retry {attempt+1}/{_RETRY_MAX} in {sleep_for:.1f}s")
            time.sleep(sleep_for)
        except Exception as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            _log_usage(prompt_type, None, latency_ms, error=f"{type(e).__name__}: {e}")
            raise
    # Should never reach here, but be defensive
    raise last_exc if last_exc else RuntimeError("Claude retry loop exited unexpectedly")


def analyze(prompt: str, max_tokens: int = 500, prompt_type: str = "generic") -> str:
    """Backwards-compatible entry: single user prompt + the default system message."""
    system_blocks = [
        {"type": "text", "text": DEFAULT_SYSTEM, "cache_control": {"type": "ephemeral"}}
    ]
    return _call_with_retry(
        system_blocks=system_blocks,
        user_text=prompt,
        max_tokens=max_tokens,
        prompt_type=prompt_type,
    )


def analyze_v2(system_text: str, user_text: str, max_tokens: int = 500,
               prompt_type: str = "generic") -> str:
    """Explicit (system, user) split. `system_text` is marked cache-eligible —
    if it's stable across calls AND exceeds the model's caching threshold,
    Anthropic will reuse it across requests."""
    system_blocks = [
        {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}
    ]
    return _call_with_retry(
        system_blocks=system_blocks,
        user_text=user_text,
        max_tokens=max_tokens,
        prompt_type=prompt_type,
    )
