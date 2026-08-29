import tarfile

import pytest

from openjarvis.operations.allocator import ResourceAllocator
from openjarvis.operations.backup import BackupService
from openjarvis.operations.devices import DeviceCommandGate


def test_resource_allocator_enforces_quotas_and_release():
    allocator = ResourceAllocator(cpu_cores=1, memory_mb=256, max_leases=2)
    first = allocator.acquire("job-1", cpu=0.75, memory_mb=128)
    assert first is not None
    assert allocator.acquire("job-2", cpu=0.5, memory_mb=128) is None
    assert allocator.release(first.lease_id) is True
    assert allocator.acquire("job-2", cpu=0.5, memory_mb=128) is not None


def test_device_gate_requires_allowlist_and_approval():
    calls = []
    gate = DeviceCommandGate(sender=lambda device, command, params: calls.append((device.device_id, command, params)) or True)
    device = gate.pair("desk lamp", "mqtt", {"turn_on"})
    denied = gate.request(device.device_id, "unlock_door")
    assert denied["allowed"] is False
    pending = gate.request(device.device_id, "turn_on", {"brightness": 50})
    assert pending["approval_required"] is True
    assert gate.approve(pending["approval_id"]) is True
    assert calls[0][1] == "turn_on"
    assert gate.approve(pending["approval_id"]) is False


def test_backup_create_verify_restore_and_excludes_secrets(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.txt").write_text("safe data", encoding="utf-8")
    (source / ".env").write_text("SECRET=do-not-copy", encoding="utf-8")
    service = BackupService(source_root=source)
    archive = tmp_path / "backup.tar.gz"
    created = service.create(archive)
    assert created["files"] == 1
    assert service.verify(archive)["valid"] is True
    restored = tmp_path / "restored"
    assert service.restore(archive, restored)["restored"] is True
    assert (restored / "notes.txt").read_text(encoding="utf-8") == "safe data"
    assert not (restored / ".env").exists()


def test_backup_rejects_traversal_member(tmp_path):
    archive = tmp_path / "bad.tar.gz"
    with tarfile.open(archive, "w:gz") as handle:
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        import io
        handle.addfile(info, io.BytesIO(b"x"))
    service = BackupService(source_root=tmp_path / "source")
    with pytest.raises((KeyError, ValueError)):
        service.restore(archive, tmp_path / "out")
