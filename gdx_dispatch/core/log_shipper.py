from __future__ import annotations

import gzip
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger("gdx_dispatch.log_shipper")


class LogShipper:
    def __init__(
        self,
        *,
        target: str | None = None,
        retention_days: int | None = None,
        s3_bucket: str | None = None,
        aws_region: str | None = None,
    ) -> None:
        self.target = (target or os.getenv("LOG_SHIP_TARGET", "stdout")).strip().lower()
        self.retention_days = int(retention_days or os.getenv("LOG_RETENTION_DAYS", "90"))
        self.s3_bucket = (s3_bucket if s3_bucket is not None else os.getenv("LOG_S3_BUCKET", "")).strip()
        self.aws_region = (aws_region if aws_region is not None else os.getenv("AWS_REGION", "us-east-1")).strip()

    def ship(self, entries: list[dict[str, Any]]) -> bool:
        if not entries:
            return True
        if self.target == "stdout":
            return self._ship_stdout(entries)
        if self.target == "s3":
            return self._ship_s3(entries)
        if self.target == "cloudwatch":
            return self._ship_cloudwatch(entries)
        log.warning("Unknown LOG_SHIP_TARGET=%s", self.target)
        return False

    def _ship_stdout(self, entries: list[dict[str, Any]]) -> bool:
        for row in entries:
            print(json.dumps(row, sort_keys=True))
        return True

    def _ship_s3(self, entries: list[dict[str, Any]]) -> bool:
        if not self.s3_bucket:
            log.warning("S3 shipping skipped: LOG_S3_BUCKET not configured")
            return False

        key = datetime.now(UTC).strftime("logs/%Y/%m/%d/%H/gdx-logs.json.gz")
        body = "\n".join(json.dumps(e, sort_keys=True) for e in entries).encode("utf-8")
        payload = gzip.compress(body)
        try:
            import boto3  # type: ignore

            client = boto3.client("s3", region_name=self.aws_region)
            client.put_object(
                Bucket=self.s3_bucket,
                Key=key,
                Body=payload,
                ContentType="application/json",
                ContentEncoding="gzip",
            )
            return True
        except Exception as exc:
            log.exception("_ship_s3_failed")
            log.warning("S3 shipping failed: %s", exc)
            return False

    def _ship_cloudwatch(self, entries: list[dict[str, Any]]) -> bool:
        try:
            import boto3  # type: ignore

            client = boto3.client("logs", region_name=self.aws_region)
            group_name = os.getenv("LOG_CLOUDWATCH_GROUP", "gdx-logs")
            stream_name = datetime.now(UTC).strftime("%Y-%m-%d")
            try:
                client.create_log_group(logGroupName=group_name)
            except Exception:
                log.exception("_ship_cloudwatch_failed")
                pass
            try:
                client.create_log_stream(logGroupName=group_name, logStreamName=stream_name)
            except Exception:
                log.exception("_ship_cloudwatch_failed")
                pass
            events = [
                {"timestamp": int(datetime.now(UTC).timestamp() * 1000), "message": json.dumps(row, sort_keys=True)}
                for row in entries
            ]
            client.put_log_events(logGroupName=group_name, logStreamName=stream_name, logEvents=events)
            return True
        except Exception as exc:
            log.exception("_ship_cloudwatch_failed")
            log.warning("CloudWatch shipping failed: %s", exc)
            return False
