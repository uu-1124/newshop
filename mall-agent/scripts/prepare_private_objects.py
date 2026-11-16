"""Create one private agent bucket and a bucket-scoped account; preserve mall objects and policies."""

import json
import secrets
from pathlib import Path

import urllib3
from minio import Minio, MinioAdmin
from minio.credentials import StaticProvider
from minio.error import S3Error


def main():
    root = Path(__file__).resolve().parents[2]
    path = root / "deployment/.run/objects-env.json"
    original = json.loads((root / "deployment/secrets.json").read_text(encoding="utf-8-sig"))
    config = (
        json.loads(path.read_text())
        if path.exists()
        else {
            "endpoint": "127.0.0.1:19000",
            "bucket": "mall-agent-private",
            "accessKey": "agentobjects" + secrets.token_hex(6),
            "secretKey": secrets.token_hex(24),
        }
    )
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=15), retries=False)
    owner = Minio(config["endpoint"], "mallminio", original["minio"], secure=False, http_client=http)
    admin = MinioAdmin(
        endpoint=config["endpoint"],
        credentials=StaticProvider("mallminio", original["minio"]),
        secure=False,
        http_client=http,
    )
    if not owner.bucket_exists(config["bucket"]):
        owner.make_bucket(config["bucket"])
    try:
        owner.get_bucket_policy(config["bucket"])
    except S3Error as exc:
        if exc.code != "NoSuchBucketPolicy":
            raise
    else:
        raise SystemExit("Unexpected bucket policy; left untouched")
    bucket = "arn:aws:s3:::" + config["bucket"]
    admin.user_add(config["accessKey"], config["secretKey"])
    admin.policy_add(
        "mall-agent-private-objects",
        policy={
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Action": ["s3:GetBucketLocation"], "Resource": [bucket]},
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                    "Resource": [bucket + "/chat/*"],
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                    "Resource": [bucket + "/knowledge/*"],
                },
            ],
        },
    )
    admin.policy_set("mall-agent-private-objects", user=config["accessKey"])
    client = Minio(
        config["endpoint"], config["accessKey"], config["secretKey"], secure=False, http_client=http
    )
    try:
        list(client.list_objects("mall", recursive=False))
    except S3Error as exc:
        assert exc.code == "AccessDenied"
    else:
        raise AssertionError("Private account unexpectedly accessed mall bucket")
    env_path = root / "mall-agent/.env"
    updates = {
        "AGENT_OBJECTS_ENDPOINT": config["endpoint"],
        "AGENT_OBJECTS_BUCKET": config["bucket"],
        "AGENT_OBJECTS_ACCESS_KEY": config["accessKey"],
        "AGENT_OBJECTS_SECRET_KEY": config["secretKey"],
        "AGENT_ATTACHMENTS_ENABLED": "true",
    }
    lines = [
        line
        for line in env_path.read_text(encoding="utf-8").splitlines()
        if line.split("=", 1)[0] not in updates
    ]
    env_path.write_text(
        "\n".join(lines + [key + "=" + value for key, value in updates.items()]) + "\n", encoding="utf-8"
    )
    print("PASS private bucket, scoped account and mall bucket access denial; local secret config updated")


if __name__ == "__main__":
    main()
