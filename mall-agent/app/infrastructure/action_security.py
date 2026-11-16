"""Separate execution attestations and encrypted, reconciliation-only grants."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken

from app.domain.identity import DomainError


def parameter_hash(member_id, parameters):
    canonical = {
        "schema_version": "agent-action-v1",
        "action_type": parameters["action_type"],
        "member_id": str(member_id),
        "parameters": parameters,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


class ActionSecurity:
    def __init__(self, settings):
        self.confirmation = settings.confirmation_secret.get_secret_value()
        self.reconciliation = settings.reconciliation_secret.get_secret_value()
        self.envelope = Fernet(settings.action_encryption_key.get_secret_value().encode())

    def issue(self, action, audit_id, confirmed_at):
        now = datetime.now(UTC)
        claims = {
            "audience": {
                "ADD_CART": "mall-portal-agent-cart",
                "CREATE_RETURN": "mall-portal-agent-return",
            }.get(action["action_type"], "mall-portal-agent-order"),
            "key_id": "v1",
            "operation_no": str(action["operation_no"]),
            "action_id": str(action["id"]),
            "member_id": str(action["member_id"]),
            "action_type": action["action_type"],
            "parameters_hash": action["parameters_hash"],
            "control_epoch": action["control_epoch"],
            "quote_id": str(action["quote_id"]),
            "confirmation_audit_id": str(audit_id),
            "confirmed_at": confirmed_at.isoformat(),
            "issued_at": int(now.timestamp()),
        }

        def sign(purpose, secret, expiry):
            payload = base64.urlsafe_b64encode(
                json.dumps(
                    claims | {"purpose": purpose, "expires_at": int(expiry.timestamp())},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).rstrip(b"=")
            signature = base64.urlsafe_b64encode(hmac.digest(secret.encode(), payload, "sha256")).rstrip(b"=")
            return (payload + b"." + signature).decode()

        expiry = now + timedelta(days=7)
        execution = sign("execute_confirmed", self.confirmation, now + timedelta(seconds=120))
        grant = sign("reconcile_only", self.reconciliation, expiry)
        return execution, self.envelope.encrypt(grant.encode()), expiry

    def decrypt(self, encrypted):
        try:
            return self.envelope.decrypt(bytes(encrypted)).decode()
        except (InvalidToken, ValueError):
            raise DomainError("RECONCILE_GRANT_UNAVAILABLE", "操作需要人工核对", 409) from None

    def renew_reconciliation(self, action, audit_id, confirmed_at):
        """Renew only original-operation read/finalize authority, never execute."""
        now = datetime.now(UTC)
        expiry = now + timedelta(days=7)
        claims = {
            "audience": {
                "ADD_CART": "mall-portal-agent-cart",
                "CREATE_RETURN": "mall-portal-agent-return",
            }.get(action["action_type"], "mall-portal-agent-order"),
            "key_id": "v1",
            "purpose": "reconcile_only",
            "operation_no": str(action["operation_no"]),
            "action_id": str(action["id"]),
            "member_id": str(action["member_id"]),
            "action_type": action["action_type"],
            "parameters_hash": action["parameters_hash"],
            "control_epoch": action["control_epoch"],
            "quote_id": str(action["quote_id"]),
            "confirmation_audit_id": str(audit_id),
            "confirmed_at": confirmed_at.isoformat(),
            "issued_at": int(now.timestamp()),
            "expires_at": int(expiry.timestamp()),
        }
        payload = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=")
        signature = base64.urlsafe_b64encode(
            hmac.digest(self.reconciliation.encode(), payload, "sha256")
        ).rstrip(b"=")
        return self.envelope.encrypt(payload + b"." + signature), expiry

    def proof(self, action, attachment):
        data = {
            "attachment_id": str(attachment["attachment_id"]),
            "attachment_ref_id": str(attachment["ref_id"]),
            "sha256": attachment["sha256"],
            "mime_type": attachment["mime_type"],
            "byte_size": attachment["byte_size"],
        }
        claims = data | {
            "purpose": "return_proof",
            "operation_no": str(action["operation_no"]),
            "member_id": str(action["member_id"]),
            "expires_at": int(datetime.now(UTC).timestamp()) + 120,
        }
        payload = base64.urlsafe_b64encode(
            json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
        ).rstrip(b"=")
        signature = base64.urlsafe_b64encode(
            hmac.digest(self.confirmation.encode(), payload, "sha256")
        ).rstrip(b"=")
        return data | {"attestation": (payload + b"." + signature).decode()}
