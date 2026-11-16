import hashlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def archive(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "deployment"))
    from backup_archive import ArchiveWriter

    return ArchiveWriter(tmp_path / "archives", tmp_path / "keys")


def test_encrypted_archive_authenticates_before_exposing_data_and_preserves_bytes(archive):
    from backup_archive import ArchiveReader

    raw = b"private order rows and attachment bytes\x00" * 9000
    archive.add("pg.dump", [raw[:17], raw[17:]])
    archive.json("inventory.json", {"protected": ["proof-1"], "deleted": ["conversation-2"]})
    manifest = archive.seal({"consistency": "WRITERS_PAUSED_DATABASES_LOCKED"})
    assert raw[:100] not in (archive.folder / "pg.dump.enc").read_bytes()
    assert manifest["artifacts"]["pg.dump"]["plaintext_sha256"] == hashlib.sha256(raw).hexdigest()
    reader = ArchiveReader(archive.folder.parent, archive.id, archive.keys)
    assert reader.verify() == 2
    assert reader.read("pg.dump") == raw
    assert reader.json("inventory.json")["deleted"] == ["conversation-2"]


def test_modified_manifest_or_artifact_is_rejected(archive):
    from backup_archive import ArchiveReader, BackupError

    archive.add("mysql.sql", [b"INSERT INTO operation VALUES ('already succeeded');"])
    archive.seal({"execute_allowed": False})
    file = archive.folder / "manifest.json"
    original = file.read_bytes()
    value = json.loads(original)
    value["artifacts"]["mysql.sql"]["file"] = "../../original.sql"
    file.write_text(json.dumps(value))
    with pytest.raises(BackupError, match="authentication"):
        ArchiveReader(archive.folder.parent, archive.id, archive.keys)
    file.write_bytes(original)
    encrypted = archive.folder / "mysql.sql.enc"
    data = encrypted.read_bytes()
    encrypted.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    reader = ArchiveReader(archive.folder.parent, archive.id, archive.keys)
    with pytest.raises(BackupError, match="hash mismatch"):
        reader.read("mysql.sql")


def test_paths_and_duplicate_writes_cannot_escape_registered_archive(archive):
    from backup_archive import BackupError

    for name in ["../mysql", "C:/mall", "/pg", "a\\b", "a/../b"]:
        with pytest.raises(BackupError):
            archive.add(name, [b"no"])
    archive.add("pg.dump", [b"valid"])
    with pytest.raises(BackupError):
        archive.add("pg.dump", [b"replace"])
    archive.seal({})
    with pytest.raises(BackupError):
        archive.add("late.json", [b"late"])


def test_restore_result_is_signed_and_cannot_be_reassigned_or_forged(archive):
    from backup_archive import ArchiveReader, BackupError

    archive.json("credentials.json", {"rotated_password": "test-only"})
    archive.seal({"execute_allowed": False})
    reader = ArchiveReader(archive.folder.parent, archive.id, archive.keys)
    result = {"restore_id": archive.id, "result": "FAILED", "execute_replayed": 0}
    reader.record_result(result)
    assert reader.result() == result
    target = archive.folder / "result.json"
    forged = json.loads(target.read_bytes())
    forged["result"] = "PASSED"
    target.write_text(json.dumps(forged))
    with pytest.raises(BackupError, match="authentication"):
        reader.result()
    with pytest.raises(BackupError, match="ownership"):
        reader.record_result({"restore_id": "another"})


def test_snapshot_identity_uses_captured_config_and_actual_database_version(monkeypatch):
    folder = Path(__file__).resolve().parents[2] / "deployment"
    monkeypatch.syspath_prepend(str(folder))
    from backup_archive import BackupError
    from backup_data import snapshot_identity

    class Connection:
        def execute(self, query):
            assert query == "SELECT version_num FROM alembic_version"
            return self

        def fetchone(self):
            return {"version_num": "0011"}

    monkeypatch.setenv("AGENT_BUILD", "unrelated-process-build")
    assert snapshot_identity(Connection(), {"mall-agent/.env": 'AGENT_BUILD="core-functions-1.0"\n'}) == {
        "migration": "0011",
        "build": "core-functions-1.0",
    }
    for config in [{}, {"mall-agent/.env": "AGENT_BUILD="}, {"mall-agent/.env": "AGENT_BUILD=${SECRET}"}]:
        with pytest.raises(BackupError, match="build identity"):
            snapshot_identity(Connection(), config)
