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
from pathlib import Path

import pytest

from codeagent import _git_safety
from codeagent._git_safety import (
    BASELINE_ARGS,
    FIXED_CAT_PATH,
    MAX_FILTER_ARGV_BYTES,
    MAX_FILTER_DRIVER_NAME_BYTES,
    MAX_FILTER_DRIVERS,
    MIN_GIT_VERSION,
    AttributeRecord,
    FilterNeutralization,
    GitSafetyError,
    GitSafetyFailure,
    check_git_preflight,
    enumerate_filter_neutralization,
    git_environment,
    is_safe_filter_state,
    is_safe_non_filter_state,
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

    assert not any(key.startswith("GIT_") and key != "GIT_NO_LAZY_FETCH" for key in env)


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
