"""Exercise the default fixture deployment via the same front-door URLs as a browser."""

import base64
import json
import sys
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from uuid import uuid4

shop, admin = sys.argv[1:] or ["http://127.0.0.1:18180", "http://127.0.0.1:18181"]
password = "NewshopDemo!2026"
member_api, staff_api = "/mall-agent/v1/member", "/mall-agent/v1/admin"


def request(base, path, data=None, token=None, *, method=None, content_type="application/json"):
    headers = {"Idempotency-Key": str(uuid4())}
    if token:
        headers["Authorization"] = token
    if data is not None:
        headers["Content-Type"] = content_type
        if not isinstance(data, bytes):
            data = json.dumps(data).encode()
    req = Request(base + path, data, headers=headers, method=method)
    return urlopen(req, timeout=30)


def api(base, path, data=None, token=None, **options):
    try:
        with request(base, path, data, token, **options) as response:
            result = json.load(response)
    except HTTPError as error:
        raise RuntimeError(f"{path}: HTTP {error.code}") from None
    if result["code"] not in (200, "OK"):
        raise RuntimeError(f"{path}: {result['code']} {result.get('message')}")
    return result["data"]


def auth(result):
    return f"{result['tokenHead'].strip()} {result['token']}"


def ready(check):
    deadline = monotonic() + 240
    while True:
        try:
            return check()
        except (URLError, TimeoutError, RuntimeError) as error:
            if monotonic() >= deadline:
                raise
            print(f"Waiting for Java registration/login: {error}")
            sleep(5)


for base in (shop, admin):
    with request(base, "/") as page:
        assert page.headers.get_content_type() == "text/html" and b"<script" in page.read()
staff = auth(ready(lambda: api(admin, "/mall-admin/admin/login", {"username": "demo-admin", "password": password})))
buyer = auth(ready(lambda: api(
    shop, "/mall-portal/sso/login", urlencode({"username": "demo-buyer", "password": password}).encode(),
    content_type="application/x-www-form-urlencoded",
)))
menus = api(admin, "/mall-admin/admin/info", token=staff)["menus"]
assert {"agent", "agentService", "agentKnowledge"} <= {menu["name"] for menu in menus}
assert api(shop, "/mall-portal/member/address/list", token=buyer)
print("PASS both frontend entry points, both logins, staff menus and demo address")

detail = api(shop, "/mall-portal/product/detail/26")
with urlopen(urljoin(shop, detail["product"]["pic"]), timeout=15) as image:
    assert image.headers.get_content_type().startswith("image/") and image.read(32)
result = api(shop, "/mall-search/esProduct/search?" + urlencode({"keyword": "小米", "pageNum": 1, "pageSize": 5}))
if isinstance(result, list):
    assert result
else:
    assert result["total"] > 0
print("PASS catalog, bundled product image and Chinese search")

boundary = "DemoFile" + uuid4().hex
file_name = "smoke-" + uuid4().hex + ".png"
image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aRz8AAAAASUVORK5CYII=")
payload = (
    f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{file_name}"\r\n'
    "Content-Type: image/png\r\n\r\n"
).encode() + image + f"\r\n--{boundary}--\r\n".encode()
uploaded = api(admin, "/mall-admin/minio/upload", payload, staff, content_type=f"multipart/form-data; boundary={boundary}")
assert uploaded["url"].startswith("/mall/")
try:
    for base in (shop, admin):
        with request(base, uploaded["url"]) as downloaded:
            assert downloaded.read() == image
finally:
    api(admin, "/mall-admin/minio/delete?" + urlencode({"objectName": uploaded["url"].removeprefix("/mall/")}), b"", staff)
print("PASS image upload, download from both sites and cleanup")

assert api(shop, member_api + "/me", token=buyer)["id"] == "1"
conversation = api(shop, member_api + "/conversations", {
    "title": "独立演示验收", "entry_context": {"kind": "PRODUCT", "product_id": "26"},
}, buyer)
path = member_api + "/conversations/" + conversation["id"]
api(shop, path + "/messages", {"client_message_id": str(uuid4()), "text": "介绍这件商品", "attachment_ids": []}, buyer)
with request(shop, path + "/events", token=buyer) as response:
    assert response.headers.get_content_type() == "text/event-stream"
    deadline = monotonic() + 60
    seen = set()
    for raw in response:
        if raw.startswith(b"event:"):
            seen.add(raw.split(b":", 1)[1].strip())
        if b"message.completed" in seen:
            break
        if monotonic() >= deadline:
            break
    assert {b"message.delta", b"message.completed"} <= seen, seen
print("PASS fixture conversation and streaming reply through the gateway")

latest = api(shop, path, token=buyer)["conversation"]
handoff = api(shop, path + "/handoff", {"expected_conversation_version": latest["version"], "reason": "演示接待"}, buyer)
entries = api(admin, staff_api + "/handoffs", token=staff)["items"]
assert any(item["id"] == handoff["id"] for item in entries)
print("PASS buyer requests human support and the staff workbench sees the ticket")
