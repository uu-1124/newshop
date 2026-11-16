"""Read-only verification of this release's sources and optional local evidence."""

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--with-local-evidence", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads(
        (root / "deployment/conservative-fixes-manifest.json").read_text(
            encoding="utf-8"
        )
    )
    failures = []
    for name, expected in manifest["source_sha256_lf"].items():
        path = root / name
        if not path.is_file():
            failures.append(name + ": missing")
            continue
        content = path.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(content).hexdigest() != expected:
            failures.append(name + ": source differs")
    if args.with_local_evidence:
        artifacts = (
            manifest["local_evidence_sha256"]
            | manifest["runtime_and_rollback_jars_sha256"]
        )
        for name, expected in artifacts.items():
            path = root / name
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                failures.append(name + ": local evidence missing or differs")
    baseline = subprocess.run(
        ["git", "merge-base", "--is-ancestor", manifest["baseline_commit"], "HEAD"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    if baseline.returncode:
        failures.append("Git baseline is not reachable from HEAD")
    if failures:
        raise SystemExit("FAIL\n" + "\n".join(failures))
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(
        json.dumps(
            {
                "result": "passed",
                "head": revision,
                "sources": len(manifest["source_sha256_lf"]),
                "local_evidence_checked": args.with_local_evidence,
                "note": "Hashes bind retained evidence; this does not rerun tests or probe services.",
            }
        )
    )


if __name__ == "__main__":
    main()
