"""Export the running demo so it can be restored on another host.

Stops the stack first (data volumes are kept) so MySQL, PostgreSQL, MinIO and
Milvus are captured in a consistent state, then writes a dated directory with
dumps, volume archives and a manifest. Start the stack again afterwards.

    python backup.py                 # -> backups/<timestamp>/
    python backup.py --keep-running  # skip the stop/start cycle (inconsistent risk)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
VOLUMES = ["minio", "mongo", "vector-objects", "vector-index", "vector-etcd"]
ARCHIVE_IMAGE = "alpine:3.20"


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def run(args: list[str], *, capture: bool = False) -> str:
    if capture:
        result = subprocess.run(args, cwd=HERE, check=True, stdout=subprocess.PIPE)
        return result.stdout.decode("utf-8", "ignore")
    subprocess.run(args, cwd=HERE, check=True)
    return ""


def compose(env_file: str, *args: str) -> list[str]:
    return ["docker", "compose", "--env-file", env_file, *args]


def dump_mysql(env: dict[str, str], env_file: str, target: Path) -> None:
    command = (
        'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction '
        "--routines --events --triggers --default-character-set=utf8mb4 "
        "--databases mall 2>/dev/null"
    )
    result = subprocess.run(
        compose(env_file, "exec", "-T", "mysql", "sh", "-c", command),
        cwd=HERE, check=True, stdout=subprocess.PIPE,
    )
    with target.open("wb") as handle:
        import gzip
        handle.write(gzip.compress(result.stdout, 6))
    if len(result.stdout) < 1024:
        raise SystemExit("MySQL dump looks empty; check DEMO_MYSQL_ROOT in demo.env")


def dump_postgres(env: dict[str, str], env_file: str, target: Path) -> None:
    result = subprocess.run(
        compose(env_file, "exec", "-T", "postgres", "pg_dumpall", "-U", "agent_owner", "--clean"),
        cwd=HERE, check=True, stdout=subprocess.PIPE,
    )
    import gzip
    with target.open("wb") as handle:
        handle.write(gzip.compress(result.stdout, 6))
    if len(result.stdout) < 1024:
        raise SystemExit("PostgreSQL dump looks empty; check the postgres container")


def archive_volume(volume: str, target: Path) -> None:
    host_dir = str(target.parent).replace("\\", "/")
    run([
        "docker", "run", "--rm",
        "-v", f"{volume}:/data:ro",
        "-v", f"{host_dir}:/backup",
        ARCHIVE_IMAGE,
        "tar", "czf", f"/backup/{target.name}", "-C", "/data", ".",
    ])


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def env_fingerprint(env: dict[str, str]) -> str:
    """Fingerprint of the per-install secrets, used to spot a mismatched restore."""
    keys = sorted(k for k in env if k.startswith("DEMO_") and any(
        word in k for word in ("PASSWORD", "ROOT", "SECRET", "KEY", "JWT", "BRIDGE", "BROKER",
                               "CURSOR", "CONFIRM", "RECONCILE", "ENVELOPE", "READER", "BUILDER")
    ))
    joined = "".join(f"{k}={env[k]}\n" for k in keys)
    return hashlib.sha256(joined.encode()).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=HERE, check=True,
            stdout=subprocess.PIPE,
        ).stdout.decode().strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="demo.env", help="compose env file (default: demo.env)")
    parser.add_argument("--output", default=None, help="destination directory")
    parser.add_argument("--keep-running", action="store_true", help="do not stop the stack")
    options = parser.parse_args()

    if shutil.which("docker") is None:
        raise SystemExit("docker is not on PATH")
    env_path = HERE / options.env
    if not env_path.exists():
        raise SystemExit(f"{options.env} not found; run generate-env.py first")
    env = read_env(env_path)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(options.output) if options.output else HERE / "backups" / stamp
    out = out.resolve()
    out.mkdir(parents=True, exist_ok=True)

    print(f"Backing up into {out}")
    if not options.keep_running:
        print("Stopping the stack to quiesce writes (volumes are kept)")
        run(compose(options.env, "stop"))

    try:
        print("Dumping MySQL")
        dump_mysql(env, options.env, out / "mysql.sql.gz")
        print("Dumping PostgreSQL")
        dump_postgres(env, options.env, out / "postgres.sql.gz")
        for volume in VOLUMES:
            name = f"newshop-demo_{volume}"
            print(f"Archiving volume {name}")
            archive_volume(name, out / f"volume-{volume}.tar.gz")
    finally:
        if not options.keep_running:
            print("Starting the stack again")
            run(compose(options.env, "up", "-d", "--wait", "--wait-timeout", "480"))

    files = sorted(p for p in out.iterdir() if p.is_file() and p.name != "manifest.json")
    manifest = {
        "created_at": stamp,
        "git_commit": git_commit(),
        "compose_project": "newshop-demo",
        "source_env": options.env,
        "quiesced": not options.keep_running,
        "env_fingerprint": env_fingerprint(env),
        "files": {p.name: {"bytes": p.stat().st_size, "sha256": sha256(p)} for p in files},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    total = sum(item["bytes"] for item in manifest["files"].values())
    print(f"Done. {len(files)} file(s), {total / 1048576:.1f} MiB")
    print("Keep the demo.env that produced this backup: restoring encrypted action or attachment rows needs the same keys.")
    print("Copy this whole directory to the new host, then run restore.py inside deployment/demo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
