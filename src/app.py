"""Lambda handler: store JSON in S3 under the property its access code maps to.

POST (to the function URL)          creates properties/<property>/<timestamp>-<uuid>.json
PUT  /<property>/<file>.json        replaces an existing file (routed here by CloudFront)
"""

import base64
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from functools import lru_cache

import boto3
from botocore.exceptions import ClientError

CODES_PREFIX = "codes/"
PROPERTIES_PREFIX = "properties/"
ACCESS_CODE_HEADER = "x-access-code"
CACHE_CONTROL = "public, max-age=60"
# PUT path, as seen through CloudFront: /<property>/<file>.json
PUT_PATH_RE = re.compile(
    r"^/(?P<prop>[a-z0-9][a-z0-9_-]{0,62})/(?P<file>[A-Za-z0-9_-][A-Za-z0-9._-]{0,200}\.json)$"
)


@lru_cache(maxsize=1)
def get_s3():
    return boto3.client("s3")


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
        json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return None
    return body

def _isIIIF(data) -> bool:
    print (data)
    body = json.loads(data)
    if not isinstance(body, dict):
        print (f"Not a dict {type(body).__name__}")
        return False

    if "id" in body:
        types = ["Manifest","Collection","AnnotationCollection","AnnotationPage","Annotation"]
        if "type" not in body:
            print ("No type")
            return False
        if body["type"] not in types:
            print ("Type not in allowed list")
            return False
    elif "@id" in body:   
        types = ["sc:Manifest","sc:Collection","sc:AnnotationList","oa:Annotation"]
        if "@type" not in body:
            print ("No @type")
            return False
        if body["@type"] not in types:
            print ("@Type not in allowed list")
            return False
    else:
        print ("no id")
        return False

    return True    

def _exists(bucket: str, key: str) -> bool:
    try:
        get_s3().head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return True


def _store(bucket: str, key: str, body: bytes) -> dict:
    """Write body to properties/<key> and return the response payload.

    key is relative to properties/, which is also the public URL path
    (CloudFront's S3 origin path is /properties).
    """
    get_s3().put_object(
        Bucket=bucket,
        Key=f"{PROPERTIES_PREFIX}{key}",
        Body=body,
        ContentType="application/json",
        CacheControl=CACHE_CONTROL,
    )
    result = {"key": key}
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if base_url:
        result["url"] = f"{base_url}/{key}"
    return result


def _create(bucket: str, prop: str, body: bytes) -> dict:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return _response(201, _store(bucket, f"{prop}/{timestamp}-{uuid.uuid4()}.json", body))


def _update(bucket: str, prop: str, body: bytes, path: str) -> dict:
    match = PUT_PATH_RE.match(path)
    if not match:
        return _response(404, {"error": "not found"})
    if match["prop"] != prop:
        return _response(403, {"error": "access code does not match this property"})
    key = f"{match['prop']}/{match['file']}"
    if not _exists(bucket, f"{PROPERTIES_PREFIX}{key}"):
        return _response(404, {"error": "not found"})
    return _response(200, _store(bucket, key, body))


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

    body = _parse_body(event)
    if body is None:
        return _response(400, {"error": "body must be valid JSON"})

    if not _isIIIF(body):    
        return _response(400, {"error": "body must be IIIF"})

    if method == "POST":
        return _create(bucket, prop, body)
    return _update(bucket, prop, body, event.get("rawPath") or "")
