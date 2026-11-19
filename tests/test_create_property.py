import json

import pytest

import app
import create_property as cp
from conftest import BUCKET, keys_under


def upload(code):
    return app.handler(
        {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {"x-access-code": code},
            "body": '{"ok": true}',
        },
        None,
    )


def test_creates_marker_mapping_and_registry(s3):
    code = cp.create_property(s3, BUCKET, "acme")

    code_hash = cp.hash_code(code)
    assert "properties/acme/" in keys_under(s3, "properties/")
    mapping = json.loads(s3.get_object(Bucket=BUCKET, Key=f"codes/{code_hash}.json")["Body"].read())
    assert mapping == {"property": "acme"}
    registry = json.loads(s3.get_object(Bucket=BUCKET, Key="registry/acme.json")["Body"].read())
    assert registry == {"code_hash": code_hash}
    assert code not in json.dumps(mapping) + json.dumps(registry)


def test_created_code_works_end_to_end(s3):
    code = cp.create_property(s3, BUCKET, "acme")

    resp = upload(code)

    assert resp["statusCode"] == 201
    key = json.loads(resp["body"])["key"]
    assert key.startswith("acme/")
    assert f"properties/{key}" in keys_under(s3, "properties/acme/")


def test_refuses_existing_property(s3):
    cp.create_property(s3, BUCKET, "acme")

    with pytest.raises(cp.PropertyError, match="already exists"):
        cp.create_property(s3, BUCKET, "acme")


def test_refuses_existing_property_after_marker_expires(s3):
    cp.create_property(s3, BUCKET, "acme")
    s3.delete_object(Bucket=BUCKET, Key="properties/acme/")  # simulate lifecycle expiry

    with pytest.raises(cp.PropertyError, match="already exists"):
        cp.create_property(s3, BUCKET, "acme")


def test_rotate_replaces_code(s3):
    old = cp.create_property(s3, BUCKET, "acme")

    new = cp.create_property(s3, BUCKET, "acme", rotate=True)

    assert new != old
    assert upload(old)["statusCode"] == 403
    assert upload(new)["statusCode"] == 201
    assert keys_under(s3, "codes/") == [f"codes/{cp.hash_code(new)}.json"]


def test_rotate_requires_existing_property(s3):
    with pytest.raises(cp.PropertyError, match="does not exist"):
        cp.create_property(s3, BUCKET, "acme", rotate=True)


@pytest.mark.parametrize("name", ["../etc", "Acme", "", "a/b", "-lead", "x" * 64])
def test_rejects_invalid_names(s3, name):
    with pytest.raises(cp.PropertyError, match="invalid property name"):
        cp.create_property(s3, BUCKET, name)


def test_main_prints_code_and_exits_nonzero_on_duplicate(s3, monkeypatch, capsys):
    monkeypatch.setattr(cp.boto3, "client", lambda *_a, **_k: s3)

    assert cp.main(["acme", "--bucket", BUCKET]) == 0
    out = capsys.readouterr().out
    code = out.split("Access code: ")[1].split()[0]
    assert upload(code)["statusCode"] == 201

    assert cp.main(["acme", "--bucket", BUCKET]) == 1
    assert "already exists" in capsys.readouterr().err
