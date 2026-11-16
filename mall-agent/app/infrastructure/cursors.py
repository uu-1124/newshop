import base64
import hmac
import json

from app.domain.identity import DomainError


class Cursors:
    def __init__(self, secret: str):
        self.secret = secret.encode()

    def encode(self, value, scope):
        data = json.dumps([scope, value], separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(hmac.digest(self.secret, data, "sha256") + data).decode().rstrip("=")

    def decode(self, token, scope):
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            if not hmac.compare_digest(raw[:32], hmac.digest(self.secret, raw[32:], "sha256")):
                raise ValueError
            claimed, value = json.loads(raw[32:])
            if claimed != scope or not isinstance(value, list):
                raise ValueError
            return value
        except (ValueError, TypeError, UnicodeError):
            raise DomainError("INVALID_CURSOR", "分页游标无效", 400) from None
