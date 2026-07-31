from pathlib import Path

from supernote_module_generator.toolchains import _choose, _version_tuple


def test_choose_single_candidate_without_prompt(tmp_path: Path):
    candidate = tmp_path / "jdk"; candidate.mkdir()
    assert _choose("JDK 17", [candidate], None, True, lambda _: "") == candidate.resolve()


def test_choose_multiple_candidate_by_number(tmp_path: Path):
    first = tmp_path / "one"; second = tmp_path / "two"; first.mkdir(); second.mkdir()
    assert _choose("JDK 17", [first, second], None, True, lambda _: "2") == second.resolve()


def test_choose_multiple_candidate_with_interactive_selector(tmp_path: Path):
    first = tmp_path / "one"; second = tmp_path / "two"; first.mkdir(); second.mkdir()
    calls = []

    def select(label, candidates, preferred):
        calls.append((label, candidates, preferred))
        return candidates[1]

    assert _choose("JDK 17", [first, second], None, True, lambda _: "", select=select) == second.resolve()
    assert calls == [("JDK 17", [first.resolve(), second.resolve()], first.resolve())]


def test_version_tuple_handles_android_tool_versions():
    assert _version_tuple("cmake version 3.22.1") == (3, 22, 1)
    assert _version_tuple("not installed") == ()
