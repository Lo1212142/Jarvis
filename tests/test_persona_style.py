from openjarvis.core.config import SystemPromptConfig
from openjarvis.prompt.builder import SystemPromptBuilder


def test_persona_style_includes_bounded_humor_and_predictions() -> None:
    builder = SystemPromptBuilder(
        agent_template="You are Jarvis.",
        system_prompt_config=SystemPromptConfig(
            humor_enabled=True,
            humor_style="dry",
            sarcasm_level="light",
            predictive_suggestions=True,
            max_predictive_suggestions=2,
        ),
    )
    prompt = builder.build()
    assert "context-aware dry humor" in prompt
    assert "sarcasm level is light" in prompt
    assert "at most 2 proactive suggestions" in prompt
    assert "ask before sending" in prompt


def test_persona_style_can_disable_humor_and_predictions() -> None:
    builder = SystemPromptBuilder(
        agent_template="You are Jarvis.",
        system_prompt_config=SystemPromptConfig(
            humor_enabled=False,
            predictive_suggestions=False,
        ),
    )
    prompt = builder.build()
    assert "Do not use jokes or sarcasm" in prompt
    assert "Do not proactively predict" in prompt
