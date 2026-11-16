# Executed over the fixed SSH transport. request is injected through stdin, never a shell argument.
import json
import re
import shutil
import subprocess
from uuid import UUID

request = globals().get("request", {})  # Injected by the fixed SSH transport.


def invoke(arguments, check=True):
    result = subprocess.run(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if check and result.returncode:
        raise RuntimeError("Isolated restore container operation failed")
    return result


def inspect(kind, name):
    result = invoke(["docker", kind, "inspect", name], check=False)
    return json.loads(result.stdout)[0] if not result.returncode else None


def owned(kind, name, plan):
    value = inspect(kind, name)
    labels = (
        (value.get("Config", {}).get("Labels") if kind == "container" else value.get("Labels"))
        if value
        else {}
    )
    if value and (labels or {}).get("com.mall.agent.restore") != plan:
        raise RuntimeError("Restore resource ownership mismatch; left untouched")
    return value


def main():
    plan = str(UUID(request["plan_id"]))
    if plan != request["plan_id"]:
        raise RuntimeError("Invalid restore identifier")
    prefix = "mall-agent-r-" + plan.replace("-", "")
    label = "com.mall.agent.restore=" + plan
    network = prefix + "-network"
    services = {
        "pg": ("mall-agent-postgres", "1g", "25432:5432", "/var/lib/postgresql/data"),
        "mysql": ("mall-swarm-mysql-1", "1536m", "23306:3306", "/var/lib/mysql"),
        "objects": ("mall-swarm-minio-1", "512m", "29000:9000", "/data"),
        "etcd": ("mall-agent-etcd", "384m", None, "/etcd"),
        "vector-objects": ("mall-agent-vector-minio", "512m", None, "/data"),
        "vectors": ("mall-agent-milvus", "4g", "29530:19530", "/var/lib/milvus"),
    }
    operation = request["operation"]
    if operation == "restore-create":
        available = (
            int(next(line.split()[1] for line in open("/proc/meminfo") if line.startswith("MemAvailable:")))
            * 1024
        )
        if available < 8 * 2**30 or shutil.disk_usage("/opt").free < 8 * 2**30:
            raise RuntimeError("Insufficient headroom for an isolated restore")
        for name in ("pg_password", "mysql_password", "objects_password"):
            if not re.fullmatch(r"[a-f0-9]{48,80}", request[name]):
                raise RuntimeError("Invalid rotated restore credential")
        if not owned("network", network, plan):
            invoke(["docker", "network", "create", "--internal", "--label", label, network])
        for service, (original, memory, port, mount) in services.items():
            name, volume = prefix + "-" + service, prefix + "-" + service + "-data"
            if owned("container", name, plan):
                raise RuntimeError("Restore containers already exist; choose a new registered restore")
            if owned("volume", volume, plan):
                raise RuntimeError("Restore volume already exists; no data overwritten")
            original_value = inspect("container", original)
            if not original_value:
                raise RuntimeError("Original trusted image unavailable")
            invoke(["docker", "volume", "create", "--label", label, volume])
            args = [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "--label",
                label,
                "--network",
                network,
                "--memory",
                memory,
                "--cpus",
                "1.5",
                "--restart",
                "no",
                "--log-driver",
                "local",
                "--log-opt",
                "max-size=5m",
                "--log-opt",
                "max-file=2",
                "-v",
                volume + ":" + mount,
            ]
            # Internal networks do not publish host ports. The verifier reaches
            # container IPs exclusively through the authenticated SSH connection.
            if service == "pg":
                args += [
                    "-e",
                    "POSTGRES_USER=mall_agent_owner",
                    "-e",
                    "POSTGRES_DB=mall_agent",
                    "-e",
                    "POSTGRES_PASSWORD=" + request["pg_password"],
                ]
            elif service == "mysql":
                args += [
                    "-e",
                    "MYSQL_DATABASE=mall",
                    "-e",
                    "MYSQL_ROOT_HOST=%",
                    "-e",
                    "MYSQL_ROOT_PASSWORD=" + request["mysql_password"],
                ]
            elif service in {"objects", "vector-objects"}:
                args += [
                    "-e",
                    "MINIO_ROOT_USER=restoreroot",
                    "-e",
                    "MINIO_ROOT_PASSWORD=" + request["objects_password"],
                ]
            elif service == "vectors":
                args += [
                    "--security-opt",
                    "seccomp=unconfined",
                    "-e",
                    "ETCD_ENDPOINTS=" + prefix + "-etcd:2379",
                    "-e",
                    "MINIO_ADDRESS=" + prefix + "-vector-objects:9000",
                    "-e",
                    "MINIO_ACCESS_KEY_ID=restoreroot",
                    "-e",
                    "MINIO_SECRET_ACCESS_KEY=" + request["objects_password"],
                    "-e",
                    "MINIO_REGION=us-east-1",
                    "-e",
                    "MQ_TYPE=woodpecker",
                    "-e",
                    "COMMON_SECURITY_AUTHORIZATIONENABLED=true",
                ]
                invoke(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--user",
                        "0:0",
                        "-v",
                        volume + ":/var/lib/milvus",
                        "--entrypoint",
                        "/bin/sh",
                        original_value["Image"],
                        "-c",
                        "chown -R $(id -u milvus):$(id -g milvus) /var/lib/milvus",
                    ]
                )
            args += [original_value["Image"]]
            if service == "mysql":
                args += ["--event-scheduler=OFF", "--skip-log-bin", "--max-connections=30"]
            elif service in {"objects", "vector-objects"}:
                args += ["server", "/data", "--console-address", ":9001"]
            elif service == "etcd":
                args += [
                    "etcd",
                    "--advertise-client-urls=http://" + prefix + "-etcd:2379",
                    "--listen-client-urls=http://0.0.0.0:2379",
                    "--data-dir=/etcd",
                ]
            elif service == "vectors":
                args += ["milvus", "run", "standalone"]
            invoke(args)
        print(
            json.dumps(
                {
                    "result": "ISOLATED_CONTAINERS_CREATED",
                    "plan_id": plan,
                    "network_internal": True,
                    "applications": [],
                    "model_calls": False,
                    "execute_workers": False,
                    "consumers": False,
                }
            )
        )
    elif operation in {"restore-status", "restore-endpoints"}:
        value = owned("network", network, plan)
        if not value or not value.get("Internal"):
            raise RuntimeError("Restore network is not internal")
        containers = []
        endpoints = {}
        for service in services:
            name = prefix + "-" + service
            item = owned("container", name, plan)
            if not item:
                raise RuntimeError("Isolated restore container missing")
            if set(item["NetworkSettings"]["Networks"]) != {network}:
                raise RuntimeError("Unexpected restore network attachment")
            if item["HostConfig"]["PortBindings"]:
                raise RuntimeError("Restore services must not publish host ports")
            for bindings in (item["HostConfig"]["PortBindings"] or {}).values():
                if any(binding["HostIp"] != "127.0.0.1" for binding in bindings):
                    raise RuntimeError("Restore port is not loopback only")
            containers.append({"name": name, "running": item["State"]["Running"], "image_id": item["Image"]})
            if service in {"pg", "mysql", "objects", "vectors"}:
                endpoints[service] = {
                    "host": item["NetworkSettings"]["Networks"][network]["IPAddress"],
                    "port": {"pg": 5432, "mysql": 3306, "objects": 9000, "vectors": 19530}[service],
                }
        if operation == "restore-endpoints":
            print(json.dumps({"plan_id": plan, "endpoints": endpoints}))
            return
        print(
            json.dumps(
                {
                    "plan_id": plan,
                    "network_internal": True,
                    "containers": containers,
                    "application_count": 0,
                    "execute_workers": False,
                    "model_calls": False,
                }
            )
        )
    elif operation == "restore-destroy":
        # Every target is derived from this UUID and checked for ownership before removal.
        for service in services:
            name = prefix + "-" + service
            if owned("container", name, plan):
                invoke(["docker", "rm", "-f", name])
        for service in services:
            volume = prefix + "-" + service + "-data"
            if owned("volume", volume, plan):
                invoke(["docker", "volume", "rm", volume])
        if owned("network", network, plan):
            invoke(["docker", "network", "rm", network])
        print(json.dumps({"result": "OWNED_ISOLATED_RESOURCES_REMOVED", "plan_id": plan}))
    else:
        raise RuntimeError("Unregistered restore operation")


try:
    main()
except Exception as error:
    # Source data, environment and subprocess output never escape into ordinary logs.
    print(
        "Isolated restore operation failed: "
        + (str(error) if isinstance(error, RuntimeError) else type(error).__name__),
        file=__import__("sys").stderr,
    )
    raise SystemExit(1)
