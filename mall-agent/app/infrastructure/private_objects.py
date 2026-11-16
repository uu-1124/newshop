import io

import urllib3
from minio import Minio

from app.domain.identity import DomainError


class PrivateObjects:
    def __init__(self, settings):
        self.bucket = settings.objects_bucket
        self.http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=5, read=15), retries=False)
        self.client = Minio(
            settings.objects_endpoint,
            settings.objects_access_key.get_secret_value(),
            settings.objects_secret_key.get_secret_value(),
            secure=False,
            http_client=self.http,
        )

    def put(self, key, content, content_type="image/jpeg"):
        try:
            self.client.put_object(
                self.bucket, key, io.BytesIO(content), len(content), content_type=content_type
            )
        except Exception:
            raise DomainError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件存储暂不可用", 503) from None

    def read(self, key, max_bytes=5 * 1024 * 1024):
        response = None
        try:
            response = self.client.get_object(self.bucket, key)
            result = response.read(max_bytes + 1)
            if len(result) > max_bytes:
                raise ValueError("Stored object too large")
            return result
        except Exception:
            raise DomainError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件读取暂不可用", 503) from None
        finally:
            if response:
                response.close()
                response.release_conn()

    def close(self):
        self.http.clear()

    def delete(self, key):
        try:
            self.client.remove_object(self.bucket, key)
        except Exception:
            raise DomainError("ATTACHMENT_STORAGE_UNAVAILABLE", "附件删除暂不可用", 503) from None
