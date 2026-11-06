"""A fake OpenAI Chat Completions server built on httpx.MockTransport.

It reads the request exactly as OpenAI would receive it (system prompt, schema, INPUT
block) and answers with the deterministic mock responders. This exercises the real
OpenAIProvider wire format end to end without network access.
"""

from __future__ import annotations

import json

import httpx

from app.llm.mock_responders import DEFAULT_RESPONDERS


def _operation(system: str, payload: dict) -> str:
    if "Triage Agent" in system:
        return "triage"
    if "Planning Agent" in system:
        return "plan_remediation" if "findings" in payload else "plan_investigation"
    if "Policy Reviewer" in system:
        return "policy_review"
    if "argument resolver" in system:
        return "resolve_arguments"
    if "Verification Agent" in system:
        return "verify"
    raise AssertionError("unknown agent prompt")


class FakeOpenAI:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        payload = json.loads(user.split("INPUT:\n<<<\n", 1)[1].rsplit("\n>>>", 1)[0])
        answer = DEFAULT_RESPONDERS[_operation(system, payload)](payload)
        return httpx.Response(
            200,
            json={
                "model": body["model"],
                "choices": [{"message": {"content": json.dumps(answer)}}],
                "usage": {"prompt_tokens": len(user) // 4, "completion_tokens": 50},
            },
        )

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)
