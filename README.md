# json-blob

An AWS Lambda function (Python 3.12) that receives a JSON body and stores it in the S3 bucket `iiif-json-blob`. Each property has its own folder and its own access code. The code the caller sends decides which folder the JSON goes into.

## Bucket layout

| Prefix | Contents | Expires |
| --- | --- | --- |
| `properties/<property>/` | Uploaded JSON, `<timestamp>-<uuid>.json` | after 30 days (lifecycle rule) |
| `codes/<sha256-of-code>.json` | `{"property": "<property>"}`: the Lambda reads this | never |
| `registry/<property>.json` | `{"code_hash": "<sha256>"}`: used by the setup script to detect duplicates and rotate codes | never |

Only the hashes of codes are stored, so anyone who can read the bucket still can't see the codes.

## Local development

```sh
uv sync              # creates .venv/ (git-ignored) with pytest, moto and the SAM CLI
uv run pytest -v     # tests use moto, so they never touch real AWS
uv run sam validate --lint
uv run sam build
```

To run only some of the tests:

```sh
uv run pytest -v tests/test_app.py                                  # one file
uv run pytest -v tests/test_app.py::test_valid_code_stores_json     # one test
uv run pytest -v -k invalidat                                       # tests whose names contain "invalidat"
```

For a parametrized test, add the case ID in square brackets, as `pytest -v` prints it, e.g. `"tests/test_app.py::test_invalid_json_is_bad_request[not json]"`. Quote it, so the shell doesn't treat the brackets as a pattern.

## Calling the function

POST new JSON to the public URL, the same one uploads are read from: `https://jsonblob.iiif.io/`, or the `PublicBaseUrl` stack output until the custom domain is set up. The path is ignored; the access code decides which property the file goes into.

```sh
curl -X POST "https://jsonblob.iiif.io/" \
  -H "x-access-code: <code>" \
  -H "Content-Type: application/json" \
  -d '{"hello": "world"}'
```

From Python, using [`requests`](https://pypi.org/project/requests/):

```python
import requests

resp = requests.post(
    "https://jsonblob.iiif.io/",
    json=data,  # sends the body as JSON with Content-Type: application/json
    headers={"x-access-code": code},
)
resp.raise_for_status()
url = resp.json()["url"]  # public HTTPS link to the stored JSON
```

Browser apps on any origin can POST too: CloudFront answers CORS preflight requests itself. The access code is what protects uploads.

You can also POST straight to the Lambda function URL (the `FunctionUrl` stack output). It works the same way, but until the custom domain is set up its responses contain only `key`, not `url`. The function URL handles its own CORS preflights; to allow only specific origins there, add `AllowedUploadOrigins=https://example.org,https://other.example` to `parameter_overrides` in `samconfig.toml`. That setting doesn't apply to the CloudFront URL.

| Status | Meaning |
| --- | --- |
| 201 | Stored. The response is `{"key": "<property>/<file>.json", "url": "https://jsonblob.iiif.io/<property>/<file>.json"}`. Before the custom domain is set up, `url` uses the `*.cloudfront.net` host you POSTed to |
| 400 | The body isn't valid JSON, or isn't a IIIF resource |
| 401 | The `x-access-code` header is missing |
| 403 | The code isn't recognised |

`PublicBaseUrl` and `FunctionUrl` are stack outputs: `aws cloudformation describe-stacks --stack-name json-blob --query "Stacks[0].Outputs"`.

## Reading uploads

Uploads are served over HTTPS by CloudFront at `https://jsonblob.iiif.io/<property>/<file>.json` (the `url` field returned by an upload), with `Access-Control-Allow-Origin: *`, so IIIF viewers and other browser apps on any origin can fetch them. The URL path is the S3 key without the `properties/` prefix, because CloudFront's origin path is `/properties`. 

- The bucket itself stays private (Block Public Access is on). CloudFront reads it through Origin Access Control, only under `properties/`, so `codes/` and `registry/` can't be reached at all, and nobody can list folder contents.
- Anyone who has a URL can read that file. The filenames include a random UUID, so they can't be guessed.
- Files are stored with `Cache-Control: public, max-age=0, s-maxage=86400`. Browsers re-check with CloudFront on every request (usually a cheap `304 Not Modified`), and CloudFront keeps a copy for up to a day. A PUT through the public URL clears CloudFront's copy, so updates show straight away (see below).
- Uploads are deleted 30 days after they were last written, so their URLs stop working then (or up to a day later, while CloudFront still has a cached copy).

## Updating uploads

PUT to a file's public URL with an access code for the same property to replace its contents:

```sh
curl -X PUT "https://jsonblob.iiif.io/acme/<file>.json" \
  -H "x-access-code: <code>" \
  -H "Content-Type: application/json" \
  -d '{"hello": "again"}'
```

```python
import requests

resp = requests.put(url, json=data, headers={"x-access-code": code})
resp.raise_for_status()
print(resp.json()["invalidated"])  # True once CloudFront's cached copy is being cleared
```

| Status | Meaning |
| --- | --- |
| 200 | Replaced. The response is `{"key": ..., "url": ..., "invalidated": true}` (the same URL). `invalidated` says whether CloudFront's cached copy was cleared |
| 400 | The body isn't valid JSON (the file is unchanged) |
| 401 | The `x-access-code` header is missing |
| 403 | The code isn't recognised, or it belongs to a different property |
| 404 | No such file. PUT only updates; use POST to create new files |
| 405 | Methods other than GET, HEAD, POST, PUT and OPTIONS aren't allowed on the public URL |

- Readers see the new version straight away. After replacing the file, the Lambda asks CloudFront to invalidate that one path, which usually takes a few seconds. It doesn't wait for this to finish.
- If the invalidation can't be started, the file is still replaced and the response says `"invalidated": false`. Readers may then see the old version for up to a day. To clear it by hand: `aws cloudfront create-invalidation --distribution-id <id> --paths "/<property>/<file>.json"`.
- PUTs straight to the Lambda function URL don't invalidate, because the function only learns the distribution's ID from requests that come through CloudFront.
- A PUT counts as a new write, so it restarts the file's 30-day expiry.
- Browser POSTs and PUTs from any origin work: CloudFront answers the CORS preflight itself.

## Managing properties

You need AWS credentials that can read and write the bucket.

```sh
uv run python scripts/create_property.py acme            # new property; prints the code once
uv run python scripts/create_property.py acme --rotate   # issue a new code; the old one stops working
```

Store the printed code somewhere safe. It can't be recovered from the bucket. Property names must be lowercase letters, digits, `-` or `_`, and at most 63 characters.

## CI/CD

`.github/workflows/deploy.yml` runs the tests on every push and pull request. On pushes to `main`, once the tests pass, it deploys with `sam deploy` using GitHub OIDC (no long-lived keys).

### Notes

- The bucket has `DeletionPolicy: Retain`, so deleting the stack doesn't delete the bucket or the codes in it. To redeploy from scratch after that, you need to delete the bucket by hand or import it into the new stack.
- S3 runs lifecycle expirations about once a day, so uploads may stay a day or so past 30 days. The empty `properties/<property>/` folder markers expire too; that's harmless.
- Versioning is deliberately off. With versioning on, expiration would only add delete markers and the old data would stay.
