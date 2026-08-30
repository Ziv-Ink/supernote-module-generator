from __future__ import annotations

import os
from pathlib import Path
import stat
import subprocess

import pytest

import supernote_module_generator.filesystem as filesystem_module
import supernote_module_generator.transaction as transaction_module
from supernote_module_generator.feature_generator import FeatureConfig
from supernote_module_generator.feature_model import StarterFamily
from supernote_module_generator.errors import FilesystemError
from supernote_module_generator.feature_operations import FeatureOperationService
from supernote_module_generator.filesystem import (
    contained_entry_kind_no_follow,
    copy_entry_no_follow,
    entry_kind,
    iter_tree_no_follow,
)
from supernote_module_generator.generation_service import GenerationService
from supernote_module_generator.transaction import Transaction
from supernote_module_generator.v4_cli_operations import V4CliOperationService


def _plugin(root: Path) -> Path:
    (root / "android/app").mkdir(parents=True)
    (root / "PluginConfig.json").write_text("{}\n", encoding="utf-8")
    (root / "android/settings.gradle").write_text("include ':app'\n", encoding="utf-8")
    (root / "android/app/build.gradle").write_text("plugins {}\n", encoding="utf-8")
    (root / "package.json").write_text(
        '{"name":"platform-fixture","dependencies":{}}\n', encoding="utf-8"
    )
    FeatureOperationService(root).add(
        FeatureConfig(
            root / "local_modules/alpha",
            "alpha",
            "0.1.0",
            "com.example.alpha",
            "Alpha",
            starters=(StarterFamily.NATIVE,),
        )
    )
    service = GenerationService(root)
    plan = service.plan(
        operation="bootstrap",
        requested_targets=("alpha",),
        allow_unmanifested_bootstrap=True,
    )
    service.execute(plan, Transaction(root, "bootstrap", ("alpha",)))
    return root


def _exercise_mutation_validation_and_rollback(root: Path) -> None:
    service = GenerationService(root)
    source = root / "local_modules/alpha/android/src/main/cpp/feature.cpp"
    generated = root / "local_modules/alpha/index.d.ts"
    baseline_generated = generated.read_bytes()
    source.write_text(
        source.read_text(encoding="utf-8").replace("greet(", "greetPath("),
        encoding="utf-8",
    )
    plan = service.plan(operation="update", requested_targets=("alpha",))
    transaction = Transaction(root, "update", ("alpha",))
    service.execute(plan, transaction, commit=False)
    assert generated.read_bytes() != baseline_generated
    rollback = transaction.rollback()
    assert rollback.status == "completed"
    assert generated.read_bytes() == baseline_generated
    assert "greetPath(" in source.read_text(encoding="utf-8")

    committed = service.plan(operation="update", requested_targets=("alpha",))
    service.execute(committed, Transaction(root, "update", ("alpha",)))
    assert V4CliOperationService(root).check().status == "success"


@pytest.mark.parametrize("directory", ("project with spaces", "פרויקט-unicode-文档"))
def test_active_v4_path_supports_spaces_and_unicode(
    tmp_path: Path, directory: str
) -> None:
    root = _plugin(tmp_path / directory)

    _exercise_mutation_validation_and_rollback(root)


@pytest.mark.skipif(os.name != "nt", reason="native Windows long-path contract")
def test_windows_long_path_reaches_active_v4_mutation_validation_and_rollback(
    tmp_path: Path,
) -> None:
    root = tmp_path
    component = "long-v4-path-segment-0123456789"
    while len(str(root)) < 285:
        root /= component
    root = _plugin(root)

    assert len(str(root)) >= 285
    _exercise_mutation_validation_and_rollback(root)


@pytest.mark.skipif(os.name != "nt", reason="native Windows transaction paths")
def test_windows_transaction_entry_accepts_native_absolute_path(tmp_path: Path) -> None:
    destination = tmp_path / "project with spaces" / "package.json"
    destination.parent.mkdir()
    destination.write_bytes(b"{}\n")

    assert transaction_module._validate_absolute_entry_path(
        tmp_path,
        str(destination),
        allow_missing_ancestors=False,
    ) == destination


@pytest.mark.skipif(os.name != "nt", reason="native Windows reparse-point contract")
def test_windows_junction_is_never_traversed_or_observed(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel.txt"
    sentinel.write_text("external remains\n", encoding="utf-8")
    os.utime(external, ns=(11_000_000_000, 12_000_000_000))
    os.utime(sentinel, ns=(13_000_000_000, 14_000_000_000))
    observed = tmp_path / "observed"
    observed.mkdir()
    junction = observed / "junction"
    result = subprocess.run(
        ("cmd", "/c", "mklink", "/J", str(junction), str(external)),
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.fail(f"Windows runner could not create the required junction: {result.stderr}")
    # Creating a junction resolves its target and can itself advance the target
    # directory's atime when NTFS last-access updates are enabled. Establish the
    # generator-owned observation boundary only after the OS fixture exists.
    expected_directory = external.lstat()
    expected_sentinel = sentinel.lstat()

    assert entry_kind(junction) == "symlink"
    assert contained_entry_kind_no_follow(observed, junction) == "symlink"
    assert list(iter_tree_no_follow(observed)) == [junction]
    observed_directory = external.lstat()
    observed_sentinel = sentinel.lstat()
    assert (
        observed_directory.st_atime_ns,
        observed_directory.st_mtime_ns,
    ) == (expected_directory.st_atime_ns, expected_directory.st_mtime_ns)
    assert (
        observed_sentinel.st_atime_ns,
        observed_sentinel.st_mtime_ns,
    ) == (expected_sentinel.st_atime_ns, expected_sentinel.st_mtime_ns)
    assert sentinel.read_text(encoding="utf-8") == "external remains\n"


@pytest.mark.skipif(os.name != "nt", reason="native Windows retained-handle race")
def test_windows_contained_classifier_retains_ancestor_against_junction_swap(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "plugin"
    ancestor = root / "scripts"
    ancestor.mkdir(parents=True)
    leaf = ancestor / "runPlugin.ps1"
    leaf.write_text("inside\n", encoding="utf-8")
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "runPlugin.ps1"
    sentinel.write_bytes(b"outside\n")
    expected = sentinel.lstat()
    original_retain = filesystem_module._windows_retain_non_reparse_ancestors
    attempted = False

    def retain_then_attempt_swap(path):
        nonlocal attempted
        handles = original_retain(path)
        attempted = True
        displaced = tmp_path / "scripts-displaced"
        with pytest.raises(OSError):
            os.replace(ancestor, displaced)
        return handles

    monkeypatch.setattr(
        filesystem_module,
        "_windows_retain_non_reparse_ancestors",
        retain_then_attempt_swap,
    )

    assert contained_entry_kind_no_follow(root, leaf) == "file"
    assert attempted
    current = sentinel.lstat()
    assert sentinel.read_bytes() == b"outside\n"
    assert (
        current.st_mode,
        current.st_atime_ns,
        current.st_mtime_ns,
    ) == (expected.st_mode, expected.st_atime_ns, expected.st_mtime_ns)


@pytest.mark.skipif(os.name != "nt", reason="native Windows retained-link race")
def test_windows_symlink_target_read_retains_identity_across_aba_attempt(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source-link"
    try:
        source.symlink_to("owned-target")
    except OSError as exc:
        pytest.fail(f"Windows runner cannot create the required symlink: {exc}")
    baseline = filesystem_module.hash_entry_no_follow(source)
    original_read = filesystem_module._windows_read_symlink_target
    attempted = False

    def read_after_swap_attempt(handle):
        nonlocal attempted
        attempted = True
        with pytest.raises(OSError):
            os.replace(source, tmp_path / "displaced-link")
        return original_read(handle)

    monkeypatch.setattr(
        filesystem_module,
        "_windows_read_symlink_target",
        read_after_swap_attempt,
    )

    assert filesystem_module.hash_entry_no_follow(source) == baseline
    assert attempted
    assert os.readlink(source) == "owned-target"


@pytest.mark.skipif(os.name != "nt", reason="native Windows retained-atime race")
def test_windows_atime_neutralization_preserves_concurrent_mtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("value\n", encoding="utf-8")
    created = source.lstat()
    os.utime(source, ns=(1_000_000_000, created.st_mtime_ns))
    external_mtime = created.st_mtime_ns + 10_000_000_000
    original_apply = filesystem_module._windows_apply_handle_metadata_values
    injected = False

    def update_mtime_before_atime_restore(handle, **values):
        nonlocal injected
        if values["mtime_ns"] is None:
            injected = True
            current = source.lstat()
            os.utime(source, ns=(current.st_atime_ns, external_mtime))
        original_apply(handle, **values)

    monkeypatch.setattr(
        filesystem_module,
        "_windows_apply_handle_metadata_values",
        update_mtime_before_atime_restore,
    )

    with pytest.raises(FilesystemError, match="changed while"):
        filesystem_module.read_regular_bytes_no_follow(source)

    assert injected
    assert source.lstat().st_mtime_ns == external_mtime


@pytest.mark.skipif(os.name != "nt", reason="native Windows retained-copy race")
def test_windows_copy_destination_cannot_be_replaced_before_metadata_publication(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.txt"
    source.write_text("value\n", encoding="utf-8")
    expected = source.read_bytes()
    destination = tmp_path / "destination.txt"
    original_apply = filesystem_module._apply_descriptor_metadata
    attempted = False

    def attempt_replace_then_apply(descriptor, metadata):
        nonlocal attempted
        attempted = True
        with pytest.raises(OSError):
            os.replace(destination, tmp_path / "detached.txt")
        original_apply(descriptor, metadata)

    monkeypatch.setattr(
        filesystem_module,
        "_apply_descriptor_metadata",
        attempt_replace_then_apply,
    )

    copy_entry_no_follow(source, destination)

    assert attempted
    assert destination.read_bytes() == expected
    assert not (tmp_path / "detached.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="native Windows handle metadata contract")
def test_windows_copy_preserves_exact_supported_file_and_directory_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    file = source / "value.txt"
    file.write_text("value\n", encoding="utf-8")
    os.utime(file, ns=(2_000_000_021_000_000_000, 22_000_000_000))
    os.utime(source, ns=(2_000_000_023_000_000_000, 24_000_000_000))
    expected_file = file.lstat()
    expected_directory = source.lstat()
    published: set[Path] = set()
    published_file = False
    original_verify = filesystem_module._verify_windows_entry_metadata
    original_apply_descriptor = filesystem_module._apply_descriptor_metadata

    def verify_published_metadata(path: Path, handle: int, metadata) -> None:
        original_verify(path, handle, metadata)
        attributes, atime_ns, mtime_ns = (
            filesystem_module._windows_handle_metadata_values(handle)
        )
        assert atime_ns == metadata.st_atime_ns // 100 * 100
        assert mtime_ns == metadata.st_mtime_ns // 100 * 100
        if stat.S_ISREG(metadata.st_mode):
            assert bool(attributes & 0x1) != bool(metadata.st_mode & stat.S_IWRITE)
        filesystem_module._windows_apply_handle_metadata_values(
            handle,
            mode=None,
            regular=stat.S_ISREG(metadata.st_mode),
            atime_ns=metadata.st_atime_ns,
            mtime_ns=None,
        )
        published.add(path)

    def verify_published_file(descriptor: int, metadata) -> None:
        nonlocal published_file
        original_apply_descriptor(descriptor, metadata)
        handle = filesystem_module._windows_descriptor_handle(descriptor)
        attributes, atime_ns, mtime_ns = (
            filesystem_module._windows_handle_metadata_values(handle)
        )
        assert atime_ns == metadata.st_atime_ns // 100 * 100
        assert mtime_ns == metadata.st_mtime_ns // 100 * 100
        assert bool(attributes & 0x1) != bool(metadata.st_mode & stat.S_IWRITE)
        filesystem_module._windows_apply_handle_metadata_values(
            handle,
            mode=None,
            regular=True,
            atime_ns=metadata.st_atime_ns,
            mtime_ns=None,
        )
        published_file = True

    monkeypatch.setattr(
        filesystem_module,
        "_verify_windows_entry_metadata",
        verify_published_metadata,
    )
    monkeypatch.setattr(
        filesystem_module,
        "_apply_descriptor_metadata",
        verify_published_file,
    )

    destination = tmp_path / "destination"
    copy_entry_no_follow(source, destination)

    copied_directory = destination.lstat()
    copied_file = (destination / "value.txt").lstat()
    assert published == {destination}
    assert published_file
    assert (
        copied_file.st_mode,
        copied_file.st_mtime_ns,
    ) == (
        expected_file.st_mode,
        expected_file.st_mtime_ns,
    )
    assert (
        copied_directory.st_mode,
        copied_directory.st_mtime_ns,
    ) == (
        expected_directory.st_mode,
        expected_directory.st_mtime_ns,
    )
    assert (destination / "value.txt").read_bytes() == file.read_bytes()
