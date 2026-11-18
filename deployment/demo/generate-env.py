"""Print per-installation Compose settings. Run once and keep demo.env."""

from base64 import urlsafe_b64encode
from secrets import token_bytes, token_hex

for key in (
    "DEMO_MYSQL_ROOT", "DEMO_MYSQL_PASSWORD", "DEMO_POSTGRES_ROOT", "DEMO_POSTGRES_PASSWORD",
    "DEMO_REDIS_PASSWORD", "DEMO_RABBIT_PASSWORD", "DEMO_JWT", "DEMO_MINIO_PASSWORD",
    "DEMO_OBJECTS_PASSWORD", "DEMO_BRIDGE", "DEMO_STAFF_BRIDGE", "DEMO_BROKER",
    "DEMO_CURSOR", "DEMO_CONFIRM", "DEMO_RECONCILE",
    "DEMO_VECTOR_ROOT", "DEMO_VECTOR_READER", "DEMO_VECTOR_BUILDER", "DEMO_VECTOR_OBJECTS",
):
    print(f"{key}={token_hex(24)}")
print("DEMO_ENVELOPE=" + urlsafe_b64encode(token_bytes(32)).decode())
print("""
DEMO_SHOP_PORT=18180
DEMO_ADMIN_PORT=18181
DEMO_AI_PROVIDER=fixture
DEMO_AI_URL=https://api.openai.com/v1
DEMO_AI_MODEL=gpt-5-mini
DEMO_AI_KEY=
DEMO_VISION_ENABLED=false
DEMO_VISION_MODEL=gpt-5-mini
DEMO_ALI_KEY=
DEMO_KNOWLEDGE_ENABLED=false
""".strip())
