"""Tests for the shared Git-safety foundation (ADR 0006).

This is the smallest reviewable slice: environment sanitization, the
hardened baseline argv, the Git >= 2.45 / --no-lazy-fetch preflight,
bounded filter-driver enumeration/neutralization, and NUL-safe
check-attr output parsing. Nothing here is wired into workspace.py,
patch.py, checkpoint_ref.py, or the controller yet.

Real throwaway Git repositories are used wherever real Git semantics
are being relied on (config enumeration, check-attr parsing). Only the
failure paths that cannot be provoked with the real installed Git (an
unsupported version, a hung process, malformed output) monkeypatch the
single `_run` seam, matching `test_checkpoint_ref.py`'s convention.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from codeagent import _git_safety
from codeagent._git_safety import (
    ATTR_CHECK_CHUNK_SIZE,
    BASELINE_ARGS,
    FIXED_CAT_PATH,
    MAX_FILTER_ARGV_BYTES,
    MAX_FILTER_DRIVER_NAME_BYTES,
    MAX_FILTER_DRIVERS,
    MAX_TRACKED_PATH_BYTES,
    MAX_TRACKED_PATHS,
    MIN_GIT_VERSION,
    AttributeRecord,
    FilterNeutralization,
    GitSafetyError,
    GitSafetyFailure,
    check_filter_attribute_for_paths,
    check_git_preflight,
    enumerate_filter_neutralization,
    evaluate_tracked_filter_safety,
    git_environment,
    is_safe_filter_state,
    is_safe_non_filter_state,
    list_tracked_paths,
    parse_check_attr_output,
    run_git,
)

# Deliberately reached for through the module: argv construction is a
# private seam so that `run_git` is the only public execution API an
# integrating production module is offered (a caller that built argv
# itself could pass it to a bare subprocess call and silently lose the
# sanitized environment).
_build_git_argv = _git_safety._build_git_argv

_GIT_IDENTITY = [
    "-c",
    "user.name=CodeAgent Test",
    "-c",
    "user.email=codeagent-test@example.invalid",
]


def _clean_env() -> dict[str, str]:
    """Independent test oracle, computed without depending on the
    module under test."""
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, *args],
        check=check,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", str(path)], check=True, capture_output=True, text=True, env=_clean_env()
    )
    return path


@pytest.fixture(autouse=True)
def isolated_git_user_config(tmp_path_factory, monkeypatch):
    """Isolate every test's Git invocations — both the test oracle's
    `_git()`/`_make_repo()` and the production `run_git`/`_run` seam —
    from this machine's or CI runner's real user-level Git
    configuration.

    This is a real, previously-hit failure mode: GitHub's
    `ubuntu-24.04` runner image registers a global
    `filter.lfs.{clean,smudge,process}` driver (Git LFS ships
    pre-installed and globally configured), which silently joined every
    test repository's enumerated `filter.*` config and broke
    driver-count assertions in CI while this suite passed locally,
    where no such global config exists.

    Each test gets a fresh `HOME` and `XDG_CONFIG_HOME`, covering both
    locations Git may read global config from (`$HOME/.gitconfig` and
    `$XDG_CONFIG_HOME/git/config`).

    `GIT_CONFIG_NOSYSTEM` is deliberately **not** set: production
    intentionally reads system-level Git configuration (ADR 0006 does
    not exempt it, and `_git_safety.git_environment()` only strips
    `GIT_*` variables), so a test-only `NOSYSTEM` override would
    exercise behavior no production invocation ever runs under. Only
    the *user*-level configuration locations are isolated here.
    """
    home = tmp_path_factory.mktemp("isolated-home")
    xdg_config_home = tmp_path_factory.mktemp("isolated-xdg-config")
    home.mkdir(parents=True, exist_ok=True)
    xdg_config_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))


@pytest.fixture
def ambient_filter_baseline(tmp_path_factory) -> FilterNeutralization:
    """Whatever `enumerate_filter_neutralization` discovers for a
    completely unconfigured repository under this test's isolated
    `HOME` — i.e., configuration this suite does not and must not
    control, such as a real ambient **system-level** filter driver
    (GitHub's `ubuntu-24.04` runner image ships Git LFS pre-installed
    via `git lfs install --system`, which registers `filter.lfs.*` in
    `/etc/gitconfig`).

    `isolated_git_user_config` isolates `HOME`/`XDG_CONFIG_HOME` (the
    *user*-level config locations) but deliberately leaves system-level
    configuration untouched, since production reads it intentionally.
    Tests that configure their own driver(s) must therefore diff
    against this baseline instead of asserting an absolute empty/exact
    driver set, which would be false whenever the environment running
    this suite has any real ambient system-level filter — exactly the
    condition that broke this file's first version of these tests in
    CI (see ENGINEERING_LOG.md).
    """
    baseline_repo = _make_repo(tmp_path_factory.mktemp("ambient-baseline"))
    return enumerate_filter_neutralization(baseline_repo)


# ---------------------------------------------------------------------------
# Environment sanitization
# ---------------------------------------------------------------------------


def test_git_environment_strips_all_git_prefixed_variables(monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/hostile/path")
    monkeypatch.setenv("GIT_WORK_TREE", "/hostile/tree")
    monkeypatch.setenv("GIT_NAMESPACE", "hostile")

    env = git_environment()

    deliberately_set = {"GIT_NO_LAZY_FETCH", "GIT_NO_REPLACE_OBJECTS", "GIT_LITERAL_PATHSPECS"}
    assert not any(key.startswith("GIT_") and key not in deliberately_set for key in env)


def test_git_environment_preserves_non_git_variables(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HOME", "/home/tester")

    env = git_environment()

    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HOME"] == "/home/tester"


def test_git_environment_sets_no_lazy_fetch():
    env = git_environment()

    assert env["GIT_NO_LAZY_FETCH"] == "1"


def test_git_environment_overrides_a_hostile_no_lazy_fetch_value(monkeypatch):
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")

    env = git_environment()

    assert env["GIT_NO_LAZY_FETCH"] == "1"


def test_git_environment_sets_no_replace_objects():
    env = git_environment()

    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"


def test_git_environment_sets_literal_pathspecs():
    env = git_environment()

    assert env["GIT_LITERAL_PATHSPECS"] == "1"


# ---------------------------------------------------------------------------
# Hardened baseline argv
# ---------------------------------------------------------------------------


def test_baseline_args_contains_hooks_path_null():
    assert "-c" in BASELINE_ARGS
    idx = BASELINE_ARGS.index("core.hooksPath=/dev/null")
    assert BASELINE_ARGS[idx - 1] == "-c"


def test_baseline_args_contains_fsmonitor_false_explicit_boolean():
    assert "core.fsmonitor=false" in BASELINE_ARGS


def test_baseline_args_contains_autocrlf_false():
    assert "core.autocrlf=false" in BASELINE_ARGS


def test_baseline_args_contains_submodule_recurse_false():
    assert "core.submodule.recurse=false" in BASELINE_ARGS or "submodule.recurse=false" in BASELINE_ARGS


def test_baseline_args_contains_no_pager_flag():
    assert "--no-pager" in BASELINE_ARGS


def test_baseline_args_contains_no_lazy_fetch_flag():
    assert "--no-lazy-fetch" in BASELINE_ARGS


def test_baseline_args_contains_no_replace_objects_flag():
    assert "--no-replace-objects" in BASELINE_ARGS


def test_baseline_args_contains_literal_pathspecs_flag():
    assert "--literal-pathspecs" in BASELINE_ARGS


def test_build_git_argv_without_repo_has_no_dash_c_repo_flag():
    argv = _build_git_argv(None, "--version")

    assert argv[0] == "git"
    assert "-C" not in argv
    assert argv[-1] == "--version"
    assert all(flag in argv for flag in BASELINE_ARGS)


def test_build_git_argv_with_repo_inserts_dash_c_before_extra_args(tmp_path):
    argv = _build_git_argv(tmp_path, "status", "--porcelain")

    c_index = argv.index("-C")
    assert argv[c_index + 1] == str(tmp_path)
    assert argv[-2:] == ["status", "--porcelain"]
    # -C must come after the baseline flags but before the command.
    assert argv.index("status") > c_index


# ---------------------------------------------------------------------------
# Git >= 2.45 / --no-lazy-fetch preflight
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("git version 2.54.0", (2, 54)),
        ("git version 2.54.0 (Apple Git-157)", (2, 54)),
        ("git version 2.45.0.windows.1", (2, 45)),
        ("git version 2.30.2 (Debian)", (2, 30)),
        ("git version 2.9", (2, 9)),
        ("git version 2.7.0.rc0", (2, 7)),
    ],
)
def test_parse_git_version_handles_platform_suffixed_strings(text, expected):
    assert _git_safety._parse_git_version(text) == expected


@pytest.mark.parametrize(
    "text",
    ["git version 2", "GIT VERSION 2.54.0", "", "not a git version string"],
)
def test_parse_git_version_rejects_malformed_strings(text):
    assert _git_safety._parse_git_version(text) is None


def test_parse_git_version_compares_numerically_not_lexicographically():
    # A naive string comparison would put "2.9" above "2.45" ('9' > '4'
    # lexicographically); the tuple-of-int comparison must not do that.
    assert _git_safety._parse_git_version("git version 2.9.0") < MIN_GIT_VERSION
    assert _git_safety._parse_git_version("git version 2.100.0") > MIN_GIT_VERSION


def test_check_git_preflight_succeeds_against_the_real_installed_git():
    # No mocking: this proves the real installed Git (must be >= 2.45
    # for this suite to mean anything) actually recognizes
    # --no-lazy-fetch, not merely that a canned response is accepted.
    check_git_preflight()


def test_check_git_preflight_rejects_a_version_below_2_45(monkeypatch):
    def fake_run(args, **kwargs):
        assert args == ["--version"]
        return subprocess.CompletedProcess(args, 0, stdout="git version 2.44.0\n", stderr="")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_git_preflight()

    assert excinfo.value.reason is GitSafetyFailure.UNSUPPORTED_GIT_VERSION


def test_check_git_preflight_accepts_exactly_the_minimum_version(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(list(args))
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="git version 2.45.0\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="git version 2.45.0\n", stderr="")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    check_git_preflight()

    assert len(calls) == 2


def test_check_git_preflight_rejects_malformed_version_output(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="not a git version string\n", stderr="")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_git_preflight()

    assert excinfo.value.reason is GitSafetyFailure.MALFORMED_VERSION_OUTPUT


def test_check_git_preflight_rejects_unrecognized_no_lazy_fetch_option(monkeypatch):
    def fake_run(args, **kwargs):
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="git version 2.54.0\n", stderr="")
        # A version that claims >= 2.45 but whose --no-lazy-fetch call
        # still fails (broken build, distro patch, spoofed string).
        return subprocess.CompletedProcess(
            args, 129, stdout="", stderr="error: unknown option `no-lazy-fetch'\n"
        )

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_git_preflight()

    assert excinfo.value.reason is GitSafetyFailure.UNRECOGNIZED_LAZY_FETCH_OPTION


def test_check_git_preflight_error_message_has_no_raw_stderr(monkeypatch):
    def fake_run(args, **kwargs):
        if args == ["--version"]:
            return subprocess.CompletedProcess(args, 0, stdout="git version 2.54.0\n", stderr="")
        return subprocess.CompletedProcess(
            args, 129, stdout="", stderr="/Users/secret/host/path leaked here\n"
        )

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_git_preflight()

    assert "/Users/secret" not in excinfo.value.message
    assert "leaked" not in excinfo.value.message


def test_check_git_preflight_translates_a_launch_failure(monkeypatch):
    def fake_run(args, **kwargs):
        raise GitSafetyError(
            GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE, "the git executable could not be launched"
        )

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_git_preflight()

    assert excinfo.value.reason is GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE


def test_run_raises_timeout_as_categorical_failure(monkeypatch):
    def fake_subprocess_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=1.0)

    monkeypatch.setattr(_git_safety.subprocess, "run", fake_subprocess_run)

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety._run(["--version"])

    assert excinfo.value.reason is GitSafetyFailure.GIT_COMMAND_TIMEOUT


def test_run_raises_executable_unavailable_on_os_error(monkeypatch):
    def fake_subprocess_run(*args, **kwargs):
        raise FileNotFoundError("no such file: git")

    monkeypatch.setattr(_git_safety.subprocess, "run", fake_subprocess_run)

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety._run(["--version"])

    assert excinfo.value.reason is GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE


def test_run_uses_sanitized_environment(monkeypatch, tmp_path):
    captured_env = {}

    def fake_subprocess_run(argv, **kwargs):
        captured_env.update(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("GIT_DIR", "/hostile")
    monkeypatch.setattr(_git_safety.subprocess, "run", fake_subprocess_run)

    _git_safety._run(["--version"])

    assert "GIT_DIR" not in captured_env
    assert captured_env["GIT_NO_LAZY_FETCH"] == "1"


# ---------------------------------------------------------------------------
# Filter-driver enumeration and neutralization
# ---------------------------------------------------------------------------


def test_isolated_git_user_config_fixture_starts_with_no_global_filter_config():
    """Characterization test for the autouse isolation fixture itself:
    the isolated `HOME`'s `--global` scope must have zero configured
    filter drivers before any test writes anything.

    This deliberately tests **global** scope specifically, not the
    merged view `enumerate_filter_neutralization` returns — the fixture
    isolates `HOME`/`XDG_CONFIG_HOME` (user-level config) but leaves
    system-level configuration untouched on purpose (production reads
    it intentionally), so a real ambient system-level driver (e.g.
    GitHub's runner-image `filter.lfs`) can still legitimately appear
    in the merged view without this fixture having failed."""
    result = subprocess.run(
        ["git", "config", "--global", "--get-regexp", r"^filter\."],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    assert result.returncode == 1  # git's "no matching config" exit code
    assert result.stdout == ""


def test_enumerate_filter_neutralization_discovers_a_global_driver(
    tmp_path, ambient_filter_baseline
):
    """Positive control pinning that global Git configuration remains
    part of production behavior: this module must not accidentally
    become blind to `--global` filter configuration merely because the
    tests isolate *which* global config file is read. The repository
    itself has no local filter configuration at all."""
    repo = _make_repo(tmp_path / "r")
    subprocess.run(
        ["git", "config", "--global", "filter.globaldriver.clean", "some-command"],
        check=True,
        capture_output=True,
        text=True,
        env=_clean_env(),
    )

    result = enumerate_filter_neutralization(repo)

    assert result.driver_names - ambient_filter_baseline.driver_names == {"globaldriver"}
    assert f"filter.globaldriver.clean={FIXED_CAT_PATH}" in result.args


def test_enumerate_filter_neutralization_empty_repo_has_no_overrides(
    tmp_path, ambient_filter_baseline
):
    """A repository with no filter configuration of its own introduces
    no driver beyond whatever this environment's real ambient
    (system-level, not isolated by design) configuration already
    contributes."""
    repo = _make_repo(tmp_path / "r")

    result = enumerate_filter_neutralization(repo)

    assert result == ambient_filter_baseline


def test_enumerate_filter_neutralization_overrides_clean_and_smudge(
    tmp_path, ambient_filter_baseline
):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "some-hostile-command")
    _git(repo, "config", "filter.hostile.smudge", "another-hostile-command")

    result = enumerate_filter_neutralization(repo)

    assert result.driver_names - ambient_filter_baseline.driver_names == {"hostile"}
    assert "-c" in result.args
    assert f"filter.hostile.clean={FIXED_CAT_PATH}" in result.args
    assert f"filter.hostile.smudge={FIXED_CAT_PATH}" in result.args


def test_enumerate_filter_neutralization_clears_process_only_when_configured(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd")
    _git(repo, "config", "filter.hostile.process", "long-running-hostile-process")

    result = enumerate_filter_neutralization(repo)

    assert "filter.hostile.process=" in result.args


def test_enumerate_filter_neutralization_never_touches_process_when_absent(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd")

    result = enumerate_filter_neutralization(repo)

    assert not any(arg.startswith("filter.hostile.process=") for arg in result.args)


def test_enumerate_filter_neutralization_never_sets_required_false(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd")
    _git(repo, "config", "filter.hostile.required", "true")

    result = enumerate_filter_neutralization(repo)

    assert not any("required" in arg for arg in result.args)


def test_enumerate_filter_neutralization_handles_dotted_driver_names(
    tmp_path, ambient_filter_baseline
):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.weird.name.clean", "cmd")

    result = enumerate_filter_neutralization(repo)

    assert result.driver_names - ambient_filter_baseline.driver_names == {"weird.name"}
    assert f"filter.weird.name.clean={FIXED_CAT_PATH}" in result.args


def test_enumerate_filter_neutralization_deduplicates_exact_driver_names(
    tmp_path, ambient_filter_baseline
):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd1")
    _git(repo, "config", "filter.hostile.smudge", "cmd2")

    result = enumerate_filter_neutralization(repo)

    assert result.driver_names - ambient_filter_baseline.driver_names == {"hostile"}


def test_enumerate_filter_neutralization_rejects_too_many_drivers(tmp_path):
    repo = _make_repo(tmp_path / "r")
    config_path = repo / ".git" / "config"
    with config_path.open("a") as handle:
        for i in range(MAX_FILTER_DRIVERS + 1):
            handle.write(f"[filter \"d{i}\"]\n\tclean = cmd\n")

    with pytest.raises(GitSafetyError) as excinfo:
        enumerate_filter_neutralization(repo)

    assert excinfo.value.reason is GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED


def test_enumerate_filter_neutralization_rejects_a_too_long_driver_name(tmp_path):
    repo = _make_repo(tmp_path / "r")
    long_name = "x" * (MAX_FILTER_DRIVER_NAME_BYTES + 1)
    _git(repo, "config", f"filter.{long_name}.clean", "cmd")

    with pytest.raises(GitSafetyError) as excinfo:
        enumerate_filter_neutralization(repo)

    assert excinfo.value.reason is GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED


def test_enumerate_filter_neutralization_rejects_oversized_total_argv(tmp_path):
    repo = _make_repo(tmp_path / "r")
    config_path = repo / ".git" / "config"
    # Each driver contributes ~3 overrides of up to ~256-byte names;
    # comfortably exceed 65536 bytes total without exceeding the
    # driver-count bound.
    name_len = MAX_FILTER_DRIVER_NAME_BYTES
    driver_count = 80
    with config_path.open("a") as handle:
        for i in range(driver_count):
            name = f"{i:03d}" + "y" * (name_len - 3)
            handle.write(f'[filter "{name}"]\n\tclean = cmd\n\tsmudge = cmd\n\tprocess = cmd\n')

    with pytest.raises(GitSafetyError) as excinfo:
        enumerate_filter_neutralization(repo)

    assert excinfo.value.reason is GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED


def test_enumerate_filter_neutralization_malformed_output_is_categorical(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")

    def fake_run(args, **kwargs):
        # Missing the value half of a key/value NUL-separated pair.
        return subprocess.CompletedProcess(args, 0, stdout="filter.hostile.clean\0dangling", stderr="")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        enumerate_filter_neutralization(repo)

    assert excinfo.value.reason is GitSafetyFailure.FILTER_ENUMERATION_MALFORMED


def test_enumerate_filter_neutralization_handles_multi_valued_subkey(
    tmp_path, ambient_filter_baseline
):
    """`git config --add` can give one subkey multiple values; --get-
    regexp reports each as a separate record for the same key. Only
    presence matters here (the original value is never reused), so
    this must not crash or duplicate the override."""
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "first-value")
    _git(repo, "config", "--add", "filter.hostile.clean", "second-value")

    result = enumerate_filter_neutralization(repo)

    assert result.driver_names - ambient_filter_baseline.driver_names == {"hostile"}
    assert result.args.count(f"filter.hostile.clean={FIXED_CAT_PATH}") == 1


def test_enumerate_filter_neutralization_ignores_unrelated_keys(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd")
    _git(repo, "config", "filter.hostile.required", "true")

    result = enumerate_filter_neutralization(repo)

    # Only clean/smudge/process ever produce overrides; required is
    # read but never emitted.
    assert all("required" not in arg for arg in result.args)


def test_enumerate_filter_neutralization_real_command_never_executes_hostile_filter(tmp_path):
    """End-to-end proof: neutralized overrides actually stop a real
    hostile clean filter from running during `git add`."""
    repo = _make_repo(tmp_path / "r")
    marker = tmp_path / "marker.log"
    _git(repo, "config", "filter.hostile.clean", f"sh -c 'echo RAN >> {marker}; cat'")
    (repo / ".gitattributes").write_text("a.txt filter=hostile\n")
    (repo / "a.txt").write_text("content\n")
    _git(repo, "add", ".gitattributes")

    result = enumerate_filter_neutralization(repo)
    # -c overrides must precede the subcommand (and -C) to take effect.
    full_argv = ["git", *BASELINE_ARGS, *result.args, "-C", str(repo), "add", "a.txt"]
    subprocess.run(full_argv, check=True, capture_output=True, text=True, env=git_environment())

    assert not marker.exists()


# ---------------------------------------------------------------------------
# NUL-safe check-attr parsing and attribute-state safety classification
# ---------------------------------------------------------------------------


def test_is_safe_non_filter_state_unspecified_is_safe():
    assert is_safe_non_filter_state("unspecified") is True


def test_is_safe_non_filter_state_unset_is_safe():
    assert is_safe_non_filter_state("unset") is True


def test_is_safe_non_filter_state_set_is_unsafe():
    assert is_safe_non_filter_state("set") is False


def test_is_safe_non_filter_state_named_value_is_unsafe():
    assert is_safe_non_filter_state("hostile-driver") is False
    assert is_safe_non_filter_state("UTF-16") is False
    assert is_safe_non_filter_state("crlf") is False


# ---------------------------------------------------------------------------
# Filter-state classification is driver-set dependent, never context-free
#
# `git check-attr` prints the literal string "unset" both for a
# genuinely unset attribute (`-filter`) and for an explicit assignment
# naming a driver called "unset" (`filter=unset`) -- and likewise for
# "unspecified". Proven with real positive controls below: the b/d
# drivers execute while a/c do not, yet all four report identical
# strings. So the filter attribute cannot be classified from check-attr
# output alone.
# ---------------------------------------------------------------------------


def test_is_safe_filter_state_unset_is_safe_when_no_such_driver():
    assert is_safe_filter_state("unset", configured_driver_names=frozenset()) is True


def test_is_safe_filter_state_unspecified_is_safe_when_no_such_driver():
    assert is_safe_filter_state("unspecified", configured_driver_names=frozenset()) is True


def test_is_safe_filter_state_unset_is_unsafe_when_a_driver_is_named_unset():
    assert is_safe_filter_state("unset", configured_driver_names=frozenset({"unset"})) is False


def test_is_safe_filter_state_unspecified_is_unsafe_when_a_driver_is_named_unspecified():
    assert (
        is_safe_filter_state("unspecified", configured_driver_names=frozenset({"unspecified"}))
        is False
    )


def test_is_safe_filter_state_set_is_always_unsafe():
    assert is_safe_filter_state("set", configured_driver_names=frozenset()) is False


def test_is_safe_filter_state_ordinary_named_driver_is_always_unsafe():
    assert is_safe_filter_state("hostile", configured_driver_names=frozenset()) is False
    assert is_safe_filter_state("hostile", configured_driver_names=frozenset({"hostile"})) is False


def test_is_safe_filter_state_unrelated_driver_names_do_not_cause_refusal():
    # Only an exact collision with the reported string is ambiguous.
    assert (
        is_safe_filter_state("unset", configured_driver_names=frozenset({"lfs", "hostile"}))
        is True
    )


def test_parse_check_attr_output_empty_string_yields_no_records():
    assert parse_check_attr_output("") == []


def test_parse_check_attr_output_parses_one_triple():
    raw = "a.txt\0filter\0unspecified\0"

    records = parse_check_attr_output(raw)

    assert records == [AttributeRecord(path="a.txt", attribute="filter", value="unspecified")]


def test_parse_check_attr_output_parses_multiple_records():
    raw = "a.txt\0filter\0set\0a.txt\0text\0unspecified\0b.txt\0eol\0crlf\0"

    records = parse_check_attr_output(raw)

    assert records == [
        AttributeRecord(path="a.txt", attribute="filter", value="set"),
        AttributeRecord(path="a.txt", attribute="text", value="unspecified"),
        AttributeRecord(path="b.txt", attribute="eol", value="crlf"),
    ]


def test_parse_check_attr_output_rejects_incomplete_trailing_record():
    raw = "a.txt\0filter\0set\0a.txt\0text\0"  # missing the third field

    with pytest.raises(GitSafetyError) as excinfo:
        parse_check_attr_output(raw)

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED


def test_attribute_record_is_safe_for_safe_states():
    none_configured = frozenset()
    assert (
        AttributeRecord(path="a.txt", attribute="filter", value="unspecified").is_safe(
            configured_driver_names=none_configured
        )
        is True
    )
    assert (
        AttributeRecord(path="a.txt", attribute="filter", value="unset").is_safe(
            configured_driver_names=none_configured
        )
        is True
    )


def test_attribute_record_is_safe_for_unsafe_states():
    none_configured = frozenset()
    assert (
        AttributeRecord(path="a.txt", attribute="filter", value="set").is_safe(
            configured_driver_names=none_configured
        )
        is False
    )
    assert (
        AttributeRecord(path="a.txt", attribute="filter", value="hostile").is_safe(
            configured_driver_names=none_configured
        )
        is False
    )


def test_attribute_record_is_safe_requires_the_driver_set_keyword():
    """A caller must not be able to classify a filter record without
    having enumerated the configured drivers first."""
    record = AttributeRecord(path="a.txt", attribute="filter", value="unset")

    with pytest.raises(TypeError):
        record.is_safe()  # type: ignore[call-arg]


def test_attribute_record_filter_is_unsafe_when_driver_name_collides():
    record = AttributeRecord(path="b.txt", attribute="filter", value="unset")

    assert record.is_safe(configured_driver_names=frozenset({"unset"})) is False


def test_attribute_record_non_filter_attribute_ignores_the_driver_set():
    """A driver named "unset" says nothing about the `text` attribute;
    only `filter` resolves its value as a driver name."""
    record = AttributeRecord(path="a.txt", attribute="text", value="unset")

    assert record.is_safe(configured_driver_names=frozenset({"unset"})) is True


def test_enumerate_filter_neutralization_handles_bare_boolean_subkey(
    tmp_path, ambient_filter_baseline
):
    """`git config -z --get-regexp` emits a bare-boolean entry (e.g.
    `[filter "x"]\\n\\tclean` with no `=value`) as `key\\0` with no
    embedded newline at all -- a real, valid git config shape, not
    malformed input. It must still be recognized as "clean is
    configured for this driver", not rejected."""
    repo = _make_repo(tmp_path / "r")
    config_path = repo / ".git" / "config"
    with config_path.open("a") as handle:
        handle.write('[filter "boolonly"]\n\tclean\n')

    result = enumerate_filter_neutralization(repo)

    assert result.driver_names - ambient_filter_baseline.driver_names == {"boolonly"}
    assert f"filter.boolonly.clean={FIXED_CAT_PATH}" in result.args


def test_enumerate_filter_neutralization_survives_non_utf8_driver_name(
    tmp_path, ambient_filter_baseline
):
    """A filter driver name is an arbitrary quoted string in Git's
    config file grammar and can contain bytes that are not valid UTF-8.
    Enumeration must not crash with an uncaught UnicodeDecodeError; it
    must either neutralize it or raise a categorical GitSafetyError."""
    repo = _make_repo(tmp_path / "r")
    config_path = repo / ".git" / "config"
    with config_path.open("ab") as handle:
        handle.write(b'[filter "bad\xff\xfename"]\n\tclean = /bin/cat\n')

    # Must not raise UnicodeDecodeError (or any exception other than
    # GitSafetyError).
    try:
        result = enumerate_filter_neutralization(repo)
    except GitSafetyError:
        return
    assert len(result.driver_names - ambient_filter_baseline.driver_names) == 1


def test_run_timeout_does_not_chain_the_full_argv_into_cause(monkeypatch):
    """subprocess.TimeoutExpired's own str() embeds the full argv,
    which can include a real host repository path (`-C <repo>`). The
    raised GitSafetyError must not chain that exception as __cause__,
    so a bare traceback print can never surface a filesystem path."""

    def fake_subprocess_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "-C", "/secret/host/path"], timeout=1.0)

    monkeypatch.setattr(_git_safety.subprocess, "run", fake_subprocess_run)

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety._run(["--version"])

    assert excinfo.value.__cause__ is None


def test_enumerate_filter_neutralization_fails_closed_when_cat_missing(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd")
    monkeypatch.setattr(_git_safety, "FIXED_CAT_PATH", "/nonexistent/definitely/not/cat")

    with pytest.raises(GitSafetyError) as excinfo:
        enumerate_filter_neutralization(repo)

    assert excinfo.value.reason is GitSafetyFailure.FILTER_PASSTHROUGH_UNAVAILABLE


def test_enumerate_filter_neutralization_skips_cat_check_when_nothing_to_neutralize(
    tmp_path, ambient_filter_baseline
):
    """No driver needs clean/smudge overridden (e.g. only `required` is
    set) -- the passthrough executable must not be required to exist."""
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.required", "true")

    result = enumerate_filter_neutralization(repo)

    assert result.driver_names - ambient_filter_baseline.driver_names == {"hostile"}
    assert not any(arg.startswith("filter.hostile.") for arg in result.args)


def test_check_git_preflight_distinguishes_command_failure_from_malformed_output(monkeypatch):
    """A nonzero exit from `git --version` itself (binary broken,
    permissions issue) is a different failure than a zero exit with
    output that fails to parse -- they must not share a reason."""

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, stdout="", stderr="fatal: something")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_git_preflight()

    assert excinfo.value.reason is GitSafetyFailure.GIT_VERSION_CHECK_FAILED
    assert excinfo.value.reason is not GitSafetyFailure.MALFORMED_VERSION_OUTPUT


def test_run_git_composes_baseline_argv_and_sanitized_environment(monkeypatch, tmp_path):
    """The public composed entrypoint must apply both the baseline argv
    and the sanitized environment in one call, so an integrating caller
    cannot accidentally use one without the other."""
    captured = {}

    def fake_subprocess_run(argv, **kwargs):
        captured["argv"] = argv
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("GIT_DIR", "/hostile")
    monkeypatch.setattr(_git_safety.subprocess, "run", fake_subprocess_run)

    _git_safety.run_git(tmp_path, "status", "--porcelain")

    assert "--no-lazy-fetch" in captured["argv"]
    assert "-c" in captured["argv"]
    assert "-C" in captured["argv"]
    assert str(tmp_path) in captured["argv"]
    assert "GIT_DIR" not in captured["env"]
    assert captured["env"]["GIT_NO_LAZY_FETCH"] == "1"


def _make_magic_driver_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repository whose filter drivers are *named* after check-attr's
    own output strings, so `filter=unset` and `filter=unspecified` are
    real, live, executing drivers."""
    repo = _make_repo(tmp_path / "r")
    marker = tmp_path / "marker.log"
    driver = tmp_path / "driver.sh"
    driver.write_text(f'#!/bin/sh\necho "DRIVER-RAN:$1" >> "{marker}"\ncat\n')
    driver.chmod(0o755)

    (repo / ".gitattributes").write_text(
        "a.txt -filter\nb.txt filter=unset\nc.txt text\nd.txt filter=unspecified\n"
    )
    for name in ("a", "b", "c", "d"):
        (repo / f"{name}.txt").write_text(f"content of {name}\n")
    _git(repo, "config", "filter.unset.clean", f"{driver} unset-driver")
    _git(repo, "config", "filter.unspecified.clean", f"{driver} unspecified-driver")
    _git(repo, "add", ".gitattributes")
    return repo, marker


def _filter_states(repo: Path) -> dict[str, str]:
    result = run_git(
        repo, "check-attr", "--cached", "-z", "--stdin", "filter", input_text="a.txt\0b.txt\0c.txt\0d.txt\0"
    )
    return {r.path: r.value for r in parse_check_attr_output(result.stdout)}


def test_check_attr_reports_identical_unset_for_genuine_and_named_driver(tmp_path):
    repo, _marker = _make_magic_driver_repo(tmp_path)

    states = _filter_states(repo)

    # a.txt is genuinely `-filter`; b.txt names a live driver called
    # "unset". check-attr cannot tell them apart.
    assert states["a.txt"] == "unset"
    assert states["b.txt"] == "unset"


def test_check_attr_reports_identical_unspecified_for_genuine_and_named_driver(tmp_path):
    repo, _marker = _make_magic_driver_repo(tmp_path)

    states = _filter_states(repo)

    assert states["c.txt"] == "unspecified"
    assert states["d.txt"] == "unspecified"


def test_named_unset_driver_really_executes_when_unprotected(tmp_path):
    """Positive control: the `unset`-named driver is live, so a
    classification that trusted check-attr's string would permit host
    code execution."""
    repo, marker = _make_magic_driver_repo(tmp_path)

    _git(repo, "add", "b.txt")

    assert marker.exists()
    assert "unset-driver" in marker.read_text()


def test_named_unspecified_driver_really_executes_when_unprotected(tmp_path):
    repo, marker = _make_magic_driver_repo(tmp_path)

    _git(repo, "add", "d.txt")

    assert marker.exists()
    assert "unspecified-driver" in marker.read_text()


def test_genuinely_unset_and_unspecified_paths_do_not_execute_a_driver(tmp_path):
    """Negative control: the same live drivers must not fire for the
    paths that are genuinely unset / unspecified, which is exactly why
    the two cases are indistinguishable from the output string alone."""
    repo, marker = _make_magic_driver_repo(tmp_path)

    _git(repo, "add", "a.txt", "c.txt")

    assert not marker.exists()


def test_driver_set_dependent_classification_refuses_the_magic_names(tmp_path):
    """The corrected rule, end to end against the real repository: with
    the discovered driver-name set in hand, the genuinely-safe paths
    stay safe and the collided ones are refused."""
    repo, _marker = _make_magic_driver_repo(tmp_path)
    neutralization = enumerate_filter_neutralization(repo)
    states = _filter_states(repo)

    def classify(path: str) -> bool:
        return is_safe_filter_state(
            states[path], configured_driver_names=neutralization.driver_names
        )

    assert {"unset", "unspecified"} <= neutralization.driver_names
    # Conservative refusal: a.txt and c.txt are genuinely safe, but
    # indistinguishable from b.txt/d.txt, so all four are refused.
    assert classify("a.txt") is False
    assert classify("b.txt") is False
    assert classify("c.txt") is False
    assert classify("d.txt") is False


def test_classification_stays_permissive_without_the_magic_driver_names(tmp_path):
    """The same paths classify as safe once no driver claims those
    names -- the refusal above is caused by the collision, not by the
    attribute states themselves."""
    repo = _make_repo(tmp_path / "r")
    (repo / ".gitattributes").write_text("a.txt -filter\nc.txt text\n")
    (repo / "a.txt").write_text("x\n")
    (repo / "c.txt").write_text("y\n")
    _git(repo, "config", "filter.ordinary.clean", "some-command")
    _git(repo, "add", ".gitattributes", "a.txt", "c.txt")

    neutralization = enumerate_filter_neutralization(repo)
    result = run_git(
        repo, "check-attr", "--cached", "-z", "--stdin", "filter", input_text="a.txt\0c.txt\0"
    )
    states = {r.path: r.value for r in parse_check_attr_output(result.stdout)}

    assert {"ordinary"} <= neutralization.driver_names
    assert is_safe_filter_state(states["a.txt"], configured_driver_names=neutralization.driver_names)
    assert is_safe_filter_state(states["c.txt"], configured_driver_names=neutralization.driver_names)


def test_filter_neutralization_driver_names_is_immutable(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd")

    result = enumerate_filter_neutralization(repo)

    assert isinstance(result.driver_names, frozenset)
    with pytest.raises(AttributeError):
        result.driver_names.add("smuggled")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# NUL framing hardening
# ---------------------------------------------------------------------------


def test_parse_check_attr_output_rejects_missing_terminal_nul():
    """Truncated output whose field count still happens to divide by
    three must not be accepted as complete."""
    raw = "a.txt\0filter\0set\0b.txt\0text\0unspecified"  # no trailing NUL

    with pytest.raises(GitSafetyError) as excinfo:
        parse_check_attr_output(raw)

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED


def test_parse_check_attr_output_rejects_empty_path():
    raw = "\0filter\0set\0"

    with pytest.raises(GitSafetyError) as excinfo:
        parse_check_attr_output(raw)

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED


def test_parse_check_attr_output_rejects_empty_attribute_name():
    raw = "a.txt\0\0set\0"

    with pytest.raises(GitSafetyError) as excinfo:
        parse_check_attr_output(raw)

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED


def test_parse_check_attr_output_preserves_surrogate_escaped_paths():
    raw = "bad\udcffname.txt\0filter\0unspecified\0"

    records = parse_check_attr_output(raw)

    assert records[0].path == "bad\udcffname.txt"


def test_enumerate_filter_neutralization_rejects_config_without_terminal_nul(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout="filter.hostile.clean\ncmd", stderr=""  # no trailing NUL
        )

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        enumerate_filter_neutralization(repo)

    assert excinfo.value.reason is GitSafetyFailure.FILTER_ENUMERATION_MALFORMED


@pytest.mark.parametrize(
    "text",
    [
        "git version 2.45evil",
        "git version 2.45.0evil",
        "git version 2.54.0 trailing junk",
        "git version 2.54.0\nextra line",
        "git version 2.54.0 (Apple Git-157) and more",
    ],
)
def test_parse_git_version_rejects_trailing_junk(text):
    assert _git_safety._parse_git_version(text) is None


def test_parse_check_attr_output_against_a_real_repository(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / ".gitattributes").write_text("a.txt filter=hostile\nb.txt -filter\n")
    (repo / "a.txt").write_text("x\n")
    (repo / "b.txt").write_text("y\n")
    _git(repo, "add", ".gitattributes", "a.txt", "b.txt")

    result = _git_safety._run(
        _build_git_argv(repo, "check-attr", "--cached", "-z", "--stdin", "filter")[1:],
        input_text="a.txt\0b.txt\0",
    )
    records = parse_check_attr_output(result.stdout)

    by_path = {record.path: record for record in records}
    configured = enumerate_filter_neutralization(repo).driver_names
    assert by_path["a.txt"].value == "hostile"
    assert by_path["a.txt"].is_safe(configured_driver_names=configured) is False
    assert by_path["b.txt"].value == "unset"
    assert by_path["b.txt"].is_safe(configured_driver_names=configured) is True


# ---------------------------------------------------------------------------
# Shared tracked-path listing + bounded/chunked filter-attribute inspection
# (used by workspace.py's pre-materialization refusal check)
# ---------------------------------------------------------------------------


def test_list_tracked_paths_empty_repo(tmp_path):
    repo = _make_repo(tmp_path / "r")

    assert list_tracked_paths(repo) == ()


def test_list_tracked_paths_returns_tracked_files(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("x\n")
    (repo / "b.txt").write_text("y\n")
    _git(repo, "add", "a.txt", "b.txt")

    paths = list_tracked_paths(repo)

    assert set(paths) == {"a.txt", "b.txt"}


def test_list_tracked_paths_ignores_untracked_files(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "tracked.txt").write_text("x\n")
    _git(repo, "add", "tracked.txt")
    (repo / "untracked.txt").write_text("y\n")

    paths = list_tracked_paths(repo)

    assert paths == ("tracked.txt",)


def _intercept_ls_files_popen(monkeypatch, tmp_path, *, stdout_bytes: bytes, returncode: int):
    """`list_tracked_paths` now runs through the bounded binary seam
    (real `subprocess.Popen`, not the text `_run` seam) -- these
    fixtures intercept `git ls-files` specifically and substitute a
    real short-lived process emitting exactly `stdout_bytes` then
    exiting with `returncode`, while every other git invocation (the
    fixture repo's own setup) is untouched."""
    real_popen = subprocess.Popen
    payload_file = tmp_path / "fake_ls_files_payload.bin"
    payload_file.write_bytes(stdout_bytes)

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"] and "ls-files" in argv:
            return real_popen(
                ["sh", "-c", f"cat {payload_file}; exit {returncode}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)


def test_list_tracked_paths_rejects_excessive_count(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")
    payload = "".join(f"path{i}\0" for i in range(MAX_TRACKED_PATHS + 1)).encode()
    _intercept_ls_files_popen(monkeypatch, tmp_path, stdout_bytes=payload, returncode=0)

    with pytest.raises(GitSafetyError) as excinfo:
        list_tracked_paths(repo)

    assert excinfo.value.reason is GitSafetyFailure.TRACKED_PATH_COUNT_EXCEEDED


def test_list_tracked_paths_rejects_a_too_long_path(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")
    payload = (("x" * (MAX_TRACKED_PATH_BYTES + 1)) + "\0").encode()
    _intercept_ls_files_popen(monkeypatch, tmp_path, stdout_bytes=payload, returncode=0)

    with pytest.raises(GitSafetyError) as excinfo:
        list_tracked_paths(repo)

    assert excinfo.value.reason is GitSafetyFailure.TRACKED_PATH_TOO_LONG


def test_list_tracked_paths_rejects_empty_path(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")
    _intercept_ls_files_popen(monkeypatch, tmp_path, stdout_bytes=b"a.txt\0\0", returncode=0)

    with pytest.raises(GitSafetyError) as excinfo:
        list_tracked_paths(repo)

    assert excinfo.value.reason is GitSafetyFailure.TRACKED_PATH_LISTING_MALFORMED


def test_list_tracked_paths_rejects_duplicate_path(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")
    _intercept_ls_files_popen(monkeypatch, tmp_path, stdout_bytes=b"a.txt\0a.txt\0", returncode=0)

    with pytest.raises(GitSafetyError) as excinfo:
        list_tracked_paths(repo)

    assert excinfo.value.reason is GitSafetyFailure.TRACKED_PATH_LISTING_MALFORMED


def test_list_tracked_paths_rejects_truncated_output(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")
    _intercept_ls_files_popen(monkeypatch, tmp_path, stdout_bytes=b"a.txt\0b.txt", returncode=0)

    with pytest.raises(GitSafetyError) as excinfo:
        list_tracked_paths(repo)

    assert excinfo.value.reason is GitSafetyFailure.TRACKED_PATH_LISTING_MALFORMED


def test_list_tracked_paths_fails_closed_when_command_fails(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")
    _intercept_ls_files_popen(monkeypatch, tmp_path, stdout_bytes=b"", returncode=128)

    with pytest.raises(GitSafetyError) as excinfo:
        list_tracked_paths(repo)

    assert excinfo.value.reason is GitSafetyFailure.TRACKED_PATH_LISTING_UNAVAILABLE
    assert "fatal" not in excinfo.value.message


def test_list_tracked_paths_rejects_output_exceeding_the_bounded_limit(monkeypatch, tmp_path):
    """New evidence for this pass: `list_tracked_paths` now goes
    through the bounded binary seam, so an oversized listing is
    detected via `limit + 1` semantics without ever materializing it
    in full -- this is the real "genuinely bounded" behavior the
    docstring now claims."""
    repo = _make_repo(tmp_path / "r")
    monkeypatch.setattr(_git_safety, "MAX_STAGE_LISTING_BYTES", 16)
    oversized_payload = b"x" * 17 + b"\0"
    _intercept_ls_files_popen(monkeypatch, tmp_path, stdout_bytes=oversized_payload, returncode=0)

    with pytest.raises(GitSafetyError) as excinfo:
        list_tracked_paths(repo)

    assert excinfo.value.reason is GitSafetyFailure.BINARY_OUTPUT_TOO_LARGE


def test_check_filter_attribute_for_paths_empty(tmp_path):
    repo = _make_repo(tmp_path / "r")

    assert check_filter_attribute_for_paths(repo, ()) == ()


def test_check_filter_attribute_for_paths_chunks_large_input(tmp_path):
    """Prove chunking actually happens: more paths than one chunk holds
    must still all be inspected, via multiple bounded git invocations."""
    repo = _make_repo(tmp_path / "r")
    count = ATTR_CHECK_CHUNK_SIZE + 5
    names = [f"f{i}.txt" for i in range(count)]
    for name in names:
        (repo / name).write_text("x\n")
    _git(repo, "add", *names)

    records = check_filter_attribute_for_paths(repo, tuple(names))

    assert len(records) == count
    assert {r.path for r in records} == set(names)
    assert all(r.attribute == "filter" for r in records)


def test_check_filter_attribute_for_paths_rejects_record_count_mismatch(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")

    def fake_run(args, **kwargs):
        # Only one record for two requested paths.
        return subprocess.CompletedProcess(args, 0, stdout="a.txt\0filter\0unspecified\0", stderr="")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_filter_attribute_for_paths(repo, ("a.txt", "b.txt"))

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_RECORD_COUNT_MISMATCH


def test_check_filter_attribute_for_paths_rejects_path_mismatch(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")

    def fake_run(args, **kwargs):
        # Reports "wrong.txt" instead of the requested "a.txt".
        return subprocess.CompletedProcess(args, 0, stdout="wrong.txt\0filter\0unspecified\0", stderr="")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_filter_attribute_for_paths(repo, ("a.txt",))

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_RECORD_PATH_MISMATCH


def test_check_filter_attribute_for_paths_rejects_duplicate_record(monkeypatch, tmp_path):
    """Both requested slots are for the same path, and both records
    correctly match their position -- so the path-mismatch check alone
    would not catch this; the independent duplicate check must."""
    repo = _make_repo(tmp_path / "r")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="a.txt\0filter\0unspecified\0a.txt\0filter\0unspecified\0",
            stderr="",
        )

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_filter_attribute_for_paths(repo, ("a.txt", "a.txt"))

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_RECORD_DUPLICATE_PATH


def test_check_filter_attribute_for_paths_rejects_unexpected_attribute(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="a.txt\0text\0unspecified\0", stderr="")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_filter_attribute_for_paths(repo, ("a.txt",))

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_RECORD_UNEXPECTED_ATTRIBUTE


def test_check_filter_attribute_for_paths_fails_closed_on_command_failure(monkeypatch, tmp_path):
    repo = _make_repo(tmp_path / "r")

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 128, stdout="", stderr="fatal: boom")

    monkeypatch.setattr(_git_safety, "_run", fake_run)

    with pytest.raises(GitSafetyError) as excinfo:
        check_filter_attribute_for_paths(repo, ("a.txt",))

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_INSPECTION_UNAVAILABLE
    assert "fatal" not in excinfo.value.message


def test_evaluate_tracked_filter_safety_true_for_plain_repo(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("x\n")
    _git(repo, "add", "a.txt")

    assert evaluate_tracked_filter_safety(repo) is True


def test_evaluate_tracked_filter_safety_true_for_no_tracked_files(tmp_path):
    repo = _make_repo(tmp_path / "r")

    assert evaluate_tracked_filter_safety(repo) is True


def test_evaluate_tracked_filter_safety_false_for_active_filter(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "config", "filter.hostile.clean", "cmd")
    (repo / ".gitattributes").write_text("a.txt filter=hostile\n")
    (repo / "a.txt").write_text("x\n")
    _git(repo, "add", ".gitattributes", "a.txt")

    assert evaluate_tracked_filter_safety(repo) is False


def test_evaluate_tracked_filter_safety_refuses_driver_named_unset(tmp_path):
    """Positive control, end to end through the composed function: a
    live driver named `unset` collides with check-attr's own reported
    string for a genuinely negated path, so the whole evaluation must
    refuse per ADR 0006 finding 16."""
    repo, _marker = _make_magic_driver_repo(tmp_path)

    assert evaluate_tracked_filter_safety(repo) is False


def test_evaluate_tracked_filter_safety_true_once_magic_driver_is_removed(tmp_path):
    """Negative control proving the refusal above is caused by the
    driver-name collision, not the attribute states themselves."""
    repo = _make_repo(tmp_path / "r")
    (repo / ".gitattributes").write_text("a.txt -filter\nc.txt text\n")
    (repo / "a.txt").write_text("x\n")
    (repo / "c.txt").write_text("y\n")
    _git(repo, "add", ".gitattributes", "a.txt", "c.txt")

    assert evaluate_tracked_filter_safety(repo) is True


# ---------------------------------------------------------------------------
# Replacement refs (refs/replace/*) can silently substitute object content
# for rev-parse/cat-file/ls-tree. --no-replace-objects + GIT_NO_REPLACE_OBJECTS=1
# (both, redundantly) must neutralize this; GIT_REPLACE_REF_BASE stripping
# alone does not (it only changes which replace namespace is consulted).
# ---------------------------------------------------------------------------


def _commit_with_content(repo, content: str) -> str:
    (repo / "a.txt").write_text(content)
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", content)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def test_replace_ref_subverts_rev_parse_tree_without_protection(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit_with_content(repo, "one")
    second = _commit_with_content(repo, "two")
    third = _commit_with_content(repo, "three")
    real_tree = _git(repo, "rev-parse", f"{second}^{{tree}}").stdout.strip()
    _git(repo, "replace", second, third)

    # Bare, unprotected call (bypasses run_git/BASELINE_ARGS deliberately,
    # to prove the vulnerability the baseline is meant to close).
    subverted = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{second}^{{tree}}"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    ).stdout.strip()

    assert subverted != real_tree


def test_no_replace_objects_baseline_prevents_replace_ref_subversion(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _commit_with_content(repo, "one")
    second = _commit_with_content(repo, "two")
    real_tree = _git(repo, "rev-parse", f"{second}^{{tree}}").stdout.strip()
    third = _commit_with_content(repo, "three")
    _git(repo, "replace", second, third)

    result = run_git(repo, "rev-parse", f"{second}^{{tree}}")

    assert result.stdout.strip() == real_tree


def test_stripping_git_replace_ref_base_alone_does_not_help(tmp_path):
    """Explicit negative control per ADR 0006: GIT_REPLACE_REF_BASE
    stripping (already covered by GIT_* removal) is not the mechanism
    that neutralizes replace refs -- they live in refs/replace/*, a
    separate channel."""
    repo = _make_repo(tmp_path / "r")
    _commit_with_content(repo, "one")
    second = _commit_with_content(repo, "two")
    real_tree = _git(repo, "rev-parse", f"{second}^{{tree}}").stdout.strip()
    third = _commit_with_content(repo, "three")
    _git(repo, "replace", second, third)

    env = _clean_env()
    env.pop("GIT_REPLACE_REF_BASE", None)
    subverted = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", f"{second}^{{tree}}"],
        capture_output=True,
        text=True,
        env=env,
    ).stdout.strip()

    assert subverted != real_tree


def test_write_tree_unaffected_by_replace_ref_either_way(tmp_path):
    """write-tree only serializes the index -- no ref/commit resolution
    -- so it is structurally immune, with or without the flags."""
    repo = _make_repo(tmp_path / "r")
    first = _commit_with_content(repo, "one")
    second = _commit_with_content(repo, "two")
    _git(repo, "replace", first, second)

    (repo / "a.txt").write_text("x\n")
    _git(repo, "add", "a.txt")
    protected = run_git(repo, "write-tree").stdout.strip()
    unprotected = subprocess.run(
        ["git", "-C", str(repo), "write-tree"], capture_output=True, text=True, env=_clean_env()
    ).stdout.strip()

    assert protected == unprotected


# ---------------------------------------------------------------------------
# Pathspec magic: `--` alone does not disable glob/bracket/`?`/`:(...)`
# interpretation. --literal-pathspecs + GIT_LITERAL_PATHSPECS=1 (both,
# redundantly) must make an approved literal filename immune to matching
# an unrelated decoy path.
# ---------------------------------------------------------------------------


def _make_pathspec_fixture(repo: Path) -> None:
    (repo / "decoyA.txt").write_text("decoy1\n")
    (repo / "decoyB.txt").write_text("decoy2\n")
    (repo / "fileX.py").write_text("decoy3\n")
    _git(repo, "add", "decoyA.txt", "decoyB.txt", "fileX.py")
    _git(repo, "commit", "-q", "-m", "decoys")


def test_unprotected_glob_pathspec_matches_a_decoy_file(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)
    (repo / "*.txt").write_text("literal star content\n")
    (repo / "decoyA.txt").write_text("modified\n")

    subprocess.run(
        ["git", "-C", str(repo), "add", "--", "*.txt"], check=True, capture_output=True, env=_clean_env()
    )
    status = _git(repo, "status", "--porcelain=v1").stdout

    assert "A  *.txt" in status
    assert "M  decoyA.txt" in status  # the decoy was swept in too


def test_literal_pathspecs_baseline_prevents_glob_matching_a_decoy(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)
    (repo / "*.txt").write_text("literal star content\n")
    (repo / "decoyA.txt").write_text("modified\n")

    run_git(repo, "add", "--", "*.txt")
    status = _git(repo, "status", "--porcelain=v1").stdout

    assert "A  *.txt" in status
    assert "M  decoyA.txt" not in status
    assert " M decoyA.txt" in status  # unstaged, untouched


def test_unprotected_question_mark_pathspec_matches_a_decoy_file(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)
    (repo / "file?.py").write_text("literal question content\n")
    (repo / "fileX.py").write_text("modified\n")

    subprocess.run(
        ["git", "-C", str(repo), "add", "--", "file?.py"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    status = _git(repo, "status", "--porcelain=v1").stdout

    assert "M  fileX.py" in status  # matched via ? wildcard


def test_literal_pathspecs_baseline_prevents_question_mark_matching(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)
    (repo / "file?.py").write_text("literal question content\n")
    (repo / "fileX.py").write_text("modified\n")

    run_git(repo, "add", "--", "file?.py")
    status = _git(repo, "status", "--porcelain=v1").stdout

    assert "M  fileX.py" not in status
    assert " M fileX.py" in status


def test_unprotected_magic_prefix_pathspec_matches_multiple_files(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)
    (repo / "decoyA.txt").write_text("modified\n")

    result = subprocess.run(
        ["git", "-C", str(repo), "add", "--", ":(glob)*.txt"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    status = _git(repo, "status", "--porcelain=v1").stdout

    assert result.returncode == 0
    assert "M  decoyA.txt" in status


def test_literal_pathspecs_baseline_fails_closed_for_magic_prefix(tmp_path):
    """Under --literal-pathspecs, ":(glob)*.txt" is treated as a literal
    (nonexistent) filename and the add fails closed, rather than
    matching anything."""
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)

    result = run_git(repo, "add", "--", ":(glob)*.txt")

    assert result.returncode != 0


def test_literal_leading_colon_filename_requires_literal_pathspecs(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)
    (repo / ":weird.txt").write_text("literal leading colon\n")

    unprotected = subprocess.run(
        ["git", "-C", str(repo), "add", "--", ":weird.txt"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    assert unprotected.returncode != 0  # unrecognized magic keyword -> hard error, not literal

    run_git(repo, "add", "--", ":weird.txt")
    status = _git(repo, "status", "--porcelain=v1").stdout
    assert "A  :weird.txt" in status


def test_lookup_style_defense_in_depth_reveals_a_bypassed_magic_match(tmp_path):
    """Even if a future call site forgot the hardened baseline, the
    exact-one-record discipline in the path-safe lookup helpers would
    still detect a multi-file magic match after the fact -- proven here
    against the raw `ls-files --stage -z` shape those helpers parse."""
    repo = _make_repo(tmp_path / "r")
    _make_pathspec_fixture(repo)
    (repo / "*.txt").write_text("literal\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "--", "*.txt"], check=True, capture_output=True, env=_clean_env()
    )

    result = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--stage", "-z", "--", "*.txt"],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    records = [r for r in result.stdout.split("\0") if r]

    assert len(records) > 1


# ---------------------------------------------------------------------------
# Object-format detection and OID validation (ADR 0003/0004: sha1 and
# sha256 are the only accepted formats; every Git-produced OID this
# module reuses is validated against the repository's actual format
# before being trusted).
# ---------------------------------------------------------------------------


def test_detect_object_format_sha1(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-q", "--object-format=sha1", str(repo)],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )

    assert _git_safety.detect_object_format(repo) == _git_safety.ObjectFormat.SHA1


def test_detect_object_format_sha256(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    result = subprocess.run(
        ["git", "init", "-q", "--object-format=sha256", str(repo)],
        capture_output=True,
        text=True,
        env=_clean_env(),
    )
    if result.returncode != 0:
        pytest.skip("this git build does not support --object-format=sha256")

    assert _git_safety.detect_object_format(repo) == _git_safety.ObjectFormat.SHA256


def test_object_format_hex_lengths():
    assert _git_safety.ObjectFormat.SHA1.hex_length == 40
    assert _git_safety.ObjectFormat.SHA256.hex_length == 64


def test_validate_oid_accepts_correct_length_and_charset():
    oid = "a" * 40
    assert _git_safety.validate_oid(_git_safety.ObjectFormat.SHA1, oid) == oid


def test_validate_oid_rejects_wrong_length():
    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.validate_oid(_git_safety.ObjectFormat.SHA1, "a" * 39)
    assert excinfo.value.reason is GitSafetyFailure.MALFORMED_OID


def test_validate_oid_rejects_non_hex_characters():
    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.validate_oid(_git_safety.ObjectFormat.SHA1, "g" * 40)
    assert excinfo.value.reason is GitSafetyFailure.MALFORMED_OID


def test_validate_oid_rejects_uppercase():
    with pytest.raises(GitSafetyError):
        _git_safety.validate_oid(_git_safety.ObjectFormat.SHA1, "A" * 40)


def test_validate_oid_sha256_requires_64_chars():
    oid = "b" * 64
    assert _git_safety.validate_oid(_git_safety.ObjectFormat.SHA256, oid) == oid
    with pytest.raises(GitSafetyError):
        _git_safety.validate_oid(_git_safety.ObjectFormat.SHA256, "b" * 40)


# ---------------------------------------------------------------------------
# Bounded binary subprocess runner: one monotonic deadline across read,
# termination, and reap; limit+1 semantics; confirmed process exit;
# never exposes argv/paths/stderr/raw exceptions.
# ---------------------------------------------------------------------------


def test_run_git_bounded_returns_small_output(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    sha = _git(repo, "rev-parse", ":a.txt").stdout.strip()

    result = _git_safety.run_git_bounded(repo, "cat-file", "-p", sha, limit=1024)

    assert result.stdout == b"hello\n"
    assert result.returncode == 0


def test_run_git_bounded_detects_oversized_output_without_reading_it_all(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "big.txt").write_bytes(b"x" * 500_000)
    _git(repo, "add", "big.txt")
    sha = _git(repo, "rev-parse", ":big.txt").stdout.strip()

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.run_git_bounded(repo, "cat-file", "-p", sha, limit=4096)

    assert excinfo.value.reason is GitSafetyFailure.BINARY_OUTPUT_TOO_LARGE


def test_run_git_bounded_exact_boundary_succeeds(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "exact.txt").write_bytes(b"y" * 100)
    _git(repo, "add", "exact.txt")
    sha = _git(repo, "rev-parse", ":exact.txt").stdout.strip()

    result = _git_safety.run_git_bounded(repo, "cat-file", "-p", sha, limit=100)

    assert len(result.stdout) == 100


def test_run_git_bounded_fails_categorically_on_nonzero_returncode(tmp_path):
    """`run_git_bounded` (the public wrapper) fails categorically on a
    nonzero exit rather than returning a `BoundedProcessResult` the
    caller must remember to check — every current call site treated a
    nonzero exit as a failure anyway."""
    repo = _make_repo(tmp_path / "r")

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.run_git_bounded(repo, "cat-file", "-p", "0" * 40, limit=1024)

    assert excinfo.value.reason is _git_safety.GitSafetyFailure.BOUNDED_COMMAND_FAILED


def test_read_bounded_still_returns_a_neutral_nonzero_result(tmp_path):
    """The private `_read_bounded` primitive keeps its own neutral
    contract (never raises for a nonzero exit) — only the public
    `run_git_bounded` wrapper is stricter."""
    repo = _make_repo(tmp_path / "r")
    argv = _git_safety._build_git_argv(repo, "cat-file", "-p", "0" * 40)

    result = _git_safety._read_bounded(argv[1:], env=_git_safety.git_environment(), timeout=5.0, limit=1024)

    assert result.returncode != 0


def test_run_git_bounded_times_out_on_a_hung_child(monkeypatch):
    """Reproduces the exact hung-child scenario probed this session:
    a process that writes partial output then never finishes. Must
    time out via the deadline, not block forever waiting for EOF."""

    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            return real_popen(
                ["sh", "-c", "printf partial; sleep 300"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety._read_bounded(["--version"], env=_git_safety.git_environment(), timeout=0.5, limit=4096)

    assert excinfo.value.reason is GitSafetyFailure.GIT_COMMAND_TIMEOUT


def test_run_git_bounded_launch_failure_is_sanitized(monkeypatch):
    def fake_popen(argv, **kwargs):
        raise FileNotFoundError("no such file: git")

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety._read_bounded(["--version"], env=_git_safety.git_environment(), timeout=1.0, limit=4096)

    assert excinfo.value.reason is GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE


def test_run_git_bounded_cleanup_unconfirmed_when_process_will_not_die(monkeypatch, tmp_path):
    """If the child cannot be confirmed terminated even after kill +
    bounded wait, a distinct categorical failure is raised rather than
    silently treating an unconfirmed process as handled."""
    repo = _make_repo(tmp_path / "r")

    real_popen = subprocess.Popen

    class StubbornProcess:
        def __init__(self, *args, **kwargs):
            self._real = real_popen(
                ["sh", "-c", "sleep 300"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            self.stdout = self._real.stdout

        def kill(self):
            pass  # simulate a kill that never actually terminates the process

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout or 0)

        @property
        def returncode(self):
            return self._real.returncode

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            return StubbornProcess()
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)

    try:
        with pytest.raises(GitSafetyError) as excinfo:
            _git_safety._read_bounded(
                ["--version"], env=_git_safety.git_environment(), timeout=0.2, limit=4096
            )
        assert excinfo.value.reason is GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED
    finally:
        subprocess.run(["pkill", "-f", "sleep 300"], capture_output=True)


def test_run_git_bounded_error_messages_do_not_leak_argv_or_paths(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "big.txt").write_bytes(b"x" * 500_000)
    _git(repo, "add", "big.txt")
    sha = _git(repo, "rev-parse", ":big.txt").stdout.strip()

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.run_git_bounded(repo, "cat-file", "-p", sha, limit=4096)

    assert str(repo) not in str(excinfo.value)
    assert sha not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


# ---------------------------------------------------------------------------
# Path-safe index/tree lookup: never `:<path>` or `<commit>:<path>`
# revision syntax; exactly-one-record discipline; mode/type validation.
# ---------------------------------------------------------------------------


def test_lookup_staged_entry_returns_regular_file(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    expected_oid = _git(repo, "rev-parse", ":a.txt").stdout.strip()

    entry = _git_safety.lookup_staged_entry(repo, "a.txt", object_format=_git_safety.ObjectFormat.SHA1)

    assert entry.path == "a.txt"
    assert entry.oid == expected_oid
    assert entry.mode == "100644"


def test_lookup_staged_entry_missing_path(tmp_path):
    repo = _make_repo(tmp_path / "r")

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.lookup_staged_entry(
            repo, "does-not-exist.txt", object_format=_git_safety.ObjectFormat.SHA1
        )

    assert excinfo.value.reason is GitSafetyFailure.INDEX_ENTRY_MISSING


def test_lookup_staged_entry_rejects_unmerged_path(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "c.txt").write_text("base\n")
    _git(repo, "add", "c.txt")
    _git(repo, "commit", "-q", "-m", "base")
    initial_branch = _git(repo, "branch", "--show-current").stdout.strip()
    _git(repo, "checkout", "-qb", "branch1")
    (repo / "c.txt").write_text("branch1\n")
    _git(repo, "commit", "-qam", "b1")
    _git(repo, "checkout", "-q", initial_branch)
    (repo / "c.txt").write_text("master\n")
    _git(repo, "commit", "-qam", "m")
    # Explicit identity, same as every other git invocation via _git()
    # -- omitting it here (as this test previously did) relies on
    # ambient global git config, which does not exist on a clean CI
    # runner: the merge then fails outright with "unable to
    # auto-detect email address" instead of producing the intended
    # conflict, and the fixture passes vacuously.
    merge_result = subprocess.run(
        ["git", "-C", str(repo), *_GIT_IDENTITY, "merge", "branch1", "-q"],
        capture_output=True,
        env=_clean_env(),
    )
    assert merge_result.returncode == 1, (
        "expected a real merge conflict (exit 1); got "
        f"{merge_result.returncode}: {merge_result.stderr!r}"
    )
    # Independent confirmation the fixture actually reached the
    # unmerged state this test means to exercise -- never trust the
    # merge's own exit code alone to imply an unmerged index.
    unmerged = _git(repo, "ls-files", "-u").stdout
    assert unmerged.strip() != "", "fixture did not produce an unmerged path to test against"

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.lookup_staged_entry(repo, "c.txt", object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.INDEX_ENTRY_AMBIGUOUS


def test_lookup_staged_entry_rejects_symlink_mode(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "target.txt").write_text("x\n")
    _git(repo, "add", "target.txt")
    subprocess.run(
        ["ln", "-s", "target.txt", str(repo / "link.txt")], check=True, capture_output=True
    )
    _git(repo, "add", "link.txt")

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.lookup_staged_entry(repo, "link.txt", object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.INDEX_ENTRY_UNEXPECTED_MODE


def test_lookup_staged_entry_rejects_multi_match_from_pathspec_magic(tmp_path):
    """The hardened baseline's `--literal-pathspecs` means a glob-looking
    string like `*.txt` is never reinterpreted as magic here — it is
    looked up as the literal filename `*.txt`, which does not exist, so
    the safe, correct outcome is MISSING rather than a multi-match. This
    is defense-in-depth evidence: the lookup never widens a literal path
    into a sweep across `a.txt`/`b.txt`, even though it could ambiguously
    match if the baseline's literal-pathspecs protection were absent."""
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("a\n")
    (repo / "b.txt").write_text("b\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "a.txt", "b.txt"], check=True, capture_output=True, env=_clean_env()
    )

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.lookup_staged_entry(repo, "*.txt", object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.INDEX_ENTRY_MISSING


def test_lookup_tree_entry_returns_blob(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected_oid = _git(repo, "rev-parse", f"{head}:a.txt").stdout.strip()

    entry = _git_safety.lookup_tree_entry(
        repo, head, "a.txt", object_format=_git_safety.ObjectFormat.SHA1
    )

    assert entry is not None
    assert entry.path == "a.txt"
    assert entry.oid == expected_oid
    assert entry.mode == "100644"
    assert entry.type == "blob"


def test_lookup_tree_entry_missing_path_returns_none(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    entry = _git_safety.lookup_tree_entry(
        repo, head, "does-not-exist.txt", object_format=_git_safety.ObjectFormat.SHA1
    )

    assert entry is None


def test_lookup_tree_entry_rejects_gitlink(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    subprocess.run(["git", "init", "-q", str(sub)], check=True, capture_output=True, env=_clean_env())
    subprocess.run(
        ["git", "-C", str(sub)] + _GIT_IDENTITY + ["commit", "-q", "--allow-empty", "-m", "sub"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    repo = _make_repo(tmp_path / "r")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "protocol.file.allow=always"] + _GIT_IDENTITY
        + ["submodule", "add", "-q", f"file://{sub}", "sub"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    _git(repo, "commit", "-q", "-m", "add submodule")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.lookup_tree_entry(repo, head, "sub", object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.TREE_ENTRY_UNEXPECTED_MODE


def test_check_index_blob_availability_passes_for_complete_repo(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")

    _git_safety.check_index_blob_availability(repo, object_format=_git_safety.ObjectFormat.SHA1)


def test_check_index_blob_availability_skips_gitlinks(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    subprocess.run(["git", "init", "-q", str(sub)], check=True, capture_output=True, env=_clean_env())
    subprocess.run(
        ["git", "-C", str(sub)] + _GIT_IDENTITY + ["commit", "-q", "--allow-empty", "-m", "sub"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    repo = _make_repo(tmp_path / "r")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "protocol.file.allow=always"] + _GIT_IDENTITY
        + ["submodule", "add", "-q", f"file://{sub}", "sub"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )

    # A staged gitlink whose target commit is not (and never needs to be)
    # present locally must not fail the check.
    _git_safety.check_index_blob_availability(repo, object_format=_git_safety.ObjectFormat.SHA1)


def test_check_index_blob_availability_detects_a_missing_blob(tmp_path):
    src = tmp_path / "src"
    _make_repo(src)
    subprocess.run(
        ["git", "-C", str(src), "config", "uploadpack.allowFilter", "true"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    (src / "a.txt").write_text("hello\n")
    _git(src, "add", "a.txt")
    _git(src, "commit", "-q", "-m", "init")
    blob_oid = _git(src, "rev-parse", "HEAD:a.txt").stdout.strip()

    # `--no-local` is required: Git's local-clone fast path ignores
    # `--filter` entirely ("filtering not recognized by server"), which
    # would silently fetch every blob and defeat this test's premise.
    clone = tmp_path / "clone"
    subprocess.run(
        [
            "git", "-c", "protocol.file.allow=always", "clone", "-q", "--no-local",
            "--filter=blob:none", "--no-checkout", f"file://{src}", str(clone),
        ],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    subprocess.run(
        ["git", "-C", str(clone), "config", "extensions.partialClone", "origin"],
        check=True,
        capture_output=True,
        env=_clean_env(),
    )
    # Stage the tree without ever fetching the blob content: read-tree
    # populates the index from HEAD's tree without materializing files.
    _git(clone, "read-tree", "HEAD")
    # Confirm the blob is genuinely absent locally before asserting on it.
    local_check = subprocess.run(
        ["git", "-C", str(clone), "-c", "core.useReplaceRefs=false", "cat-file", "-e", blob_oid],
        env={**_clean_env(), "GIT_NO_LAZY_FETCH": "1"},
        capture_output=True,
    )
    if local_check.returncode == 0:
        pytest.skip("partial clone unexpectedly has the blob locally; cannot exercise this path")

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.check_index_blob_availability(clone, object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is _git_safety.GitSafetyFailure.OBJECT_MISSING


def test_check_index_blob_availability_chunks_large_staged_sets(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "r")
    for i in range(5):
        (repo / f"f{i}.txt").write_text(f"content {i}\n")
    _git(repo, "add", *[f"f{i}.txt" for i in range(5)])

    calls: list[int] = []
    real_check = _git_safety._check_objects_present

    def _counting_check(repo_path, oids, *, object_format):
        calls.append(len(oids))
        return real_check(repo_path, oids, object_format=object_format)

    monkeypatch.setattr(_git_safety, "_check_objects_present", _counting_check)
    monkeypatch.setattr(_git_safety, "MAX_BATCH_CHECK_CHUNK", 2)

    _git_safety.check_index_blob_availability(repo, object_format=_git_safety.ObjectFormat.SHA1)

    assert calls == [2, 2, 1]


def test_read_blob_bytes_returns_exact_content(tmp_path):
    repo = _make_repo(tmp_path / "r")
    content = b"line one\r\nline two\x00binary\n"
    (repo / "a.bin").write_bytes(content)
    _git(repo, "add", "a.bin")
    oid = _git(repo, "rev-parse", ":a.bin").stdout.strip()

    result = _git_safety.read_blob_bytes(repo, oid, object_format=_git_safety.ObjectFormat.SHA1)

    assert result == content


def test_read_blob_bytes_rejects_oversized_blob(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("x" * 100)
    _git(repo, "add", "a.txt")
    oid = _git(repo, "rev-parse", ":a.txt").stdout.strip()
    monkeypatch.setattr(_git_safety, "MAX_PATCH_BLOB_BYTES", 10)

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.read_blob_bytes(repo, oid, object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.BLOB_TOO_LARGE


def test_read_blob_bytes_rejects_non_blob_type(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.read_blob_bytes(repo, head, object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.BLOB_UNEXPECTED_TYPE


def test_read_blob_bytes_rejects_missing_oid(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "init")
    fake_oid = "a" * 40

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.read_blob_bytes(repo, fake_oid, object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.BLOB_UNAVAILABLE


def test_read_commit_header_returns_tree_and_parents(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "first")
    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "a.txt").write_text("two\n")
    _git(repo, "commit", "-qam", "second")
    second = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected_tree = _git(repo, "rev-parse", f"{second}^{{tree}}").stdout.strip()

    header = _git_safety.read_commit_header(repo, second, object_format=_git_safety.ObjectFormat.SHA1)

    assert header.tree == expected_tree
    assert header.parents == (first,)


def test_read_commit_header_root_commit_has_no_parents(tmp_path):
    repo = _make_repo(tmp_path / "r")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "root")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    header = _git_safety.read_commit_header(repo, head, object_format=_git_safety.ObjectFormat.SHA1)

    assert header.parents == ()


def test_read_commit_header_rejects_non_commit_type(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    blob_oid = _git(repo, "rev-parse", ":a.txt").stdout.strip()

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.read_commit_header(repo, blob_oid, object_format=_git_safety.ObjectFormat.SHA1)

    assert excinfo.value.reason is GitSafetyFailure.COMMIT_OBJECT_UNEXPECTED_TYPE


def test_read_commit_header_ignores_message_body(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "tree \nparent \nnot a real header line")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    expected_tree = _git(repo, "rev-parse", f"{head}^{{tree}}").stdout.strip()

    header = _git_safety.read_commit_header(repo, head, object_format=_git_safety.ObjectFormat.SHA1)

    assert header.tree == expected_tree
    assert header.parents == ()


def test_read_bounded_with_stdin_survives_pipe_buffer_pressure(tmp_path):
    """Real evidence for the concurrent-writer design: a batch-check
    payload larger than a typical OS pipe buffer must not deadlock."""
    repo = _make_repo(tmp_path / "r")
    oids: list[str] = []
    for i in range(300):
        path = repo / f"f{i}.txt"
        path.write_text(f"content number {i}\n" * 5)
        _git(repo, "add", f"f{i}.txt")
        oids.append(_git(repo, "rev-parse", f":f{i}.txt").stdout.strip())

    _git_safety._check_objects_present(repo, oids, object_format=_git_safety.ObjectFormat.SHA1)


def test_check_filter_attribute_for_paths_uncached_reflects_unstaged_gitattributes(tmp_path):
    """An unstaged `.gitattributes` edit still governs `git add` itself
    -- this is why patch.py's hardening needs a working-tree
    (`cached=False`) attribute view in addition to the staged one."""
    repo = _make_repo(tmp_path / "r")
    (repo / "a.bin").write_text("hello\n")
    _git(repo, "add", "a.bin")
    _git(repo, "commit", "-q", "-m", "init")

    # Add a *staged* .gitattributes that marks a.bin filterless, but then
    # further edit it in the working tree (unstaged) to apply a filter.
    (repo / ".gitattributes").write_text("*.bin -filter\n")
    _git(repo, "add", ".gitattributes")
    (repo / ".gitattributes").write_text("*.bin filter=lfs\n")

    cached = _git_safety.check_filter_attribute_for_paths(repo, ["a.bin"], cached=True)
    uncached = _git_safety.check_filter_attribute_for_paths(repo, ["a.bin"], cached=False)

    assert cached[0].value == "unset"
    assert uncached[0].value == "lfs"


# --------------------------------------------------------------------
# check_attributes_for_paths: full ADR 0006 attribute set
# --------------------------------------------------------------------


def test_check_attributes_for_paths_reports_all_six_attributes_unspecified_by_default(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("hello\n")
    _git(repo, "add", "a.txt")

    records = _git_safety.check_attributes_for_paths(repo, ["a.txt"], cached=True)

    assert [r.attribute for r in records] == list(_git_safety.ALL_SAFETY_ATTRIBUTE_NAMES)
    assert all(r.value == "unspecified" for r in records)
    assert all(r.path == "a.txt" for r in records)


def test_check_attributes_for_paths_detects_a_live_text_eol_transformation(tmp_path):
    """Positive control: `text eol=crlf` is a live, active
    transformation (finding 7's `core.autocrlf=false` does not
    override it), and both `text` and `eol` must report as unsafe."""
    repo = _make_repo(tmp_path / "r")
    (repo / ".gitattributes").write_text("crlf.txt text eol=crlf\n")
    (repo / "crlf.txt").write_bytes(b"line one\r\nline two\r\n")
    _git(repo, "add", ".gitattributes", "crlf.txt")

    records = {
        r.attribute: r
        for r in _git_safety.check_attributes_for_paths(repo, ["crlf.txt"], cached=True)
    }
    neutralization = _git_safety.enumerate_filter_neutralization(repo)

    assert records["text"].value == "set"
    assert records["eol"].value == "crlf"
    assert not records["text"].is_safe(configured_driver_names=neutralization.driver_names)
    assert not records["eol"].is_safe(configured_driver_names=neutralization.driver_names)

    # Live evidence the transformation actually mutates content: the
    # committed blob is LF-normalized regardless of the CRLF working-
    # tree bytes above.
    blob_oid = _git(repo, "rev-parse", ":crlf.txt").stdout.strip()
    committed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", blob_oid], capture_output=True, env=_clean_env()
    ).stdout
    assert committed == b"line one\nline two\n"


def test_check_attributes_for_paths_detects_a_live_ident_transformation(tmp_path):
    """Positive control: `ident` performs live `$Id$` keyword
    expansion at staging time."""
    repo = _make_repo(tmp_path / "r")
    (repo / ".gitattributes").write_text("ident.txt ident\n")
    # A previously-expanded $Id$ line (as `ident`'s own smudge would
    # produce on checkout) -- staging must collapse it back via clean,
    # a real, observable, non-identity transformation.
    (repo / "ident.txt").write_text(
        "$Id: 0123456789abcdef0123456789abcdef01234567 $\nbody\n"
    )
    _git(repo, "add", ".gitattributes", "ident.txt")

    records = {
        r.attribute: r
        for r in _git_safety.check_attributes_for_paths(repo, ["ident.txt"], cached=True)
    }
    neutralization = _git_safety.enumerate_filter_neutralization(repo)

    assert records["ident"].value == "set"
    assert not records["ident"].is_safe(configured_driver_names=neutralization.driver_names)

    blob_oid = _git(repo, "rev-parse", ":ident.txt").stdout.strip()
    committed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", blob_oid], capture_output=True, env=_clean_env()
    ).stdout
    working_tree_bytes = (repo / "ident.txt").read_bytes()
    assert committed != working_tree_bytes
    assert committed == b"$Id$\nbody\n"


def test_check_attributes_for_paths_detects_a_live_working_tree_encoding_transformation(tmp_path):
    """Positive control: `working-tree-encoding=UTF-16` performs a
    live re-encoding of the blob relative to the working-tree bytes."""
    repo = _make_repo(tmp_path / "r")
    (repo / ".gitattributes").write_text("wte.txt working-tree-encoding=UTF-16\n")
    (repo / "wte.txt").write_text("hello\n", encoding="utf-16")
    _git(repo, "add", ".gitattributes", "wte.txt")

    records = {
        r.attribute: r
        for r in _git_safety.check_attributes_for_paths(repo, ["wte.txt"], cached=True)
    }
    neutralization = _git_safety.enumerate_filter_neutralization(repo)

    assert records["working-tree-encoding"].value == "UTF-16"
    assert not records["working-tree-encoding"].is_safe(
        configured_driver_names=neutralization.driver_names
    )

    blob_oid = _git(repo, "rev-parse", ":wte.txt").stdout.strip()
    committed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-p", blob_oid], capture_output=True, env=_clean_env()
    ).stdout
    working_tree_bytes = (repo / "wte.txt").read_bytes()
    assert committed != working_tree_bytes
    assert committed == "hello\n".encode("utf-8")


def test_check_attributes_for_paths_rejects_duplicate_requested_path(tmp_path):
    repo = _make_repo(tmp_path / "r")
    (repo / "a.txt").write_text("x\n")
    _git(repo, "add", "a.txt")

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety.check_attributes_for_paths(repo, ["a.txt", "a.txt"], cached=True)

    assert excinfo.value.reason is GitSafetyFailure.ATTRIBUTE_RECORD_DUPLICATE_PATH


def test_check_attributes_for_paths_rejects_empty_attribute_names(tmp_path):
    repo = _make_repo(tmp_path / "r")

    with pytest.raises(ValueError):
        _git_safety.check_attributes_for_paths(repo, ["a.txt"], attribute_names=())


def test_run_git_bounded_selector_setup_failure_is_categorical_and_cleans_up(monkeypatch):
    """A monitoring-setup failure (selector construction itself failing)
    must still go through the single cleanup path and kill the real
    child, not leak a process or raise an unsanitized exception."""
    real_popen = subprocess.Popen
    real_selector_cls = _git_safety.selectors.DefaultSelector

    def failing_selector():
        raise OSError("simulated selector construction failure")

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            return real_popen(
                ["sh", "-c", "sleep 300"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_git_safety.selectors, "DefaultSelector", failing_selector)

    try:
        with pytest.raises(GitSafetyError) as excinfo:
            _git_safety._read_bounded(
                ["--version"], env=_git_safety.git_environment(), timeout=1.0, limit=4096
            )
        assert excinfo.value.reason is GitSafetyFailure.PROCESS_SETUP_FAILED
    finally:
        monkeypatch.setattr(_git_safety.selectors, "DefaultSelector", real_selector_cls)
        subprocess.run(["pkill", "-f", "sleep 300"], capture_output=True)


def test_run_git_bounded_classifies_eof_then_hang_as_timeout(monkeypatch):
    """A child that closes stdout (a real EOF) but does not promptly
    exit must be classified as GIT_COMMAND_TIMEOUT, not silently
    accepted as a successful bounded read."""
    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            return real_popen(
                ["sh", "-c", "printf done; exec 1>&-; sleep 300"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)

    try:
        with pytest.raises(GitSafetyError) as excinfo:
            _git_safety._read_bounded(
                ["--version"], env=_git_safety.git_environment(), timeout=0.3, limit=4096
            )
        assert excinfo.value.reason is GitSafetyFailure.GIT_COMMAND_TIMEOUT
    finally:
        subprocess.run(["pkill", "-f", "sleep 300"], capture_output=True)


def test_run_git_bounded_cleans_up_a_writer_blocked_on_a_full_stdin_pipe(monkeypatch):
    """A writer thread blocked on a full stdin pipe (because the child
    never reads it) must be unblocked and confirmed once the child is
    killed as part of the drain-timeout cleanup path -- the original
    GIT_COMMAND_TIMEOUT is what's raised, not a spurious cleanup
    failure caused by the writer.

    Uses `exec sleep 300`, not bare `sleep 300`, so the process this
    test kills is the *same* process holding the pipe open. A plain
    `sh -c "sleep 300"` can leave `sh` forking `sleep` as a child
    instead of exec-replacing itself (confirmed to differ by shell/
    platform); killing only the parent then leaves the pipe's read end
    held open by an orphaned `sleep`, and the blocked write never
    unblocks -- which is not a bug in `_terminate_and_confirm`, it is
    that function correctly reporting `PROCESS_CLEANUP_UNCONFIRMED`
    (failing closed) rather than falsely claiming cleanup succeeded.
    That failure mode is exercised deliberately, separately, by
    `test_run_git_bounded_raises_cleanup_unconfirmed_when_writer_will_not_stop`
    below. This test's own intent is the *ordinary* case: a single
    killed process whose death promptly unblocks its writer.
    """
    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            return real_popen(
                ["sh", "-c", "exec sleep 300"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)

    # Larger than a typical OS pipe buffer, so the writer thread
    # genuinely blocks rather than completing immediately.
    oversized_payload = b"x" * (4 * 1024 * 1024)

    try:
        with pytest.raises(GitSafetyError) as excinfo:
            _git_safety._read_bounded(
                ["--version"],
                env=_git_safety.git_environment(),
                timeout=0.3,
                limit=4096,
                input_bytes=oversized_payload,
            )
        assert excinfo.value.reason is GitSafetyFailure.GIT_COMMAND_TIMEOUT
    finally:
        subprocess.run(["pkill", "-f", "sleep 300"], capture_output=True)


def test_run_git_bounded_raises_cleanup_unconfirmed_when_writer_will_not_stop(monkeypatch):
    """If the writer thread cannot be confirmed stopped even after the
    process is killed and a bounded grace join, a distinct categorical
    failure is raised -- the writer, not just the process, must be
    confirmed."""
    real_popen = subprocess.Popen

    class _HangingStdin:
        def write(self, data: bytes) -> None:
            time.sleep(5.0)  # never unblocked by closing/killing the process

        def close(self) -> None:
            pass

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            process = real_popen(["true"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            process.stdin = _HangingStdin()
            return process
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)

    with pytest.raises(GitSafetyError) as excinfo:
        _git_safety._read_bounded(
            ["--version"],
            env=_git_safety.git_environment(),
            timeout=0.2,
            limit=4096,
            input_bytes=b"hello",
        )
    assert excinfo.value.reason is GitSafetyFailure.PROCESS_CLEANUP_UNCONFIRMED


def test_read_bounded_thread_start_failure_reaps_the_child(monkeypatch):
    """A writer-thread start failure (e.g. `RuntimeError: can't start
    new thread`) is now inside the same protected region as the
    drain/confirm sequence -- it must invoke _terminate_and_confirm
    before propagating, never leak the already-spawned process."""
    real_popen = subprocess.Popen
    spawned: dict[str, subprocess.Popen] = {}

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            process = real_popen(
                ["sh", "-c", "sleep 300"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            spawned["proc"] = process
            return process
        return real_popen(argv, **kwargs)

    def raising_start(self):
        raise RuntimeError("simulated thread-start failure")

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(threading.Thread, "start", raising_start)

    try:
        with pytest.raises(GitSafetyError) as excinfo:
            _git_safety._read_bounded(
                ["--version"],
                env=_git_safety.git_environment(),
                timeout=1.0,
                limit=4096,
                input_bytes=b"hello",
            )
        assert excinfo.value.reason is GitSafetyFailure.PROCESS_SETUP_FAILED
        assert "proc" in spawned
        assert spawned["proc"].poll() is not None  # confirmed reaped, not leaked
    finally:
        subprocess.run(["pkill", "-f", "sleep 300"], capture_output=True)


def test_read_bounded_injected_git_safety_error_after_popen_cleans_up_and_propagates(
    monkeypatch,
):
    """A raw GitSafetyError raised from inside the protected region
    (not just a `_BoundedFailure`) must still invoke
    _terminate_and_confirm before propagating, and its identity/reason
    must be preserved exactly -- not converted to a generic
    PROCESS_SETUP_FAILED."""
    real_popen = subprocess.Popen
    spawned: dict[str, subprocess.Popen] = {}

    def fake_popen(argv, **kwargs):
        if argv[:1] == ["git"]:
            process = real_popen(
                ["sh", "-c", "sleep 300"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
            )
            spawned["proc"] = process
            return process
        return real_popen(argv, **kwargs)

    injected = GitSafetyError(GitSafetyFailure.MALFORMED_OID, "simulated injected failure")

    def raising_drain(process, *, deadline, limit):
        raise injected

    monkeypatch.setattr(_git_safety.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(_git_safety, "_drain_stdout", raising_drain)

    try:
        with pytest.raises(GitSafetyError) as excinfo:
            _git_safety._read_bounded(
                ["--version"], env=_git_safety.git_environment(), timeout=1.0, limit=4096
            )
        assert excinfo.value is injected
        assert spawned["proc"].poll() is not None  # confirmed reaped, not leaked
    finally:
        subprocess.run(["pkill", "-f", "sleep 300"], capture_output=True)
