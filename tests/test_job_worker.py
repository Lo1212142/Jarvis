import time

from openjarvis.jobs import JobStore
from openjarvis.jobs.worker import JobWorker, JobWorkerConfig


def _wait_for(store, job_id, status, timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.get(job_id).status == status:
            return store.get(job_id)
        time.sleep(0.02)
    raise AssertionError(f"job did not reach {status}: {store.get(job_id)}")


def test_worker_runs_only_allowlisted_handler(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    done = store.create(kind="health_check", prompt="status")
    ignored = store.create(kind="arbitrary_code", prompt="do not execute")
    worker = JobWorker(store, {"health_check": lambda job, progress: progress(0.8, {"step": "checked"}) or ["health.txt"]}, JobWorkerConfig(poll_seconds=0.02))
    worker.start()
    restored = _wait_for(store, done.id, "completed")
    assert restored.progress == 1.0
    assert restored.artifacts == ["health.txt"]
    assert store.get(ignored.id).status == "queued"
    worker.stop()
    store.close()


def test_worker_recovers_interrupted_job(tmp_path):
    store = JobStore(tmp_path / "jobs.db")
    job = store.create(kind="health_check", prompt="status")
    store.update(job.id, status="running", checkpoint={"step": "before-restart"})
    worker = JobWorker(store, {"health_check": lambda job, progress: []}, JobWorkerConfig(poll_seconds=0.02))
    worker.start()
    restored = _wait_for(store, job.id, "completed")
    assert restored.checkpoint == {"phase": "completed"}
    worker.stop()
    store.close()
