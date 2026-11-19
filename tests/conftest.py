import json

import boto3
import pytest
from moto import mock_aws

import app

BUCKET = "test-json-blob"
PUBLIC_BASE_URL = "https://cdn.example.test"


@pytest.fixture(autouse=True)
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("BUCKET_NAME", BUCKET)
    monkeypatch.setenv("PUBLIC_BASE_URL", PUBLIC_BASE_URL)


@pytest.fixture
def s3(aws_env):
    with mock_aws():
        app.get_s3.cache_clear()
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client
        app.get_s3.cache_clear()


def add_code(s3, code, prop):
    s3.put_object(
        Bucket=BUCKET,
        Key=f"codes/{app.hash_code(code)}.json",
        Body=json.dumps({"property": prop}).encode(),
    )


def keys_under(s3, prefix):
    resp = s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix)
    return [o["Key"] for o in resp.get("Contents", [])]
