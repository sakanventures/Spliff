"""MCP 2026-07-28 confirmation exchanges for the private stdio transport."""
import secrets
from time import monotonic

from .keep_both import plan_token

VERSION = "2026-07-28"
PREFIX = "io.modelcontextprotocol/"
OPENAI_FORM = "openai/elicitation"
TTL = 180


def form_kind(capabilities):
    core = capabilities.get("elicitation")
    if not isinstance(core, dict) or not isinstance(core.get("form"), dict):
        return None
    extensions = capabilities.get("extensions")
    extension = extensions.get(OPENAI_FORM) if isinstance(extensions, dict) else None
    return "openai" if isinstance(extension, dict) and isinstance(extension.get("form"), dict) else "core"


def accepted(result):
    if not isinstance(result, dict) or result.get("action") != "accept":
        return False
    content = result.get("content")
    return isinstance(content, dict) and content == {"authorize": True} and content["authorize"] is True


class InputRequired(Exception):
    def __init__(self, result):
        self.result = result


class Confirmations:
    def __init__(self):
        self.pending = {}

    def confirm(self, request_id, params, kind, review, form):
        now = monotonic()
        scope = plan_token({"tool": params["name"], "arguments": params.get("arguments", {}),
                            "kind": kind, "review": review})
        if "requestState" in params or "inputResponses" in params:
            state = params.get("requestState")
            issued = self.pending.get(state) if isinstance(state, str) else None
            if not issued or issued[0] <= now or issued[1] != scope or issued[2] == request_id:
                raise ValueError("The in-chat confirmation expired or does not match this review. Request a fresh preview.")
            del self.pending[state]
            responses = params.get("inputResponses")
            response = responses.get("approval") if isinstance(responses, dict) else None
            return accepted(response)

        self.pending = {key: entry for key, entry in self.pending.items() if entry[0] > now}
        if len(self.pending) >= 256:
            raise ValueError("Too many confirmations are pending. Wait for them to expire before retrying.")
        state = secrets.token_urlsafe(32)
        self.pending[state] = (now + TTL, scope, request_id)
        if kind == "openai":
            schema = form["requestedSchema"]
            form = {**form, "requestedSchema": {"type": "object", "properties": {}},
                    "_meta": {OPENAI_FORM: {"requestedSchema": schema}}}
        raise InputRequired({"resultType": "input_required", "requestState": state,
                             "inputRequests": {"approval": {"method": "elicitation/create", "params": form}}})
