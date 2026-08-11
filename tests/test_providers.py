"""Workers AI adapter: same messages.parse surface, OpenAI-compatible wire."""

import json

import httpx
import pytest
from pydantic import BaseModel

from app import config
from app.providers import (
    ProviderError,
    WorkersAIClient,
    extract_json,
    segment_client,
)


class Toy(BaseModel):
    name: str
    score: float


@pytest.fixture(autouse=True)
def cf_credentials(monkeypatch):
    monkeypatch.setattr(config, "CF_ACCOUNT_ID", "acct-123")
    monkeypatch.setattr(config, "CF_API_TOKEN", "tok-456")


def make_client(handler, model=None):
    transport = httpx.MockTransport(handler)
    return WorkersAIClient(model=model,
                           http_client=httpx.Client(transport=transport))


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content}}],
    })


def test_extract_json_handles_fences_prose_and_nesting():
    assert extract_json('{"a": 1}') == '{"a": 1}'
    assert extract_json('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert extract_json('Sure! Here it is: {"a": {"b": 2}} hope that helps') \
        == '{"a": {"b": 2}}'


def test_parse_happy_path_builds_openai_request():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["Authorization"]
        seen["payload"] = json.loads(request.content)
        return chat_reply(json.dumps({"name": "riya", "score": 4.5}))

    client = make_client(handler)
    response = client.messages.parse(
        model="claude-opus-5",  # ignored: the Workers model is fixed per client
        max_tokens=500,
        system=[{"type": "text", "text": "grade things"},
                {"type": "text", "text": "rubric here",
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "grade riya"}],
        output_format=Toy,
    )

    assert response.parsed_output == Toy(name="riya", score=4.5)
    assert response.stop_reason == "end_turn"
    assert "acct-123" in seen["url"]
    assert seen["auth"] == "Bearer tok-456"
    payload = seen["payload"]
    assert payload["model"] == config.WORKERS_AI_EVAL_MODEL
    assert payload["response_format"]["type"] == "json_schema"
    system = payload["messages"][0]
    assert system["role"] == "system"
    assert "grade things" in system["content"]
    assert "rubric here" in system["content"]      # blocks flattened
    assert "json schema" in system["content"].lower()


def test_parse_retries_once_on_invalid_json():
    replies = ["not json at all", json.dumps({"name": "x", "score": 1})]
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return chat_reply(replies[len(calls) - 1])

    client = make_client(handler)
    response = client.messages.parse(
        messages=[{"role": "user", "content": "go"}], output_format=Toy)

    assert response.parsed_output.name == "x"
    assert len(calls) == 2
    # The retry shows the model its own bad reply plus a correction.
    assert calls[1]["messages"][-2]["content"] == "not json at all"
    assert "not a valid json" in calls[1]["messages"][-1]["content"].lower()


def test_parse_falls_back_when_response_format_rejected():
    def handler(request):
        payload = json.loads(request.content)
        if "response_format" in payload:
            return httpx.Response(400, json={"errors": [{"message": "no"}]})
        return chat_reply(json.dumps({"name": "y", "score": 2}))

    client = make_client(handler)
    response = client.messages.parse(
        messages=[{"role": "user", "content": "go"}], output_format=Toy)
    assert response.parsed_output.name == "y"


def test_parse_gives_up_after_second_bad_reply():
    client = make_client(lambda request: chat_reply("still not json"))
    with pytest.raises(ProviderError):
        client.messages.parse(
            messages=[{"role": "user", "content": "go"}], output_format=Toy)


def test_image_blocks_are_a_hard_error():
    client = make_client(lambda request: chat_reply("{}"))
    with pytest.raises(ProviderError, match="text-only"):
        client.messages.parse(
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {}},
                {"type": "text", "text": "read this"},
            ]}],
            output_format=Toy,
        )


def test_missing_credentials_fail_loudly(monkeypatch):
    monkeypatch.setattr(config, "CF_API_TOKEN", "")
    with pytest.raises(ProviderError, match="CF_ACCOUNT_ID"):
        WorkersAIClient()


def test_segment_client_downshifts_to_cheap_model():
    client = make_client(lambda request: chat_reply("{}"))
    seg = segment_client(client)
    assert seg.model == config.WORKERS_AI_SEGMENT_MODEL
    # Non-Workers clients (Anthropic, fakes) pass through untouched.
    sentinel = object()
    assert segment_client(sentinel) is sentinel
