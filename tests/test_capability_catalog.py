import pytest
from pydantic import ValidationError

from openjarvis.self_development.capability_catalog import get_capability
from openjarvis.server.settings_routes import RuntimeSettingsPatch


def test_blocked_intrusion_capability_is_explicit() -> None:
    capability = get_capability("security.unauthorized_intrusion")
    assert capability.risk == "blocked"
    assert capability.requires_approval is True


def test_nim_rpm_is_hard_capped() -> None:
    with pytest.raises(ValidationError):
        RuntimeSettingsPatch(nim_rpm_limit=41)
    assert RuntimeSettingsPatch(nim_rpm_limit=40).nim_rpm_limit == 40
