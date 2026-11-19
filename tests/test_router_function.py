"""Run the CloudFront router function from template.yaml under Node with a stub `cloudfront` module.

CloudFront's real runtime is only available in AWS (`aws cloudfront test-function`), so this
checks the routing logic, not runtime compatibility.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from samtranslator.yaml_helper import yaml_parse

TEMPLATE = Path(__file__).resolve().parent.parent / "template.yaml"
IMPORT_LINE = "import cf from 'cloudfront';"

HARNESS = """
var selected = [];
var cf = { selectRequestOriginById: function (id) { selected.push(id); } };
%s
var results = {};
['GET', 'HEAD', 'PUT', 'OPTIONS', 'POST', 'DELETE', 'PATCH'].forEach(function (m) {
  selected = [];
  var request = { method: m, uri: '/acme/x.json', headers: {} };
  var out = handler({ request: request });
  results[m] = { passthrough: out === request, status: out.statusCode || null,
                 headers: out === request ? null : out.headers, origins: selected };
});
console.log(JSON.stringify(results));
"""

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


@pytest.fixture(scope="module")
def results():
    template = yaml_parse(TEMPLATE.read_text())
    code = template["Resources"]["RouterFunction"]["Properties"]["FunctionCode"]
    assert IMPORT_LINE in code
    script = HARNESS % code.replace(IMPORT_LINE, "")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_reads_go_to_s3(results, method):
    assert results[method] == {"passthrough": True, "status": None, "headers": None, "origins": []}


def test_put_goes_to_upload_function(results):
    assert results["PUT"]["passthrough"] is True
    assert results["PUT"]["origins"] == ["UploadFunctionOrigin"]


def test_options_answers_preflight(results):
    opts = results["OPTIONS"]
    assert opts["status"] == 204
    assert opts["headers"]["access-control-allow-origin"] == {"value": "*"}
    assert "PUT" in opts["headers"]["access-control-allow-methods"]["value"]
    assert "x-access-code" in opts["headers"]["access-control-allow-headers"]["value"]


@pytest.mark.parametrize("method", ["POST", "DELETE", "PATCH"])
def test_other_methods_rejected(results, method):
    assert results[method]["status"] == 405
    assert results[method]["origins"] == []


def test_origin_id_exists_in_distribution():
    template = yaml_parse(TEMPLATE.read_text())
    origins = template["Resources"]["Distribution"]["Properties"]["DistributionConfig"]["Origins"]
    assert "UploadFunctionOrigin" in [o["Id"] for o in origins]
