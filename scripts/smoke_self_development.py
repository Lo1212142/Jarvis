"""Smoke test for the guarded self-development workflow."""

from pathlib import Path
import tempfile

from openjarvis.self_development.pipeline import IntegrationRequest, prepare_integration


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="jarvis-self-dev-") as temp_dir:
        artifact = prepare_integration(
            IntegrationRequest(
                request="أضف أداة لقراءة رسائل Zoho Mail مع بحث وعرض فقط",
                provider="zoho",
                docs_url="https://www.zoho.com/crm/developer/docs/api/v7/",
                requested_capabilities=("read", "search", "list"),
            ),
            base_dir=Path(temp_dir),
        )
        assert artifact.status == "prepared"
        assert artifact.activation == "blocked_pending_review"
        assert '"production_tree_modified": false' in Path(artifact.manifest).read_text(encoding="utf-8")
        manifest = Path(artifact.manifest).read_text(encoding="utf-8")
        plan = Path(artifact.plan).read_text(encoding="utf-8")
        assert '"production_tree_modified": false' in manifest
        assert "Activation remains blocked" in plan
        print(artifact.workspace)
        print("prepared_without_production_changes")


if __name__ == "__main__":
    main()
