"""S3-compatible (MinIO/S3) adapter that signs download URLs."""

from __future__ import annotations

import os

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from document.domain.model.document import StorageUnavailableError


class S3ObjectStorage:
    def __init__(
        self,
        *,
        endpoint_url: str | None = None,
        bucket: str | None = None,
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str | None = None,
    ) -> None:
        self._endpoint_url = endpoint_url or os.getenv("STORAGE_ENDPOINT")
        self._bucket = bucket or os.getenv("STORAGE_BUCKET")
        self._access_key = access_key or os.getenv("STORAGE_ACCESS_KEY")
        self._secret_key = secret_key or os.getenv("STORAGE_SECRET_KEY")
        self._region = region or os.getenv("STORAGE_REGION", "us-east-1")
        self._client = None

    def _s3(self):
        if self._client is None:
            if not (self._endpoint_url and self._bucket and self._access_key and self._secret_key):
                raise StorageUnavailableError(
                    "STORAGE_ENDPOINT, STORAGE_BUCKET, STORAGE_ACCESS_KEY y STORAGE_SECRET_KEY deben configurarse"
                )
            self._client = boto3.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )
        return self._client

    def put(self, storage_key: str, content: bytes, content_type: str) -> None:
        try:
            self._s3().put_object(Bucket=self._bucket, Key=storage_key, Body=content, ContentType=content_type)
        except (BotoCoreError, ClientError) as error:
            raise StorageUnavailableError("No fue posible subir el archivo al object storage") from error

    def presign_download(self, storage_key: str, expires_in_seconds: int) -> str:
        try:
            return self._s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": storage_key},
                ExpiresIn=expires_in_seconds,
            )
        except (BotoCoreError, ClientError) as error:
            raise StorageUnavailableError("No fue posible firmar la URL de descarga") from error
