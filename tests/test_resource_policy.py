from openjarvis.security.resource_policy import ResourceBudget


def test_default_policy_is_cpu_first(monkeypatch) -> None:
    for key in (
        "JARVIS_CPU_LIMIT",
        "JARVIS_MEMORY_MB",
        "JARVIS_ENABLE_GPU",
    ):
        monkeypatch.delenv(key, raising=False)
    budget = ResourceBudget.from_environment()
    assert budget.gpu_enabled is False
    assert budget.cpu_cores == 2.0
    assert budget.memory_mb == 768
    assert budget.docker_limits()["no_gpu_reservation"] is True


def test_resource_values_are_bounded(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_MB", "999999")
    monkeypatch.setenv("JARVIS_MAX_PARALLEL_JOBS", "0")
    monkeypatch.setenv("JARVIS_SANDBOX_MEMORY_MB", "1")
    budget = ResourceBudget.from_environment()
    assert budget.memory_mb == 8192
    assert budget.max_parallel_jobs == 1
    assert budget.sandbox_memory_mb == 64
