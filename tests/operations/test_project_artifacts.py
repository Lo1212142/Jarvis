import json

import pytest

from openjarvis.operations.project_artifacts import ProjectArtifactBuilder


def test_project_artifact_builder_writes_structured_artifact(tmp_path):
    builder = ProjectArtifactBuilder(tmp_path)
    path = builder.write("adr", "Use isolated workers", {"decision": "yes", "risks": ["cost"]})
    document = json.loads(open(path, encoding="utf-8").read())
    assert document["kind"] == "adr"
    assert document["schema_version"] == 1
    assert document["data"]["decision"] == "yes"


def test_project_artifact_builder_rejects_unknown_kind(tmp_path):
    with pytest.raises(ValueError, match="unsupported"):
        ProjectArtifactBuilder(tmp_path).write("deploy_now", "bad", {})
