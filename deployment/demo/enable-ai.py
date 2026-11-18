"""Turn on a real model for an existing demo.env without hand-editing it.

    python enable-ai.py --url https://your-endpoint/v1 --model gpt-5-mini --key sk-...
    python enable-ai.py --url ... --key ... --vision --knowledge --ali-key sk-...

Writes demo.env in place with owner-only permissions and prints the start
command that matches the selected options. Nothing is sent to the provider.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV_PATH = HERE / "demo.env"
ALLOWED_HOST = re.compile(r"^https://[A-Za-z0-9.-]+(?::443)?(/[A-Za-z0-9._~/-]*)?$")

MANAGED = (
    "DEMO_AI_PROVIDER", "DEMO_AI_URL", "DEMO_AI_MODEL", "DEMO_AI_KEY",
    "DEMO_VISION_ENABLED", "DEMO_VISION_MODEL", "DEMO_ALI_KEY", "DEMO_KNOWLEDGE_ENABLED",
)


def parse(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def apply(text: str, updates: dict[str, str]) -> str:
    lines = text.splitlines()
    seen: set[str] = set()
    for index, line in enumerate(lines):
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key = line.split("=", 1)[0].strip()
        if key in updates:
            lines[index] = f"{key}={updates[key]}"
            seen.add(key)
    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="HTTPS base URL of the chat endpoint")
    parser.add_argument("--model", default="gpt-5-mini", help="model name served by that endpoint")
    parser.add_argument("--key", required=True, help="API key for the chat endpoint")
    parser.add_argument("--provider", default="openai_responses", choices=["openai_responses"])
    parser.add_argument("--vision", action="store_true", help="also enable image questions")
    parser.add_argument("--vision-model", default=None, help="model used for vision (default: --model)")
    parser.add_argument("--knowledge", action="store_true", help="enable Milvus knowledge retrieval")
    parser.add_argument("--ali-key", default=None, help="Aliyun workspace key for embeddings and rerank")
    parser.add_argument("--env", default="demo.env")
    options = parser.parse_args()

    env_path = HERE / options.env
    if not env_path.exists():
        raise SystemExit(f"{options.env} not found; run generate-env.py first")
    if not ALLOWED_HOST.match(options.url.rstrip("/")):
        raise SystemExit("--url must be a plain HTTPS base URL without credentials or query string")
    if options.knowledge and not options.ali_key:
        print("NOTE: --knowledge also needs --ali-key for embeddings and rerank.", file=sys.stderr)

    updates = {
        "DEMO_AI_PROVIDER": options.provider,
        "DEMO_AI_URL": options.url.rstrip("/"),
        "DEMO_AI_MODEL": options.model,
        "DEMO_AI_KEY": options.key,
        "DEMO_VISION_ENABLED": "true" if options.vision else "false",
        "DEMO_VISION_MODEL": options.vision_model or options.model,
        "DEMO_KNOWLEDGE_ENABLED": "true" if options.knowledge else "false",
    }
    if options.ali_key:
        updates["DEMO_ALI_KEY"] = options.ali_key

    env_path.write_text(apply(env_path.read_text(encoding="utf-8"), updates), encoding="utf-8")
    os.chmod(env_path, 0o600)

    profile = " --profile knowledge" if options.knowledge else ""
    name = Path(options.env).name
    print(f"Updated {env_path.name}")
    print("Start or restart with:")
    if options.knowledge:
        print(f"  docker compose --env-file {name}{profile} up -d vector-init")
        print(f"  docker compose --env-file {name}{profile} wait vector-init")
    print(f"  docker compose --env-file {name}{profile} up -d --wait --wait-timeout 480")
    print("Keep this file out of Git; it now holds a real key.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
