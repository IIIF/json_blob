import base64
import json
import re

import pytest

import app
from conftest import BUCKET, PUBLIC_BASE_URL, add_code, keys_under

CODE = "correct-horse-battery-staple"
OTHER_CODE = "other-property-code"
KEY_RE = re.compile(r"^acme/\d{8}T\d{6}Z-[0-9a-f-]{36}\.json$")


@pytest.fixture
def with_code(s3):
    add_code(s3, CODE, "acme")
    add_code(s3, OTHER_CODE, "other")
    return s3


def event(body='{"id": "https://example.com", "type": "Manifest"}', headers=None, b64=False, method="POST", path="/"):
    return {
        "requestContext": {"http": {"method": method}},
        "rawPath": path,
        "headers": {"x-access-code": CODE} if headers is None else headers,
        "body": body,
        "isBase64Encoded": b64,
    }


def put_event(path, body='{"v": 2}', code=CODE):
    headers = {"x-access-code": code} if code else {}
    return event(body=body, headers=headers, method="PUT", path=path)


def read(s3, key):
    return json.loads(s3.get_object(Bucket=BUCKET, Key=f"properties/{key}")["Body"].read())


@pytest.fixture
def existing(with_code):
    """An uploaded file; returns its key (relative to properties/)."""
    resp = app.handler(event(body='{"v": 1}'), None)
    assert resp["statusCode"] == 201
    return json.loads(resp["body"])["key"]


# --- POST (create) ---------------------------------------------------------


def test_valid_code_stores_json(with_code):
    resp = app.handler(event(), None)

    assert resp["statusCode"] == 201
    result = json.loads(resp["body"])
    key = result["key"]
    assert KEY_RE.match(key)
    assert result["url"] == f"{PUBLIC_BASE_URL}/{key}"
    obj = with_code.get_object(Bucket=BUCKET, Key=f"properties/{key}")
    assert json.loads(obj["Body"].read()) == {"hello": "world"}
    assert obj["ContentType"] == "application/json"
    assert obj["CacheControl"] == "public, max-age=60"


def test_wrong_code_is_forbidden(with_code):
    resp = app.handler(event(headers={"x-access-code": "nope"}), None)

    assert resp["statusCode"] == 403
    assert keys_under(with_code, "properties/") == []


def test_missing_header_is_unauthorized(with_code):
    resp = app.handler(event(headers={}), None)

    assert resp["statusCode"] == 401
    assert keys_under(with_code, "properties/") == []


def test_header_name_is_case_insensitive(with_code):
    resp = app.handler(event(headers={"X-Access-Code": CODE}), None)

    assert resp["statusCode"] == 201


@pytest.mark.parametrize("body", ["not json", "", None])
def test_invalid_json_is_bad_request(with_code, body):
    resp = app.handler(event(body=body), None)

    assert resp["statusCode"] == 400
    assert keys_under(with_code, "properties/") == []


def test_url_omitted_without_public_base_url(with_code, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL")

    resp = app.handler(event(), None)

    assert resp["statusCode"] == 201
    assert "url" not in json.loads(resp["body"])


def test_base64_body_is_decoded(with_code):
    encoded = base64.b64encode(b'{"n": 1}').decode()

    resp = app.handler(event(body=encoded, b64=True), None)

    assert resp["statusCode"] == 201
    assert read(with_code, json.loads(resp["body"])["key"]) == {"n": 1}

def test_not_iiif(with_code):
    body = {
        "key": "not IIIF"
    }

    resp = app.handler(event(body=body), None)

    assert resp["statusCode"] == 400
    assert read(with_code, json.loads(resp["body"])["key"]) == {"n": 1}



# --- PUT (update) ----------------------------------------------------------


def test_put_replaces_existing_file(with_code, existing):
    resp = app.handler(put_event(f"/{existing}"), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {"key": existing, "url": f"{PUBLIC_BASE_URL}/{existing}"}
    assert read(with_code, existing) == {"v": 2}
    obj = with_code.head_object(Bucket=BUCKET, Key=f"properties/{existing}")
    assert obj["CacheControl"] == "public, max-age=60"
    assert obj["ContentType"] == "application/json"


def test_put_with_other_propertys_code_is_forbidden(with_code, existing):
    resp = app.handler(put_event(f"/{existing}", code=OTHER_CODE), None)

    assert resp["statusCode"] == 403
    assert read(with_code, existing) == {"v": 1}


def test_put_with_wrong_code_is_forbidden(with_code, existing):
    resp = app.handler(put_event(f"/{existing}", code="nope"), None)

    assert resp["statusCode"] == 403
    assert read(with_code, existing) == {"v": 1}


def test_put_without_code_is_unauthorized(with_code, existing):
    resp = app.handler(put_event(f"/{existing}", code=None), None)

    assert resp["statusCode"] == 401
    assert read(with_code, existing) == {"v": 1}


def test_put_to_missing_file_is_not_found(with_code):
    resp = app.handler(put_event("/acme/20260101T000000Z-made-up.json"), None)

    assert resp["statusCode"] == 404
    assert keys_under(with_code, "properties/") == []


@pytest.mark.parametrize(
    "path",
    ["/acme/../codes/x.json", "/acme/x.txt", "/acme", "/", "/acme/sub/x.json", "/Acme/x.json", "acme/x.json"],
)
def test_put_bad_path_is_not_found(with_code, existing, path):
    resp = app.handler(put_event(path), None)

    assert resp["statusCode"] == 404
    assert keys_under(with_code, "properties/") == [f"properties/{existing}"]


def test_put_invalid_json_leaves_original(with_code, existing):
    resp = app.handler(put_event(f"/{existing}", body="{nope"), None)

    assert resp["statusCode"] == 400
    assert read(with_code, existing) == {"v": 1}


@pytest.mark.parametrize("method", ["GET", "DELETE", "PATCH", ""])
def test_other_methods_not_allowed(with_code, existing, method):
    resp = app.handler(event(method=method, path=f"/{existing}"), None)

    assert resp["statusCode"] == 405
    assert read(with_code, existing) == {"v": 1}
