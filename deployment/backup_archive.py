"""Authenticated encrypted backup artifacts; keys stay outside the workspace."""

import hashlib
import hmac
import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

SCHEMA = "agent-consistent-backup-v1"
MAX_ARTIFACT = 512 * 1024 * 1024


class BackupError(Exception):
    """A safe operational error: never include source bytes or credentials."""


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def identifier(value):
    parsed = str(UUID(str(value)))
    if parsed != value:
        raise BackupError("Noncanonical backup identifier")
    return parsed


def artifact_name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9.-]{0,79}", value):
        raise BackupError("Invalid artifact name")
    return value


def protect_file(path):
    path.chmod(0o600)
    if os.name == "nt":
        subprocess.run(
            ["icacls.exe", str(path), "/inheritance:r", "/grant:r", os.environ["USERNAME"] + ":(F)"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


def key_folder():
    return Path.home() / ".codex" / "newshop-backup-keys"


def safe_file(directory, name):
    path = directory / name
    if path.is_symlink() or path.resolve().parent != directory.resolve():
        raise BackupError("Artifact escaped its registered backup directory")
    return path


class ArchiveWriter:
    def __init__(self, folder, keys=None, plan_id=None):
        self.id = identifier(plan_id or str(uuid4()))
        self.folder = Path(folder) / self.id
        self.folder.mkdir(parents=True, exist_ok=False)
        self.keys = Path(keys) if keys else key_folder()
        self.keys.mkdir(parents=True, exist_ok=True)
        self.key = os.urandom(32)
        self.key_path = self.keys / (self.id + ".key")
        with self.key_path.open("xb") as output:
            output.write(self.key)
        protect_file(self.key_path)
        self.artifacts = {}
        self.sealed = False

    def add(self, name, chunks):
        artifact_name(name)
        if self.sealed or name in self.artifacts:
            raise BackupError("Artifact already sealed")
        iv = os.urandom(12)
        encryptor = Cipher(algorithms.AES(self.key), modes.GCM(iv)).encryptor()
        encryptor.authenticate_additional_data((SCHEMA + ":" + self.id + ":" + name).encode())
        plaintext, ciphertext, size = hashlib.sha256(), hashlib.sha256(), 0
        output = safe_file(self.folder, name + ".enc")
        try:
            with output.open("xb") as stream:
                for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_ARTIFACT:
                        raise BackupError("Artifact exceeds registered backup budget")
                    plaintext.update(chunk)
                    data = encryptor.update(chunk)
                    ciphertext.update(data)
                    stream.write(data)
                data = encryptor.finalize()
                ciphertext.update(data)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            self.artifacts[name] = {
                "file": output.name,
                "bytes": size,
                "iv": iv.hex(),
                "tag": encryptor.tag.hex(),
                "plaintext_sha256": plaintext.hexdigest(),
                "ciphertext_sha256": ciphertext.hexdigest(),
            }
        except BaseException:
            # Only this newly created artifact is removed. Never delete source data.
            output.unlink(missing_ok=True)
            raise

    def json(self, name, value):
        self.add(name, [canonical(value)])

    def seal(self, metadata):
        if self.sealed or not self.artifacts:
            raise BackupError("Backup cannot be sealed")
        manifest = {
            "schema": SCHEMA,
            "id": self.id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "algorithm": "AES-256-GCM",
            "artifacts": self.artifacts,
            "metadata": metadata,
        }
        manifest["hmac_sha256"] = hmac.new(
            self.key, b"manifest:" + canonical(manifest), hashlib.sha256
        ).hexdigest()
        with (self.folder / "manifest.json").open("xb") as stream:
            stream.write(canonical(manifest))
            stream.flush()
            os.fsync(stream.fileno())
        self.sealed = True
        return manifest


class ArchiveReader:
    def __init__(self, folder, plan_id, keys=None):
        self.id = identifier(plan_id)
        self.folder = Path(folder) / self.id
        if self.folder.is_symlink():
            raise BackupError("Backup directory may not be a symlink")
        key_path = (Path(keys) if keys else key_folder()) / (self.id + ".key")
        self.key = key_path.read_bytes()
        if len(self.key) != 32:
            raise BackupError("Invalid backup key")
        raw = safe_file(self.folder, "manifest.json").read_bytes()
        if len(raw) > 1024 * 1024:
            raise BackupError("Backup manifest exceeds budget")
        self.manifest = json.loads(raw)
        signed = dict(self.manifest)
        signature = signed.pop("hmac_sha256", "")
        expected = hmac.new(self.key, b"manifest:" + canonical(signed), hashlib.sha256).hexdigest()
        if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
            raise BackupError("Backup manifest authentication failed")
        if (
            signed.get("schema") != SCHEMA
            or signed.get("id") != self.id
            or signed.get("algorithm") != "AES-256-GCM"
        ):
            raise BackupError("Backup manifest version mismatch")

    def read(self, name):
        artifact_name(name)
        spec = self.manifest["artifacts"][name]
        if spec["file"] != name + ".enc" or not 0 <= spec["bytes"] <= MAX_ARTIFACT:
            raise BackupError("Invalid artifact registration")
        encrypted_path = safe_file(self.folder, spec["file"])
        if encrypted_path.stat().st_size > MAX_ARTIFACT:
            raise BackupError("Encrypted artifact exceeds budget")
        encrypted = encrypted_path.read_bytes()
        if hashlib.sha256(encrypted).hexdigest() != spec["ciphertext_sha256"]:
            raise BackupError("Encrypted artifact hash mismatch")
        try:
            decryptor = Cipher(
                algorithms.AES(self.key), modes.GCM(bytes.fromhex(spec["iv"]), bytes.fromhex(spec["tag"]))
            ).decryptor()
            decryptor.authenticate_additional_data((SCHEMA + ":" + self.id + ":" + name).encode())
            # Authentication completes before any plaintext is returned to a restore consumer.
            plain = decryptor.update(encrypted) + decryptor.finalize()
        except Exception:
            raise BackupError("Artifact authentication failed") from None
        if len(plain) != spec["bytes"] or hashlib.sha256(plain).hexdigest() != spec["plaintext_sha256"]:
            raise BackupError("Plaintext artifact hash mismatch")
        return plain

    def json(self, name):
        return json.loads(self.read(name))

    def verify(self):
        for name in self.manifest["artifacts"]:
            self.read(name)
        return len(self.manifest["artifacts"])

    def record_result(self, result):
        if result.get("restore_id") != self.id:
            raise BackupError("Restore result ownership mismatch")
        signed = dict(result)
        signed["hmac_sha256"] = hmac.new(
            self.key, b"restore-result:" + canonical(result), hashlib.sha256
        ).hexdigest()
        target = safe_file(self.folder, "result.json")
        temporary = safe_file(self.folder, "result.json.tmp")
        with temporary.open("xb") as output:
            output.write(canonical(signed))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        return signed

    def result(self):
        result = json.loads(safe_file(self.folder, "result.json").read_bytes())
        signature = result.pop("hmac_sha256", "")
        expected = hmac.new(self.key, b"restore-result:" + canonical(result), hashlib.sha256).hexdigest()
        if result.get("restore_id") != self.id or not hmac.compare_digest(signature, expected):
            raise BackupError("Restore result authentication failed")
        return result
