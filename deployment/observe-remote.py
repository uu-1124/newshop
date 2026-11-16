"""Fixed read-only host/container/Java-outbox observation. No environment or SQL rows escape."""

import json
import shutil
import subprocess

names = [
    "mall-agent-postgres",
    "mall-swarm-mysql-1",
    "mall-agent-milvus",
    "mall-swarm-minio-1",
    "mall-agent-etcd",
    "mall-agent-vector-minio",
    "mall-swarm-redis-1",
    "mall-swarm-rabbitmq-1",
    "mall-swarm-nacos-1",
    "mall-swarm-elasticsearch-1",
    "mall-swarm-mongo-1",
]
memory = {
    line.split(":", 1)[0]: int(line.split()[1]) * 1024
    for line in open("/proc/meminfo")
    if ":" in line and line.split()[1].isdigit()
}
root = (
    subprocess.check_output(["docker", "info", "--format", "{{.DockerRootDir}}"], timeout=10).decode().strip()
)
disk = shutil.disk_usage(root)
container_data = json.loads(subprocess.check_output(["docker", "inspect", *names], timeout=10))
cloud = {
    "memory_total_bytes": memory["MemTotal"],
    "memory_available_bytes": memory["MemAvailable"],
    "disk_total_bytes": disk.total,
    "disk_free_bytes": disk.free,
    "swap_used_bytes": memory["SwapTotal"] - memory["SwapFree"],
    "containers_running": sum(bool(x["State"]["Running"]) for x in container_data),
    "containers_expected": len(names),
    "containers_unhealthy": sum(
        x["State"].get("Health", {}).get("Status") == "unhealthy" for x in container_data
    ),
    "containers_oom": sum(bool(x["State"]["OOMKilled"]) for x in container_data),
    "container_restarts": sum(x["RestartCount"] for x in container_data),
}
query = """SET SESSION MAX_EXECUTION_TIME=5000; START TRANSACTION READ ONLY;
SELECT JSON_OBJECT('pending_due',count(*),'overdue_60_seconds',COALESCE(sum(due_at<UTC_TIMESTAMP(6)-INTERVAL 60 SECOND),0),
'oldest_due_seconds',COALESCE(GREATEST(0,TIMESTAMPDIFF(SECOND,min(due_at),UTC_TIMESTAMP(6))),0),
'retrying',COALESCE(sum(attempts>=3),0))
FROM mall.oms_agent_business_outbox WHERE state IN ('PENDING','PUBLISHING','SENT') AND due_at<=UTC_TIMESTAMP(6); COMMIT;"""
result = subprocess.run(
    [
        "docker",
        "exec",
        "-i",
        "mall-swarm-mysql-1",
        "sh",
        "-c",
        'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot --batch --skip-column-names',
    ],
    input=query.encode(),
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    timeout=10,
)
outbox = json.loads(result.stdout) if result.returncode == 0 else None
print(json.dumps({"cloud": cloud, "java_outbox": outbox}))
