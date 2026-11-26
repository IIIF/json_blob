"""Lambda handler: store JSON in S3 under the property its access code maps to.

POST (any path; via CloudFront or the function URL)
                                    creates properties/<property>/<timestamp>-<uuid>.json
PUT  /<property>/<file>.json        replaces an existing file (routed here by CloudFront)

Responses include "url" when PUBLIC_BASE_URL is set (custom domain) or, failing that,
when CloudFront's router function passed the viewer's host in x-public-host.
"""

import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import boto3
from botocore.exceptions import ClientError

CODES_PREFIX = "codes/"
PROPERTIES_PREFIX = "properties/"
ACCESS_CODE_HEADER = "x-access-code"
# Set by CloudFront's router function to the host the viewer called.
PUBLIC_HOST_HEADER = "x-public-host"
HOSTNAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")
# Browsers revalidate every time (cheap 304s); CloudFront keeps a copy for up to a day.
# PUT invalidates CloudFront's copy, so updates are visible at once. The day-long limit
# bounds how long an expired (deleted) upload can still be served from the cache.
CACHE_CONTROL = "public, max-age=0, s-maxage=86400"
# Set by CloudFront's router function to the ID of the distribution that routed the request.
DISTRIBUTION_ID_HEADER = "x-distribution-id"
DISTRIBUTION_ID_RE = re.compile(r"^[A-Z0-9]{1,32}$")
# PUT path, as seen through CloudFront: /<property>/<file>.json
PUT_PATH_RE = re.compile(
    r"^/(?P<prop>[a-z0-9][a-z0-9_-]{0,62})/(?P<file>[A-Za-z0-9_-][A-Za-z0-9._-]{0,200}\.json)$"
)


@lru_cache(maxsize=1)
def get_s3():
    return boto3.client("s3")


@lru_cache(maxsize=1)
def get_cloudfront():
    return boto3.client("cloudfront")


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _get_header(event: dict, name: str) -> str | None:
    for key, value in (event.get("headers") or {}).items():
        if key.lower() == name:
            return value
    return None


def _lookup_property(bucket: str, code: str) -> str | None:
    try:
        obj = get_s3().get_object(Bucket=bucket, Key=f"{CODES_PREFIX}{hash_code(code)}.json")
    except ClientError as err:
        if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(obj["Body"].read())["property"]


def _parse_body(event: dict) -> bytes | None:
    raw = event.get("body") or ""
    try:
        body = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode("utf-8")
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return data

def _isIIIF(body) -> bool:
    if not isinstance(body, dict):
        return (False, f"Not a dict {type(body).__name__}")

    if "id" in body:
        types = ["Manifest","Collection","AnnotationCollection","AnnotationPage","Annotation"]
        if "type" not in body:
            return (False, "No type")
        if body["type"] not in types:
            return (False, f"Type not in allowed list: {types}")
    elif "@id" in body:   
        types = ["sc:Manifest","sc:Collection","sc:AnnotationList","oa:Annotation"]
        if "@type" not in body:
            return (False, "No @type")
        if body["@type"] not in types:
            print ()
            return (False, f"@Type not in allowed list: {types}")
    else:
        return (False, "No id or @id found")

    return (True,"")    

def _exists(bucket: str, key: str) -> bool:
    try:
        get_s3().head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def _base_url(event: dict) -> str:
    """Public base URL for returned links, or "" if unknown.

    The custom domain (PUBLIC_BASE_URL) wins; otherwise use the CloudFront host
    the router function passed along. Direct function URL calls have neither.
    """
    configured = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if configured:
        return configured
    host = (_get_header(event, PUBLIC_HOST_HEADER) or "").strip().lower()
    if len(host) <= 253 and HOSTNAME_RE.match(host):
        return f"https://{host}"
    return ""


def _store(bucket: str, key: str, body: dict[str, Any], base_url: str) -> dict:
    """Write body to properties/<key> and return the response payload.

    key is relative to properties/, which is also the public URL path
    (CloudFront's S3 origin path is /properties).
    """
    if base_url:
        ident = f"{base_url}/{key}"
        if 'id' in body:
            body['id'] = ident
        elif '@id' in body:
            body['@id'] = ident

    get_s3().put_object(
        Bucket=bucket,
        Key=f"{PROPERTIES_PREFIX}{key}",
        Body=json.dumps(body).encode("utf-8"),
        ContentType="application/json",
        CacheControl=CACHE_CONTROL,
    )
    result = {"key": key}
    if base_url:
        result["url"] = f"{base_url}/{key}"
    return result


def _create(bucket: str, prop: str, body: dict[str, Any], base_url: str) -> dict:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{prop}/{timestamp}-{uuid.uuid4()}.json"
    return _response(201, _store(bucket, filename, body, base_url))


def _invalidate(event: dict, key: str) -> bool:
    """Ask CloudFront to drop its cached copy of /<key>. Doesn't wait for it to finish.

    Only possible when the request came through CloudFront (the router function sets
    the distribution ID). Failure is logged, not raised: the file is already replaced.
    """
    distribution_id = (_get_header(event, DISTRIBUTION_ID_HEADER) or "").strip()
    if not DISTRIBUTION_ID_RE.match(distribution_id):
        return False
    try:
        get_cloudfront().create_invalidation(
            DistributionId=distribution_id,
            InvalidationBatch={
                "Paths": {"Quantity": 1, "Items": [f"/{key}"]},
                "CallerReference": str(uuid.uuid4()),
            },
        )
    except Exception as err:  # noqa: BLE001 - any failure just means readers wait for the cache
        print(f"Invalidation of /{key} on {distribution_id} failed: {err!r}")
        return False
    return True


def _update(event: dict, bucket: str, prop: str, body: dict[str, Any], path: str, base_url: str) -> dict:
    match = PUT_PATH_RE.match(path)
    if not match:
        return _response(404, {"error": "not found"})
    if match["prop"] != prop:
        return _response(403, {"error": "access code does not match this property"})
    key = f"{match['prop']}/{match['file']}"
    if not _exists(bucket, f"{PROPERTIES_PREFIX}{key}"):
        return _response(404, {"error": "not found"})
    result = _store(bucket, key, body, base_url)
    result["invalidated"] = _invalidate(event, key)
    return _response(200, result)


def handler(event, context):
    bucket = os.environ["BUCKET_NAME"]
    method = (event.get("requestContext") or {}).get("http", {}).get("method", "").upper()
    if method not in ("POST", "PUT"):
        return _response(405, {"error": "method not allowed"})

    code = _get_header(event, ACCESS_CODE_HEADER)
    if not code:
        return _response(401, {"error": "missing access code"})

    prop = _lookup_property(bucket, code)
    if prop is None:
        return _response(403, {"error": "invalid access code"})

    # After this body is JSON
    body = _parse_body(event)
    if body is None:
        return _response(400, {"error": "body must be valid JSON"})

    (isValid, reason) = _isIIIF(body)
    if not isValid:    
        return _response(400, {"error": f"Body must be IIIF. Failed due to: {reason}"})

    base_url = _base_url(event)
    if method == "POST":
        return _create(bucket, prop, body, base_url)
    return _update(event, bucket, prop, body, event.get("rawPath") or "", base_url)
