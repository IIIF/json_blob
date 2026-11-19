"""Create a property (folder + access code) in the JSON blob bucket, or rotate its code.

Usage:
    uv run python scripts/create_property.py <property-name> [--bucket NAME] [--rotate]

The access code is printed once. Only its SHA-256 hash is stored, so it cannot be
recovered from the bucket.
"""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys

import boto3
from botocore.exceptions import ClientError

DEFAULT_BUCKET = "iiif-json-blob"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class PropertyError(Exception):
    pass


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _get_json(s3, bucket: str, key: str) -> dict | None:
    try:
        obj = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if err.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return None
        raise
    return json.loads(obj["Body"].read())


def _put_json(s3, bucket: str, key: str, data: dict) -> None:
    s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(data).encode(), ContentType="application/json")


def create_property(s3, bucket: str, name: str, rotate: bool = False) -> str:
    """Create (or rotate) the property's access code and return the new plaintext code."""
    if not NAME_RE.match(name):
        raise PropertyError(
            f"invalid property name {name!r}: use lowercase letters, digits, '-' or '_' (max 63 chars)"
        )

    registry_key = f"registry/{name}.json"
    existing = _get_json(s3, bucket, registry_key)
    if existing and not rotate:
        raise PropertyError(f"property {name!r} already exists; use --rotate to issue a new code")
    if rotate and not existing:
        raise PropertyError(f"property {name!r} does not exist; nothing to rotate")

    code = secrets.token_urlsafe(24)
    code_hash = hash_code(code)

    s3.put_object(Bucket=bucket, Key=f"properties/{name}/", Body=b"")
    _put_json(s3, bucket, f"codes/{code_hash}.json", {"property": name})
    _put_json(s3, bucket, registry_key, {"code_hash": code_hash})

    if existing:
        s3.delete_object(Bucket=bucket, Key=f"codes/{existing['code_hash']}.json")

    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("property", help="property name, e.g. acme")
    parser.add_argument(
        "--bucket",
        default=os.environ.get("BUCKET_NAME", DEFAULT_BUCKET),
        help=f"bucket name (default: $BUCKET_NAME or {DEFAULT_BUCKET})",
    )
    parser.add_argument("--rotate", action="store_true", help="replace the existing code for this property")
    args = parser.parse_args(argv)

    try:
        code = create_property(boto3.client("s3"), args.bucket, args.property, rotate=args.rotate)
    except PropertyError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1

    action = "Rotated" if args.rotate else "Created"
    print(f"{action} property {args.property!r} in bucket {args.bucket!r}.")
    print(f"Access code: {code}")
    print("Store this code somewhere safe now. It cannot be recovered from the bucket.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
