"""Initialize the demo vector index without calling an external model."""

import logging
import os

from pymilvus import MilvusClient
from pymilvus.exceptions import MilvusException

logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
options = {"uri": "http://milvus:19530", "user": "root", "timeout": 15}
root_password = os.environ["DEMO_VECTOR_ROOT"]
try:
    client = MilvusClient(**options, password=root_password)
    client.list_databases()
except MilvusException:
    client = MilvusClient(**options, password="Milvus")
    client.update_password("root", "Milvus", root_password)
    client.close()
    client = MilvusClient(**options, password=root_password)

try:
    if "mall_agent" not in client.list_databases():
        client.create_database("mall_agent")
    for role, groups in {
        "reader": ("DatabaseReadOnly", "CollectionReadOnly"),
        "builder": ("DatabaseAdmin", "CollectionReadWrite"),
    }.items():
        name = f"mall_agent_{role}"
        if name not in client.list_users():
            client.create_user(name, os.environ[f"DEMO_VECTOR_{role.upper()}"])
        if name not in client.list_roles():
            client.create_role(name)
        for group in groups:
            client.grant_privilege_v2(name, group, "*", db_name="mall_agent")
        client.grant_role(name, name)
finally:
    client.close()
