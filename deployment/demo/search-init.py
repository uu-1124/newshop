"""Index the isolated demo catalog after search and its IK analyzer are ready."""

import json
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

search = "http://search:8081"
elastic = "http://elasticsearch:9200"
deadline = monotonic() + 180
while True:
    try:
        with urlopen(search + "/actuator/health", timeout=5) as response:
            if json.load(response)["status"] == "UP":
                break
    except (HTTPError, URLError, TimeoutError) as error:
        if monotonic() >= deadline:
            raise
        print(f"Waiting for search: {error}")
    if monotonic() >= deadline:
        raise TimeoutError("Search service is not healthy")
    sleep(5)

analyze = Request(
    elastic + "/_analyze",
    data=json.dumps({"analyzer": "ik_max_word", "text": "小米手机"}).encode(),
    headers={"Content-Type": "application/json"},
)
with urlopen(analyze, timeout=60) as response:
    if not json.load(response)["tokens"]:
        raise RuntimeError("Chinese analyzer returned no tokens")
print("Chinese analyzer responded; importing demo products")
with urlopen(Request(search + "/esProduct/importAll", data=b"", method="POST"), timeout=190) as response:
    result = json.load(response)
if result.get("code") != 200 or result["data"] <= 0:
    raise RuntimeError(f"Import did not index products: {result.get('code')}")
print(f"Indexed {result['data']} demo products")
