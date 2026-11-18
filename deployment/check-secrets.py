"""Fail the build when credentials or a real host leak into tracked files.

Run from the repository root:  python deployment/check-secrets.py
Exits 0 when clean, 1 when a hit is found. Only reads Git-tracked files.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
import sys

MAX_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    ".md", ".py", ".json", ".yaml", ".yml", ".cjs", ".mjs", ".js", ".ts",
    ".vue", ".sh", ".ps1", ".sql", ".conf", ".env", ".example", ".txt", ".html", ".toml", ".ini",
}

# Hosts and domains that are placeholders or public defaults; everything else in a URL is suspect.
ALLOWED_HOST_SUFFIXES = (
    "example.com", "example.org", "example.net", "localhost",
    "127.0.0.1", "0.0.0.0", "api.openai.com", "dashscope.aliyuncs.com",
    "ghcr.io", "github.com", "registry-1.docker.io", "docker.io",
    "aliyuncs.com", "oss-cn-shenzhen.aliyuncs.com", "maven.org", "npmjs.org",
    "spring.io", "apache.org", "elastic.co", "milvus.io", "postgresql.org",
    "rabbitmq.com", "redis.io", "mongodb.com", "nacos.io", "min.io", "w3.org",
)

PATTERNS = [
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
    ("openai-style-key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("url-credentials", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@")),
]

# Known provider or infrastructure hosts that must stay out of the repository.
# Split so this file does not match its own rule when the tree is scanned.
_PROVIDER = "fusheng" + "yunsuan"
FORBIDDEN_HOSTS = re.compile(
    rf"[A-Za-z0-9.-]*(?:{_PROVIDER}|ws-[a-z0-9]{{10,}}\.cn-beijing\.maas)[A-Za-z0-9.-]*"
)
SELF = "deployment/check-secrets.py"


# Vendored third-party bundles contain unrelated numeric literals and ship unchanged.
SKIP_PATH = re.compile(r"(^|/)(node_modules|tinymce[^/]*)/|/public/tinymce|\.min\.(?:js|css)$")

# Addresses published by the upstream open-source project itself, not this deployment.
UPSTREAM_IPS = {"39.98.190.128"}


def tracked_files() -> list[str]:
    out = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout
    return [p for p in out.decode("utf-8", "ignore").split("\0") if p]


def is_candidate(path: str) -> bool:
    lowered = path.lower()
    if SKIP_PATH.search(lowered):
        return False
    if lowered.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".woff", ".woff2", ".ttf", ".jar", ".zip", ".pyc")):
        return False
    name = lowered.rsplit("/", 1)[-1]
    if name in {".env", ".dev-ssh.json", "secrets.json"}:
        return True
    suffix = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return suffix in TEXT_SUFFIXES


def public_ip(token: str) -> bool:
    try:
        addr = ipaddress.ip_address(token)
    except ValueError:
        return False
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast)


def scan(path: str) -> list[str]:
    if path == SELF:
        return []
    try:
        if not is_candidate(path):
            return []
        with open(path, "rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return []
    if len(raw) > MAX_BYTES or b"\0" in raw[:4096]:
        return []
    text = raw.decode("utf-8", "ignore")
    findings: list[str] = []
    for label, pattern in PATTERNS:
        for match in pattern.finditer(text):
            findings.append(f"{path}: {label}: {match.group(0)[:24]}...")
    for match in FORBIDDEN_HOSTS.finditer(text):
        findings.append(f"{path}: provider-host: {match.group(0)}")
    for number in re.finditer(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", text):
        token = number.group(0)
        if public_ip(token) and token not in UPSTREAM_IPS and not token.startswith(("1.1.1.1", "8.8.8.8")):
            findings.append(f"{path}: public-ip: {token}")
    return findings


def main() -> int:
    hits: list[str] = []
    for path in tracked_files():
        hits.extend(scan(path))
    if hits:
        print("Potential secrets or real hosts found in tracked files:")
        for line in hits:
            print("  " + line)
        print(f"\n{len(hits)} finding(s). Move real values into ignored local files.")
        return 1
    print("No secrets or real hosts found in tracked files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
