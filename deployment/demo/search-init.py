"""Index the isolated demo catalog after search and its IK analyzer are ready."""

import json
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

deadline = monotonic() + 240
while True:
    try:
        with urlopen(Request("http://search:8081/esProduct/importAll", method="POST"), timeout=30) as response:
            result = json.load(response)
        if result.get("code") == 200 and result["data"] > 0:
            print(f"Indexed {result['data']} demo products")
            break
        raise RuntimeError(f"Import did not index products: {result.get('code')}")
    except (HTTPError, URLError, TimeoutError, RuntimeError) as error:
        if monotonic() >= deadline:
            raise
        print(f"Waiting for search: {type(error).__name__}")
        sleep(5)
