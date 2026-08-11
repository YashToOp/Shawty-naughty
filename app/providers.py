"""Model-provider adapters.

The whole pipeline talks to models through one surface — the Anthropic
client's ``client.messages.parse(...)`` returning ``.parsed_output`` and
``.stop_reason``. The free stack swaps the backend, not the call sites:
WorkersAIClient exposes the same duck-typed surface over Cloudflare
Workers AI's OpenAI-compatible endpoint, so the evaluator, marking-scheme
generator and OCR segmentation run unchanged on either provider.

Text only: the Workers AI models used here do not read images. Vision work
in free mode goes through Google Cloud Vision OCR (see vision_ocr.py) —
passing an image/document block to this adapter is a hard error, never a
silent skip.
"""

import json
import re
from typing import Optional, Type

import httpx
from pydantic import BaseModel, ValidationError

from . import config


class ProviderError(RuntimeError):
    pass


class WorkersAIParseResponse:
    """Duck-type of the Anthropic parse response (the two fields we use)."""

    def __init__(self, parsed_output, stop_reason: str = "end_turn"):
        self.parsed_output = parsed_output
        self.stop_reason = stop_reason


_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> str:
    """Pull the first JSON object out of a model reply (fences, prose, etc.)."""
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start == -1:
        return text.strip()
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return text[start:].strip()


def _flatten(content) -> str:
    """Anthropic content (string or blocks) -> plain text; vision is an error."""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        btype = block.get("type") if isinstance(block, dict) else None
        if btype == "text":
            parts.append(block["text"])
        elif btype in ("image", "document"):
            raise ProviderError(
                "The Workers AI adapter is text-only. Vision work must go "
                "through OCR_PROVIDER=google-vision (see vision_ocr.py)."
            )
        else:
            raise ProviderError(
                f"Unsupported content block for Workers AI: {btype!r}"
            )
    return "\n\n".join(parts)


class WorkersAIMessages:
    def __init__(self, client: "WorkersAIClient"):
        self._client = client

    def parse(self, *, model: Optional[str] = None, max_tokens: int = 4096,
              system=None, messages=(), output_format: Type[BaseModel] = None,
              **_ignored) -> WorkersAIParseResponse:
        """Same call shape as anthropic messages.parse; ``model`` is ignored —
        the Workers model is fixed per client (eval vs segmentation)."""
        schema = output_format.model_json_schema()
        system_text = _flatten(system) if system else ""
        system_text += (
            "\n\nRespond with a single JSON object that validates against this "
            "JSON schema. No prose, no markdown fences, JSON only:\n"
            + json.dumps(schema)
        )
        chat = [{"role": "system", "content": system_text}]
        chat += [{"role": m["role"], "content": _flatten(m["content"])}
                 for m in messages]

        payload = {
            "model": self._client.model,
            "max_tokens": max_tokens,
            "messages": chat,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": output_format.__name__,
                                "schema": schema},
            },
        }

        content = self._complete(payload)
        try:
            parsed = output_format.model_validate_json(extract_json(content))
        except (ValidationError, ValueError) as first_error:
            # One corrective retry: show the model its reply and the error.
            payload["messages"] = chat + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": (
                    "Your previous reply was not a valid JSON object for the "
                    f"schema ({first_error}). Reply again with only the "
                    "corrected JSON object."
                )},
            ]
            content = self._complete(payload)
            try:
                parsed = output_format.model_validate_json(extract_json(content))
            except (ValidationError, ValueError) as second_error:
                raise ProviderError(
                    f"Workers AI ({self._client.model}) returned no parseable "
                    f"{output_format.__name__}: {second_error}"
                ) from second_error
        return WorkersAIParseResponse(parsed)

    def _complete(self, payload: dict) -> str:
        try:
            data = self._client.post(payload)
        except httpx.HTTPStatusError as exc:
            # Some Workers AI models reject response_format — retry without
            # it; the schema instruction in the system prompt still applies.
            if exc.response.status_code == 400 and "response_format" in payload:
                slim = {k: v for k, v in payload.items() if k != "response_format"}
                data = self._client.post(slim)
            else:
                raise ProviderError(
                    f"Workers AI request failed ({exc.response.status_code}): "
                    f"{exc.response.text[:500]}"
                ) from exc
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"Unexpected Workers AI response shape: {str(data)[:500]}"
            ) from exc


class WorkersAIClient:
    """Minimal Anthropic-client look-alike over Cloudflare Workers AI."""

    def __init__(self, model: Optional[str] = None,
                 http_client: Optional[httpx.Client] = None):
        if not (config.CF_ACCOUNT_ID and config.CF_API_TOKEN):
            raise ProviderError(
                "EVAL_PROVIDER=workers-ai needs CF_ACCOUNT_ID and CF_API_TOKEN "
                "(see .env.example)."
            )
        self.model = model or config.WORKERS_AI_EVAL_MODEL
        self._http = http_client or httpx.Client(timeout=120)
        self.messages = WorkersAIMessages(self)

    @property
    def url(self) -> str:
        return (
            "https://api.cloudflare.com/client/v4/accounts/"
            f"{config.CF_ACCOUNT_ID}/ai/v1/chat/completions"
        )

    def post(self, payload: dict) -> dict:
        response = self._http.post(
            self.url, json=payload,
            headers={"Authorization": f"Bearer {config.CF_API_TOKEN}"},
        )
        response.raise_for_status()
        return response.json()


def segment_client(client):
    """The text model for OCR segmentation: mechanical work, so in
    workers-ai mode it runs on the cheap model; any other client is reused."""
    if isinstance(client, WorkersAIClient) and \
            client.model != config.WORKERS_AI_SEGMENT_MODEL:
        return WorkersAIClient(model=config.WORKERS_AI_SEGMENT_MODEL,
                               http_client=client._http)
    return client
