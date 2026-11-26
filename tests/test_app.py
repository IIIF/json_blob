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


def put_event(path, body='{"id": "https://example.com", "type": "Manifest"}', code=CODE):
    headers = {"x-access-code": code} if code else {}
    return event(body=body, headers=headers, method="PUT", path=path)


class FakeCloudFront:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def create_invalidation(self, **kwargs):
        self.calls.append(kwargs)
        if self.fail:
            raise RuntimeError("boom")
        return {"Invalidation": {"Id": "I1", "Status": "InProgress"}}


@pytest.fixture
def cloudfront(monkeypatch):
    fake = FakeCloudFront()
    monkeypatch.setattr(app, "get_cloudfront", lambda: fake)
    return fake


def via_cloudfront(ev, distribution_id="E2TEST123"):
    ev["headers"]["x-distribution-id"] = distribution_id
    return ev


def read(s3, key):
    return json.loads(s3.get_object(Bucket=BUCKET, Key=f"properties/{key}")["Body"].read())


@pytest.fixture
def existing(with_code):
    """An uploaded file; returns its key (relative to properties/)."""
    resp = app.handler(event(body='{"id": "https://example.com", "type": "Manifest"}'), None)
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
    assert json.loads(obj["Body"].read()) == {"id": "https://example.com", "type": "Manifest"}
    assert obj["ContentType"] == "application/json"
    assert obj["CacheControl"] == app.CACHE_CONTROL == "public, max-age=0, s-maxage=86400"


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


def test_url_from_cloudfront_host_without_public_base_url(with_code, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL")

    resp = app.handler(event(headers={"x-access-code": CODE, "x-public-host": "D111.cloudfront.net"}), None)

    assert resp["statusCode"] == 201
    result = json.loads(resp["body"])
    assert result["url"] == f"https://d111.cloudfront.net/{result['key']}"


def test_public_base_url_wins_over_cloudfront_host(with_code):
    resp = app.handler(event(headers={"x-access-code": CODE, "x-public-host": "d111.cloudfront.net"}), None)

    result = json.loads(resp["body"])
    assert result["url"] == f"{PUBLIC_BASE_URL}/{result['key']}"


@pytest.mark.parametrize("host", ["", "evil.com/x", "a b.com", "localhost", "x.com:8080", "-x.com"])
def test_invalid_cloudfront_host_is_ignored(with_code, monkeypatch, host):
    monkeypatch.delenv("PUBLIC_BASE_URL")

    resp = app.handler(event(headers={"x-access-code": CODE, "x-public-host": host}), None)

    assert resp["statusCode"] == 201
    assert "url" not in json.loads(resp["body"])


@pytest.mark.parametrize("path", ["/", "/ai2026", "/other/thing.json", ""])
def test_post_path_is_ignored(with_code, path):
    resp = app.handler(event(path=path), None)

    assert resp["statusCode"] == 201
    assert KEY_RE.match(json.loads(resp["body"])["key"])


def test_base64_body_is_decoded(with_code):
    manifest = {"id": "https://example.com","type": "Manifest"}
    encoded = base64.b64encode(json.dumps(manifest).encode("utf-8")).decode("ascii")

    resp = app.handler(event(body=encoded, b64=True), None)

    assert resp["statusCode"] == 201
    assert read(with_code, json.loads(resp["body"])["key"]) == manifest

def test_not_iiif(with_code):
    body = {
        "key": "not IIIF"
    }

    resp = app.handler(event(body=json.dumps(body)), None)

    assert resp["statusCode"] == 400

# --- PUT (update) ----------------------------------------------------------


def test_put_replaces_existing_file(with_code, existing):
    resp = app.handler(put_event(f"/{existing}"), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"]) == {
        "key": existing,
        "url": f"{PUBLIC_BASE_URL}/{existing}",
        "invalidated": False,  # direct call: no distribution ID header
    }
    assert read(with_code, existing) == {"id": "https://example.com","type": "Manifest"}
    obj = with_code.head_object(Bucket=BUCKET, Key=f"properties/{existing}")
    assert obj["CacheControl"] == app.CACHE_CONTROL == "public, max-age=0, s-maxage=86400"
    assert obj["ContentType"] == "application/json"


def test_put_url_from_cloudfront_host(with_code, existing, monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL")
    ev = put_event(f"/{existing}")
    ev["headers"]["x-public-host"] = "d111.cloudfront.net"

    resp = app.handler(ev, None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["url"] == f"https://d111.cloudfront.net/{existing}"


def test_put_via_cloudfront_invalidates_cached_copy(with_code, existing, cloudfront):
    resp = app.handler(via_cloudfront(put_event(f"/{existing}")), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["invalidated"] is True
    assert len(cloudfront.calls) == 1
    call = cloudfront.calls[0]
    assert call["DistributionId"] == "E2TEST123"
    assert call["InvalidationBatch"]["Paths"] == {"Quantity": 1, "Items": [f"/{existing}"]}
    assert call["InvalidationBatch"]["CallerReference"]


def test_put_invalidation_failure_still_succeeds(with_code, existing, monkeypatch):
    fake = FakeCloudFront(fail=True)
    monkeypatch.setattr(app, "get_cloudfront", lambda: fake)

    resp = app.handler(via_cloudfront(put_event(f"/{existing}", body='{"id": "https://new", "type": "Manifest"}')), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["invalidated"] is False
    assert len(fake.calls) == 1
    assert read(with_code, existing) == {"id": "https://new", "type": "Manifest"}


@pytest.mark.parametrize("distribution_id", ["", "e2lower", "E2/../x", "E" * 33])
def test_put_with_invalid_distribution_id_does_not_invalidate(with_code, existing, cloudfront, distribution_id):
    resp = app.handler(via_cloudfront(put_event(f"/{existing}"), distribution_id), None)

    assert resp["statusCode"] == 200
    assert json.loads(resp["body"])["invalidated"] is False
    assert cloudfront.calls == []


def test_put_without_distribution_id_does_not_invalidate(with_code, existing, cloudfront):
    resp = app.handler(put_event(f"/{existing}"), None)

    assert json.loads(resp["body"])["invalidated"] is False
    assert cloudfront.calls == []


@pytest.mark.parametrize(
    "make_event",
    [
        lambda key: put_event(f"/{key}", code=OTHER_CODE),          # 403
        lambda key: put_event("/acme/20260101T000000Z-x.json"),     # 404
        lambda key: put_event(f"/{key}", body="{nope"),             # 400
        lambda key: event(),                                        # POST: new file, nothing cached
    ],
)
def test_no_invalidation_unless_put_succeeds(with_code, existing, cloudfront, make_event):
    app.handler(via_cloudfront(make_event(existing)), None)

    assert cloudfront.calls == []


def test_put_with_other_propertys_code_is_forbidden(with_code, existing):
    resp = app.handler(put_event(f"/{existing}", code=OTHER_CODE), None)

    assert resp["statusCode"] == 403
    assert read(with_code, existing) == {"id": "https://example.com","type": "Manifest"}


def test_put_with_wrong_code_is_forbidden(with_code, existing):
    resp = app.handler(put_event(f"/{existing}", code="nope"), None)

    assert resp["statusCode"] == 403
    assert read(with_code, existing) == {"id": "https://example.com","type": "Manifest"}


def test_put_without_code_is_unauthorized(with_code, existing):
    resp = app.handler(put_event(f"/{existing}", code=None), None)

    assert resp["statusCode"] == 401
    assert read(with_code, existing) == {"id": "https://example.com","type": "Manifest"}


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
    assert read(with_code, existing) == {"id": "https://example.com","type": "Manifest"}


@pytest.mark.parametrize("method", ["GET", "DELETE", "PATCH", ""])
def test_other_methods_not_allowed(with_code, existing, method):
    resp = app.handler(event(method=method, path=f"/{existing}"), None)

    assert resp["statusCode"] == 405
    assert read(with_code, existing) == {"id": "https://example.com","type": "Manifest"}