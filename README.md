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

## Calling the function

POST new JSON to the public URL, the same one uploads are read from: `https://jsonblob.iiif.io/`, or the `PublicBaseUrl` stack output until the custom domain is set up. The path is ignored; the access code decides which property the file goes into.

```sh
curl -X POST "https://jsonblob.iiif.io/" \
  -H "x-access-code: <code>" \
  -H "Content-Type: application/json" \
  -d '{"hello": "world"}'
```

From a web page on another origin:

```js
const resp = await fetch("https://jsonblob.iiif.io/", {
  method: "POST",
  headers: { "Content-Type": "application/json", "x-access-code": code },
  body: JSON.stringify(data),
});
const { url } = await resp.json();  // public HTTPS link to the stored JSON
```

CloudFront answers CORS preflight requests itself, and any origin can POST; the access code is what protects uploads.

You can also POST straight to the Lambda function URL (the `FunctionUrl` stack output). It works the same way, but until the custom domain is set up its responses contain only `key`, not `url`. The function URL handles its own CORS preflights; to allow only specific origins there, add `AllowedUploadOrigins=https://example.org,https://other.example` to `parameter_overrides` in `samconfig.toml`. That setting doesn't apply to the CloudFront URL.

| Status | Meaning |
| --- | --- |
| 201 | Stored. The response is `{"key": "<property>/<file>.json", "url": "https://jsonblob.iiif.io/<property>/<file>.json"}`. Before the custom domain is set up, `url` uses the `*.cloudfront.net` host you POSTed to |
| 400 | The body isn't valid JSON, or isn't a IIIF resource |
| 401 | The `x-access-code` header is missing |
| 403 | The code isn't recognised |

`PublicBaseUrl` and `FunctionUrl` are stack outputs: `aws cloudformation describe-stacks --stack-name json-blob --query "Stacks[0].Outputs"`.

## Reading uploads

Uploads are served over HTTPS by CloudFront at `https://jsonblob.iiif.io/<property>/<file>.json` (the `url` field returned by an upload), with `Access-Control-Allow-Origin: *`, so IIIF viewers and other browser apps on any origin can fetch them. The URL path is the S3 key without the `properties/` prefix, because CloudFront's origin path is `/properties`. Until a certificate is configured (see below), files are served from the distribution's `https://dxxxx.cloudfront.net` domain (the `PublicBaseUrl` output).

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

```js
await fetch(url, {
  method: "PUT",
  headers: { "Content-Type": "application/json", "x-access-code": code },
  body: JSON.stringify(data),
});
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
- Each invalidated path counts towards CloudFront's invalidation allowance: the first 1,000 paths a month are free, then $0.005 per path.
- A PUT counts as a new write, so it restarts the file's 30-day expiry.
- Browser POSTs and PUTs from any origin work: CloudFront answers the CORS preflight itself.
- How it works: CloudFront can't route by HTTP method, so a CloudFront Function (`RouterFunction` in `template.yaml`) sends POSTs and PUTs to the upload Lambda's function URL and GETs and HEADs to S3. For POSTs and PUTs it also passes the host the caller used in an `x-public-host` header, which the Lambda uses to build `url` when no custom domain is configured, and the distribution's ID in an `x-distribution-id` header, which the Lambda uses to invalidate updated files. It overwrites both headers, so callers can't set them. The function's permission to invalidate is a separate IAM policy (`UploadFunctionInvalidationPolicy`), limited to this one distribution. `tests/test_router_function.py` runs its logic under Node. To test it in CloudFront's real runtime after deploying, use `aws cloudfront test-function`.

### Custom domain (jsonblob.iiif.io)

1. In ACM, **in us-east-1** (CloudFront only uses certificates from that region), request a public certificate for `jsonblob.iiif.io` with DNS validation, and add the validation CNAME at your DNS provider. Wait until its status is *Issued*.
2. Add the ARN to `parameter_overrides` in `samconfig.toml`:
   `parameter_overrides = "BucketName=iiif-json-blob CertificateArn=arn:aws:acm:us-east-1:…"`
3. Deploy (push to `main`). The distribution now accepts `jsonblob.iiif.io`, and uploads return URLs on that domain.
4. Add a DNS record: `jsonblob.iiif.io CNAME <CloudFrontDomain>`. `CloudFrontDomain` is a stack output.

Without `CertificateArn`, everything still works, but on the `*.cloudfront.net` domain.

## Managing properties

You need AWS credentials that can read and write the bucket.

```sh
uv run python scripts/create_property.py acme            # new property; prints the code once
uv run python scripts/create_property.py acme --rotate   # issue a new code; the old one stops working
```

Store the printed code somewhere safe. It can't be recovered from the bucket. Property names must be lowercase letters, digits, `-` or `_`, and at most 63 characters.

## CI/CD

`.github/workflows/deploy.yml` runs the tests on every push and pull request. On pushes to `main`, once the tests pass, it deploys with `sam deploy` using GitHub OIDC (no long-lived keys).

### One-time AWS setup

1. **Add the OIDC provider.** In IAM, go to *Identity providers* and add an OpenID Connect provider with URL `https://token.actions.githubusercontent.com` and audience `sts.amazonaws.com`.
2. **Create a deploy role** with this trust policy (replace `ACCOUNT_ID` and `OWNER/REPO`):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Principal": {"Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"},
       "Action": "sts:AssumeRoleWithWebIdentity",
       "Condition": {
         "StringEquals": {"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"},
         "StringLike": {"token.actions.githubusercontent.com:sub": "repo:OWNER/REPO:ref:refs/heads/main"}
       }
     }]
   }
   ```
   Give it permissions for CloudFormation, Lambda, S3 (including bucket policies and the SAM artifact bucket that `resolve_s3` creates), CloudFront (distributions, origin access controls, functions, and origin request and response headers policies) and IAM role creation/passing for the function's execution role.
3. **Add the secret.** In the GitHub repo, go to *Settings → Secrets and variables → Actions* and add `AWS_DEPLOY_ROLE_ARN` with the role's ARN.

`samconfig.toml` is already committed (stack `json-blob`, region `us-east-1`), so you don't need to run `sam deploy --guided`.

### Notes

- The bucket has `DeletionPolicy: Retain`, so deleting the stack doesn't delete the bucket or the codes in it. To redeploy from scratch after that, you need to delete the bucket by hand or import it into the new stack.
- S3 runs lifecycle expirations about once a day, so uploads may stay a day or so past 30 days. The empty `properties/<property>/` folder markers expire too; that's harmless.
- Versioning is deliberately off. With versioning on, expiration would only add delete markers and the old data would stay.
