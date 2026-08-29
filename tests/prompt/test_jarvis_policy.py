from openjarvis.core.config import SystemPromptConfig
from openjarvis.prompt.builder import SystemPromptBuilder
from openjarvis.prompt.jarvis_policy import JARVIS_OPERATING_POLICY


def test_jarvis_policy_covers_browser_logs_and_safety():
    assert "browser computer" in JARVIS_OPERATING_POLICY.lower()
    assert "log center" in JARVIS_OPERATING_POLICY.lower()
    assert "40 requests per minute" in JARVIS_OPERATING_POLICY
    assert "CAPTCHA" in JARVIS_OPERATING_POLICY
    assert '"يا Boss"' in JARVIS_OPERATING_POLICY
    assert '"Boss"' in JARVIS_OPERATING_POLICY
    assert "resource_status" in JARVIS_OPERATING_POLICY
    assert "Never invent" in JARVIS_OPERATING_POLICY
    prompt = SystemPromptBuilder(
        "You are a test agent.",
        system_prompt_config=SystemPromptConfig(jarvis_capability_guide_enabled=True),
    ).build()
    assert "jarvis_capability_guide" not in prompt
    assert "Browser action rules" in prompt


def test_jarvis_policy_can_be_disabled_and_bounded():
    disabled = SystemPromptBuilder(
        "agent", system_prompt_config=SystemPromptConfig(jarvis_capability_guide_enabled=False)
    ).build()
    assert "Browser action rules" not in disabled
    bounded = SystemPromptBuilder(
        "agent", system_prompt_config=SystemPromptConfig(jarvis_capability_guide_max_chars=1200)
    ).build()
    assert len(bounded) < 2_000
