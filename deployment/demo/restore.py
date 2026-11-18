"""Restore a directory produced by backup.py into this demo deployment.

Order: stop the stack, start only the databases, load the logical dumps,
replace the object and vector volumes, then bring everything back up and
optionally run smoke.py.

    python restore.py backups/20261118T120000Z
    python restore.py backups/20261118T120000Z --skip-smoke
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from backup import env_fingerprint, read_env  # noqa: E402  (same directory helper)

ARCHIVE_IMAGE = "alpine:3.20"
VOLUMES = ["minio", "mongo", "vector-objects", "vector-index", "vector-etcd"]


def run(args: list[str], *, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(args, cwd=HERE, check=True, input=stdin, stdout=subprocess.PIPE)
    return result.stdout


def compose(env_file: str, *args: str) -> list[str]:
    return ["docker", "compose", "--env-file", env_file, *args]


def verify(manifest: dict, source: Path) -> None:
    missing = [name for name in manifest["files"] if not (source / name).exists()]
    if missing:
        raise SystemExit(f"Missing files in backup: {', '.join(missing)}")
    for name, meta in manifest["files"].items():
        digest = hashlib.sha256()
        with (source / name).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        if digest.hexdigest() != meta["sha256"]:
            raise SystemExit(f"Checksum mismatch: {name}")
    print(f"Verified {len(manifest['files'])} file(s) against manifest")


def restore_volume(source: Path, volume: str, name: str) -> None:
    host_dir = str(source).replace("\\", "/")
    run([
        "docker", "run", "--rm",
        "-v", f"newshop-demo_{volume}:/data",
        "-v", f"{host_dir}:/backup:ro",
        ARCHIVE_IMAGE,
        "sh", "-c", f"rm -rf /data/* /data/.[!.]* 2>/dev/null; tar xzf /backup/{name} -C /data",
    ])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="backup directory")
    parser.add_argument("--env", default="demo.env")
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--force", action="store_true", help="restore even without a manifest")
    options = parser.parse_args()

    source = Path(options.source).resolve()
    if not source.is_dir():
        raise SystemExit(f"{source} is not a directory")
    manifest_path = source / "manifest.json"
    if not manifest_path.exists():
        if not options.force:
            raise SystemExit("manifest.json not found; pass --force to restore anyway")
    else:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        verify(manifest, source)
        expected = manifest.get("env_fingerprint")
        env_path = HERE / options.env
        if expected and env_path.exists():
            actual = env_fingerprint(read_env(env_path))
            if actual != expected:
                print("WARNING: this demo.env uses different secrets than the backup.")
                print("         Rows encrypted or signed with the old keys will not be readable.")
                print("         Reuse the original demo.env if you need those rows.")

    print("Stopping the stack (volumes are kept)")
    run(compose(options.env, "down", "--remove-orphans"))

    print("Starting MySQL and PostgreSQL only")
    run(compose(options.env, "up", "-d", "--wait", "--wait-timeout", "300", "mysql", "postgres"))

    mysql_dump = source / "mysql.sql.gz"
    if mysql_dump.exists():
        print("Loading MySQL")
        run(
            compose(options.env, "exec", "-T", "mysql", "sh", "-c",
                    'exec mysql -uroot -p"$MYSQL_ROOT_PASSWORD" --default-character-set=utf8mb4'),
            stdin=gzip.decompress(mysql_dump.read_bytes()),
        )

    pg_dump = source / "postgres.sql.gz"
    if pg_dump.exists():
        print("Loading PostgreSQL")
        run(
            compose(options.env, "exec", "-T", "postgres", "psql", "-U", "agent_owner", "-d", "postgres", "-q"),
            stdin=gzip.decompress(pg_dump.read_bytes()),
        )

    run(compose(options.env, "stop", "mysql", "postgres"))
    for volume in VOLUMES:
        name = f"volume-{volume}.tar.gz"
        if (source / name).exists():
            print(f"Restoring volume newshop-demo_{volume}")
            restore_volume(source, volume, name)

    print("Starting the full stack")
    run(compose(options.env, "up", "-d", "--wait", "--wait-timeout", "480"))

    if not options.skip_smoke and (HERE / "smoke.py").exists():
        print("Running smoke.py")
        run([sys.executable, "-u", "smoke.py"])

    print("Restore finished. Old action and attachment rows need the same DEMO_ENVELOPE key as the backup.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
