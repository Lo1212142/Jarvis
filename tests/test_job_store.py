from openjarvis.jobs import JobStore


def test_job_store_persists_state_and_checkpoint(tmp_path):
    db = tmp_path / "jobs.db"
    first = JobStore(db)
    job = first.create(kind="project", prompt="Build and test a service")
    first.update(job.id, status="running", progress=0.4, checkpoint={"step": "tests"}, artifacts=["report.json"])
    first.close()

    second = JobStore(db)
    restored = second.get(job.id)
    assert restored.status == "running"
    assert restored.progress == 0.4
    assert restored.checkpoint == {"step": "tests"}
    assert restored.artifacts == ["report.json"]
    second.update(job.id, status="paused")
    assert second.get(job.id).status == "paused"
    second.update(job.id, status="cancelled", error="user requested cancellation")
    assert second.get(job.id).status == "cancelled"
    second.close()
