from __future__ import annotations

import io
import json
import os
from pathlib import Path

from supernote_module_generator.cli import main
from supernote_module_generator.transaction import JOURNAL_NAME, Transaction, recover_pending


def test_rollback_restores_files_and_removes_created_tree(tmp_path: Path):
    parent = tmp_path / "package.json"
    parent.write_text('{"before": true}\n', encoding="utf-8")
    created = tmp_path / "local_modules/local-math"
    transaction = Transaction(tmp_path, "add", ["local-math"])
    transaction.snapshot([parent, tmp_path / "local_modules"])
    (tmp_path / "local_modules").mkdir()
    created.mkdir()
    (created / "file").write_text("new", encoding="utf-8")
    parent.write_text('{"after": true}\n', encoding="utf-8")
    transaction.mark_write()

    rollback = transaction.rollback()
    assert rollback.status == "completed"
    assert parent.read_text(encoding="utf-8") == '{"before": true}\n'
    assert not (tmp_path / "local_modules").exists()
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_startup_recovery_uses_persistent_journal(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.snapshot([path])
    path.write_text("after", encoding="utf-8")
    transaction.mark_write()

    outcome = recover_pending(tmp_path)
    assert outcome.rollback.status == "completed"
    assert path.read_text(encoding="utf-8") == "before"
    assert outcome.warning is not None
    assert 'interrupted Update for "local-math"' in outcome.warning.message


def test_external_reconciliation_failure_remains_recoverable(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "remove", ["local-math"])
    transaction.snapshot([path])
    path.write_text("after", encoding="utf-8")
    transaction.mark_external(["npm", "install"])

    rollback = transaction.rollback(reconcile=lambda _: False)
    assert rollback.status == "partial"
    assert (tmp_path / JOURNAL_NAME).is_file()
    journal = json.loads((tmp_path / JOURNAL_NAME).read_text(encoding="utf-8"))
    assert journal["phase"] == "rollback_partial"


def test_update_activation_restores_original_directory(tmp_path: Path):
    destination = tmp_path / "local_modules/local-math"
    destination.mkdir(parents=True)
    (destination / "implementation.kt").write_text("original", encoding="utf-8")
    staged = tmp_path / ".staged"
    staged.mkdir()
    (staged / "generated.kt").write_text("new", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.track_created(staged)
    transaction.activate(staged, destination)
    assert (destination / "generated.kt").is_file()

    rollback = transaction.rollback()
    assert rollback.status == "completed"
    assert (destination / "implementation.kt").read_text() == "original"


def test_partial_rollback_can_be_retried_without_reapplying_restored_entries(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.snapshot([path])
    path.write_text("after", encoding="utf-8")
    transaction.mark_external(["npm", "install"])

    first = transaction.rollback(reconcile=lambda _: False)
    assert first.status == "partial"
    assert path.read_text(encoding="utf-8") == "before"

    second = recover_pending(tmp_path, reconcile=lambda _: True)
    assert second.rollback.status == "completed"
    assert path.read_text(encoding="utf-8") == "before"
    assert not (tmp_path / JOURNAL_NAME).exists()


def test_corrupt_startup_journal_is_a_structured_partial_result(tmp_path: Path):
    (tmp_path / "android").mkdir()
    (tmp_path / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        '{"name":"fixture","dependencies":{}}\n', encoding="utf-8"
    )
    (tmp_path / "android/settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    (tmp_path / JOURNAL_NAME).write_text("not json\n", encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    code = main(
        ["--json", "validate", "--all"],
        stdin=io.StringIO(),
        stdout=stdout,
        stderr=stderr,
        cwd=tmp_path,
    )

    value = json.loads(stdout.getvalue())
    assert code == 3
    assert stderr.getvalue() == ""
    assert value["status"] == "partial"
    assert value["rollback"]["status"] == "failed"
    assert value["error"]["kind"] == "startup_recovery_failed"


def test_recovery_does_not_delete_original_when_activation_rename_never_started(
    tmp_path: Path, monkeypatch
):
    destination = tmp_path / "local_modules/local-math"
    destination.mkdir(parents=True)
    (destination / "source").write_text("original", encoding="utf-8")
    staged = tmp_path / ".staged"
    staged.mkdir()
    (staged / "generated").write_text("new", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.track_created(staged)
    original_replace = os.replace

    def fail_first_module_move(source, target):
        if Path(source) == destination:
            raise OSError("simulated crash window")
        original_replace(source, target)

    monkeypatch.setattr("supernote_module_generator.transaction.os.replace", fail_first_module_move)
    try:
        transaction.activate(staged, destination)
    except OSError:
        pass
    monkeypatch.setattr("supernote_module_generator.transaction.os.replace", original_replace)

    outcome = recover_pending(tmp_path)
    assert outcome.rollback.status == "completed"
    assert (destination / "source").read_text(encoding="utf-8") == "original"


def test_recovery_recognizes_original_restored_before_journal_update(tmp_path: Path):
    destination = tmp_path / "local_modules/local-math"
    destination.mkdir(parents=True)
    (destination / "source").write_text("original", encoding="utf-8")
    staged = tmp_path / ".staged"
    staged.mkdir()
    (staged / "generated").write_text("new", encoding="utf-8")
    transaction = Transaction(tmp_path, "update", ["local-math"])
    transaction.track_created(staged)
    transaction.activate(staged, destination)
    entry = transaction.data["entries"][1]
    restore = Path(entry["restore"])
    for child in destination.iterdir():
        child.unlink()
    destination.rmdir()
    os.replace(restore, destination)

    outcome = recover_pending(tmp_path)
    assert outcome.rollback.status == "completed"
    assert (destination / "source").read_text(encoding="utf-8") == "original"


def test_startup_finalizes_a_transaction_that_reached_commit(tmp_path: Path):
    path = tmp_path / "package.json"
    path.write_text("before", encoding="utf-8")
    transaction = Transaction(tmp_path, "add", ["local-math"])
    transaction.snapshot([path])
    path.write_text("committed", encoding="utf-8")
    transaction.data["phase"] = "commit"
    transaction._persist()

    outcome = recover_pending(tmp_path)

    assert outcome.rollback.status == "not_needed"
    assert path.read_text(encoding="utf-8") == "committed"
    assert outcome.warning is not None
    assert "already committed" in outcome.warning.message
