"""Run an archived API revision on a private test port with the existing settings.

The manager, its SSH tunnels and ordinary API are not replaced. A local stop file
allows an orderly shutdown between measured phases without an HTTP control API.
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = ROOT / "deployment/.run"


def inside_run(path):
    value = Path(path).resolve()
    if not value.is_relative_to(RUN.resolve()) or value == RUN.resolve():
        raise ValueError("Benchmark source and control paths must stay in deployment/.run")
    return value


def environment():
    config = json.loads((RUN / "agent-env.json").read_text(encoding="utf-8"))
    result = {
        "AGENT_DATABASE_URL": config["databaseUrl"],
        "AGENT_BROKER_SECRET": config["brokerSecret"],
        "AGENT_BRIDGE_SECRET": config["bridgeSecret"],
        "AGENT_CURSOR_SECRET": config["cursorSecret"],
        "LANGSMITH_TRACING": "false",
        "PYTHONUTF8": "1",
    }
    path = RUN / "milvus-env.json"
    if path.exists():
        vector = json.loads(path.read_text(encoding="utf-8"))
        result.update(
            AGENT_MILVUS_DATABASE=vector["database"],
            AGENT_MILVUS_READER_USER=vector["readerUser"],
            AGENT_MILVUS_READER_PASSWORD=vector["readerPassword"],
        )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--control", required=True)
    parser.add_argument("--phase", choices=["canary", "A1", "B1", "B2", "A2"], required=True)
    parser.add_argument("--variant", choices=["A", "B"], required=True)
    parser.add_argument("--port", type=int, default=8098)
    args = parser.parse_args()
    source = inside_run(args.source)
    control = inside_run(args.control)
    if not (source / "mall-agent/app/main.py").is_file():
        raise SystemExit("Archived API source is missing")
    provenance = json.loads((source / "provenance.json").read_text(encoding="utf-8"))
    if provenance["variant"] != args.variant:
        raise SystemExit("Archived revision and selected variant differ")
    if not 8096 <= args.port <= 8098:
        raise SystemExit("Only dedicated loopback benchmark ports are allowed")
    control.mkdir(parents=True, exist_ok=True)
    stop = control / (args.phase + ".stop")
    if stop.exists():
        raise SystemExit("Stop marker already exists; use a fresh phase control directory")
    os.environ.update(environment())
    os.environ["AGENT_PERFORMANCE_TIMING_ENABLED"] = "true" if args.variant == "B" else "false"
    # Read the exact same existing dotenv configuration for both archived revisions.
    os.chdir(ROOT / "mall-agent")
    sys.path.insert(0, str(source / "mall-agent"))
    import uvicorn
    from app.runtime import run

    config = uvicorn.Config(
        "app.main:create_app",
        factory=True,
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
        timeout_keep_alive=15,
    )
    server = uvicorn.Server(config)
    state = {
        "schema_version": 1,
        "pid": os.getpid(),
        "phase": args.phase,
        "variant": args.variant,
        "commit": provenance["commit"],
        "source": str(source),
        "port": args.port,
    }
    (control / (args.phase + ".process.json")).write_text(json.dumps(state) + "\n", encoding="utf-8")

    async def serve():
        async def stopping():
            while not stop.exists():
                await asyncio.sleep(0.5)
            server.should_exit = True

        monitor = asyncio.create_task(stopping())
        try:
            await server.serve()
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)

    run(serve())


if __name__ == "__main__":
    main()
