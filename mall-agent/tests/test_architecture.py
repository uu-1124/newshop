from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.dto import EntryContext, MessageInput
from app.domain.identity import DomainError
from app.infrastructure.cursors import Cursors
from app.infrastructure.java_bridge import sign
from app.infrastructure.runtime_auth import RuntimeAuth


def test_cursors_cannot_cross_member_or_be_modified():
    codec = Cursors("unit-test-secret")
    token = codec.encode(["2026-11-11T00:00:00+00:00", str(uuid4())], "member:1")
    assert codec.decode(token, "member:1")[0].startswith("2026")
    altered = ("B" if token.startswith("A") else "A") + token[1:]
    for candidate, scope in [(token, "member:2"), (altered, "member:1")]:
        with pytest.raises(DomainError):
            codec.decode(candidate, scope)


def test_credentials_expire_and_never_appear_in_repr():
    auth = RuntimeAuth(0)
    auth.put("run", "sensitive-bearer")
    assert "sensitive-bearer" not in repr(auth.items)
    with pytest.raises(DomainError):
        auth.get("run")


def test_signature_binds_target_and_nonce():
    args = ("GET", "/mall-portal/agent-bridge/v1/identity", b"", "2026-11-11T00:00:00.000Z")
    assert sign(*args, "nonce-a", "secret") != sign(*args, "nonce-b", "secret")
    assert sign(*args, "nonce-a", "secret").startswith("v1:")


def test_inputs_reject_ambiguous_context_and_empty_message():
    with pytest.raises(ValidationError):
        EntryContext(kind="GENERAL", order_id="1")
    with pytest.raises(ValidationError):
        MessageInput(client_message_id=uuid4(), text=" ", attachment_ids=[])
    with pytest.raises(ValidationError):
        MessageInput(client_message_id=uuid4(), text="hi", attachment_ids=[], member_id="2")
