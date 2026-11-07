"""Amazon S3 implementation of the Storage protocol.

Keys are used as S3 object keys unchanged (optionally under a prefix), so
the layout in the bucket is the same as under the local data/ folder.
"""

from typing import Any

import boto3
from botocore.exceptions import ClientError

# Error codes S3 uses for "this object does not exist". HEAD requests have
# no response body, so they report the bare HTTP status instead of a name.
_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


class S3Storage:
    def __init__(self, bucket: str, prefix: str = "", client: Any = None) -> None:
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        # Credentials and region come from the environment (AWS_PROFILE,
        # ~/.aws/config, or an IAM role on AWS), never from code.
        self._client = client or boto3.client("s3")

    def _key(self, key: str) -> str:
        if ".." in key.split("/"):
            raise ValueError(f"invalid key: {key}")
        return f"{self.prefix}/{key}" if self.prefix else key

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self.bucket, Key=self._key(key))
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in _NOT_FOUND_CODES:
                return False
            raise  # e.g. AccessDenied: a real problem, not "missing"
        return True

    def read_bytes(self, key: str) -> bytes:
        response = self._client.get_object(Bucket=self.bucket, Key=self._key(key))
        return response["Body"].read()

    def write_bytes(self, key: str, data: bytes) -> None:
        """A PUT is atomic in S3: readers see the old object or the complete
        new one, never a partial upload. So, as with LocalStorage, `exists()`
        is a reliable "this step is done" check for resuming."""
        self._client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data)

    def __repr__(self) -> str:
        return f"S3Storage(s3://{self.bucket}/{self.prefix})"
