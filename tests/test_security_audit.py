from pathlib import Path

from openjarvis.tools.security_audit import SecurityAuditTool


def test_security_audit_redacts_secret_and_detects_container_risk(tmp_path: Path) -> None:
    (tmp_path / ".env.example").write_text("NIM_API_KEY=sk-12345678901234567890\n", encoding="utf-8")
    (tmp_path / "compose.yml").write_text("services:\n  app:\n    privileged: true\n", encoding="utf-8")
    result = SecurityAuditTool().execute(root=str(tmp_path), include_hashes=False)
    assert result.success
    assert result.metadata["network_access"] is False
    findings = result.metadata["findings"]
    assert any("possible-secret-material" in item["findings"] for item in findings)
    assert any("dangerous-container-setting" in item["findings"] for item in findings)
    assert "sk-123" not in str(result.metadata)
