"""Attach aggregate CPU/SSH and local-memory observations to a registered capacity run."""

import argparse
import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]


def main(run_id):
    run_id = str(UUID(run_id))
    source = ROOT / "deployment/.run" / ("capacity-" + run_id + ".json")
    target = source.with_name(source.stem + "-resources.json")
    result = {
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "scope": "Aggregate CPU, SSH and local memory. First CPU delta requires two samples; no earlier samples are inferred.",
        "samples": [],
        "errors": [],
    }
    previous = None
    started = time.monotonic()
    while time.monotonic() - started < 1800:
        state = json.loads(source.read_text(encoding="utf-8"))
        if state["result"] not in {"PREPARING", "RUNNING"}:
            result["capacity_result"] = state["result"]
            break
        try:
            process = subprocess.run(
                ["D:/develop/nodejs/node.exe", str(ROOT / "deployment/observe-capacity-remote.cjs")],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=25,
                creationflags=0x08000000,
            )
            if process.returncode or len(process.stdout) > 16384:
                raise ValueError("CPU observation unavailable")
            sample = json.loads(process.stdout)
            sample["checked_at"] = datetime.now(UTC).isoformat()
            sample["load_started_at"] = state.get("load_started_at")
            for key in ["cloud_cpu", "local_cpu"]:
                sample[key]["usage_percent"] = None
                sample[key]["steal_percent"] = None
                if previous:
                    before, after = previous[key]["ticks"], sample[key]["ticks"]
                    total = sum(after.values()) - sum(before.values())
                    idle = after["idle"] - before["idle"]
                    if key == "cloud_cpu":
                        idle += after.get("iowait", 0) - before.get("iowait", 0)
                    if total > 0:
                        sample[key]["usage_percent"] = round(100 * (total - idle) / total, 2)
                        if key == "cloud_cpu":
                            sample[key]["steal_percent"] = round(
                                100 * (after["steal"] - before["steal"]) / total, 2
                            )
            result["samples"].append(sample)
            previous = sample
        except Exception as error:
            result["errors"].append(
                {"checked_at": datetime.now(UTC).isoformat(), "type": type(error).__name__}
            )
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        time.sleep(15)
    result["finished_at"] = datetime.now(UTC).isoformat()
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "CAPACITY resource samples=" + str(len(result["samples"])) + " errors=" + str(len(result["errors"]))
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("run_id")
    main(parser.parse_args().run_id)
