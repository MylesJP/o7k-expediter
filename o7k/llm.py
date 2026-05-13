"""OpenRouter LLM API client.

Reads the API key from OPENROUTER_API_KEY env var and model IDs from
resources/models.yaml.  Provides a single `call()` function used by the
skill runner.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MODELS_PATH = _REPO_ROOT / "resources" / "models.yaml"
_ENV_PATH = _REPO_ROOT / ".env"

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _load_models() -> dict[str, str]:
    """Return {tier: model_id} from models.yaml."""
    return yaml.safe_load(_MODELS_PATH.read_text())


def _env_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value or not _ENV_PATH.exists():
        return value

    for raw in _ENV_PATH.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, env_value = line.split("=", 1)
        if key.strip() == name:
            return env_value.strip().strip('"').strip("'")
    return ""


def call(prompt: str, *, model_tier: str = "cheap", temperature: float = 0.2) -> str:
    """Send *prompt* to OpenRouter and return the assistant response text.

    *model_tier* is looked up in resources/models.yaml (cheap or heavy).
    Raises RuntimeError on API errors.
    """
    api_key = _env_value("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. Export it or add to .env."
        )

    models = _load_models()
    model_id = models.get(model_tier)
    if not model_id:
        raise RuntimeError(
            f"Unknown model_tier {model_tier!r}. "
            f"Available: {list(models.keys())}"
        )

    payload = {
        "model": model_id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=data,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/MylesJP/o7k-expediter",
            "X-Title": "o7k-expediter",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"OpenRouter API error {e.code}: {error_body}"
        ) from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"OpenRouter network error: {e}") from e

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices: {body}")

    return choices[0]["message"]["content"]
