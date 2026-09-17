"""Shared Git-safety foundation for ADR 0006
(`docs/adr/0006-git-safety-policy-for-filters-hooks-and-content-fidelity.md`,
Accepted).

Scope boundary, deliberately narrow (this is the foundation slice
only):

- This module owns the building blocks ADR 0006 specifies: Git >= 2.45
  / `--no-lazy-fetch` preflight, sanitized environment construction,
  the fixed hardened baseline argv, bounded filter-driver
  enumeration/neutralization, and NUL-safe `check-attr` output parsing
  with the safe/unsafe attribute-state classification.
- It is **not** wired into `workspace.py`, `patch.py`,
  `checkpoint_ref.py`, the controller, or any lifecycle/CLI code yet.
  No production behavior outside this module changes.
- It does not implement the no-checkout/read-tree/inspect-then-refuse
  orchestration for `worktree add` (ADR 0006 §1), the `.gitattributes`
  patch-handling sequencing (§3), or the source-status sequencing (§4)
  — those integrate this foundation into `workspace.py`/`patch.py` in a
  later slice.
- It never decides run outcomes and never constructs an
  `OperationalError`. Failures raise `GitSafetyError` carrying a
  categorical `reason`; an integrating slice translates one failure
  occurrence into exactly one `OperationalError`, mirroring
  `codeagent.checkpoint_ref.CheckpointRefError`'s existing split.

Safety properties this module enforces:

- **Git >= 2.45 is required, verified two ways.** `check_git_preflight`
  parses `git --version` and requires the reported version to be
  `>= 2.45` — the version Git's global `--no-lazy-fetch` option was
  introduced in (ADR 0006 §6) — and additionally proves the option is
  actually recognized by running `git --no-pager --no-lazy-fetch
  --version`. Either failure is unsupported substrate: a passing
  version number alone is never treated as sufficient proof that the
  installed binary behaves as documented.
- **Environment cannot redirect Git, and cannot silently re-enable lazy
  fetch.** `git_environment()` strips every inherited `GIT_*` variable
  (same rationale as `checkpoint_ref.git_environment()`) and then
  deliberately sets `GIT_NO_LAZY_FETCH=1`, overriding any inherited
  value rather than trusting it.
- **A fixed hardened baseline applies to every governed invocation.**
  `BASELINE_ARGS` is the exact ADR 0006 §5 set: `-c
  core.hooksPath=/dev/null`, `-c core.fsmonitor=false` (an explicit
  boolean — an empty string does not suppress fsmonitor), `-c
  core.autocrlf=false`, `-c submodule.recurse=false`, `--no-pager`, and
  `--no-lazy-fetch` (redundant with the environment variable above,
  deliberately — a call site that forgets one still has the other).
- **Filter-driver neutralization only touches subkeys that actually
  exist, and only `clean`/`smudge`, never `required`.**
  `enumerate_filter_neutralization` overrides `clean`/`smudge` to the
  fixed, verified absolute path `/bin/cat` (never `shutil.which`) only
  for drivers where that subkey is actually configured, and clears
  `process` (empty, never `/bin/cat` — `.process` speaks a persistent
  pkt-line protocol `/bin/cat` cannot) only when it is actually
  configured. `filter.<name>.required` is read but never overridden:
  ADR 0006 §1/finding 10 proved `required=false` only masks the
  enumerator's own construction bugs and is unnecessary once overrides
  are emitted correctly.
- **Enumeration is NUL-safe and bounded.** `git config -z
  --get-regexp` output is parsed on NUL boundaries, never
  newline-split (a driver name or value could legitimately contain
  characters a naive line-based parser would misread). Driver names are
  deduplicated by exact string equality, including names containing
  dots (a Git config subsection matches verbatim between the first and
  last dot). At most 128 distinct drivers, at most 256 bytes per driver
  name, and at most 65,536 bytes of total generated `-c` argv payload
  are permitted; malformed, NUL-invalid, or over-limit configuration is
  a structured `GitSafetyError`, never a partial or silently-degraded
  neutralization.
- **Attribute inspection is NUL-safe and framing-checked.**
  `parse_check_attr_output` parses `check-attr -z` output (flat
  `<path>\\0<attribute>\\0<value>\\0` triples) without any newline-based
  splitting, and requires a terminal NUL so truncated output is never
  mistaken for complete output even when its field count happens to
  divide by three. Empty paths and empty attribute names are refused.
- **Filter classification is driver-set dependent, never context-free.**
  `git check-attr` prints the literal string `unset` both for a
  genuinely unset attribute (`a.txt -filter`) and for an explicit
  assignment naming a driver called `unset` (`b.txt filter=unset`), and
  the same collision exists for `unspecified`. Positive controls
  against a real repository confirmed the named drivers really execute
  while the genuine cases do not — so the reported string alone cannot
  classify `filter` safety, and a context-free rule would let host code
  run during the very checkout ADR 0006 §1 relies on attribute
  inspection to protect. `is_safe_filter_state` therefore takes the
  configured driver-name set (from `enumerate_filter_neutralization`'s
  immutable `driver_names`) and treats `unset`/`unspecified` as safe
  only when that exact string is not also a configured driver name,
  refusing conservatively when it is. `is_safe_non_filter_state` keeps
  the plain state classification for `text`/`eol`/`ident`/
  `working-tree-encoding`/`crlf`, whose values Git does not resolve as
  driver names.
- **One public execution API.** `run_git` is the only public way to
  execute a Git command here; argv construction is the private
  `_build_git_argv` seam, so an integrating module is never handed a
  ready-made argv it could pass to a bare `subprocess` call, silently
  losing the sanitized environment, timeout, or error classification.
- **Sanitized errors.** Messages are fixed, categorical text. Raw Git
  stderr and filesystem paths never appear in a raised message.

No network and no Docker. Filter-neutralization tests exercise real
throwaway repositories with real hostile filter commands and markers;
only the version/option-recognition failure paths that cannot be
provoked with the single real installed Git monkeypatch the `_run`
seam, matching `checkpoint_ref`'s existing testing convention.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Collection
from dataclasses import dataclass
from enum import Enum, unique
from pathlib import Path

# Bounded so a hung Git invocation cannot stall a caller indefinitely.
# Matches codeagent.checkpoint_ref.GIT_TIMEOUT_SECONDS.
GIT_TIMEOUT_SECONDS = 30.0

# Git's global --no-lazy-fetch option was introduced in Git 2.45; ADR
# 0006 establishes this as CodeAgent v1's minimum supported Git version
# (docs/adr/0006-...-content-fidelity.md, "Partial-clone / lazy-fetch
# safety").
MIN_GIT_VERSION = (2, 45)

# Fixed, verified absolute path — never shutil.which, never a
# PATH-resolved name. Only valid as a clean/smudge passthrough; never
# as a .process override (protocol-incompatible, ADR 0006 finding 10).
FIXED_CAT_PATH = "/bin/cat"

MAX_FILTER_DRIVERS = 128
MAX_FILTER_DRIVER_NAME_BYTES = 256
MAX_FILTER_ARGV_BYTES = 65_536

# unspecified: no rule at all. unset: an explicit "-attr" negation.
# "set" (bare boolean-true) and any named value are unsafe.
#
# For `filter` these two strings are NOT sufficient on their own: see
# `is_safe_filter_state`. `git check-attr` prints the literal string
# "unset" both for a genuinely unset attribute (`-filter`) and for an
# explicit assignment naming a driver called "unset" (`filter=unset`),
# and likewise for "unspecified" — and a driver so named really does
# execute. Classifying `filter` therefore requires the configured
# driver-name set as context.
SAFE_ATTRIBUTE_STATES = frozenset({"unspecified", "unset"})

# The one attribute whose value Git resolves as a configured driver
# name, which is what makes its classification context-dependent.
FILTER_ATTRIBUTE_NAME = "filter"

# ADR 0006 section 5's exact hardened baseline, applied to every
# governed invocation. Order is fixed for determinism; git accepts
# global options and -c overrides in any order before the subcommand.
BASELINE_ARGS: tuple[str, ...] = (
    "--no-pager",
    "--no-lazy-fetch",
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.autocrlf=false",
    "-c",
    "submodule.recurse=false",
)

# Strict, fully anchored: the numeric core, then only suffix shapes
# real Git builds actually produce. Trailing junk that does not belong
# to a recognized platform suffix (e.g. "2.45evil") must not parse as a
# version at all, since a spoofed or corrupted version string is
# exactly what the second preflight check exists to catch.
_VERSION_RE = re.compile(
    r"^git version (\d+)\.(\d+)(?:\.\d+)*"  # 2.45 / 2.45.0 / 2.45.0.1
    r"(?:\.(?:rc\d+|windows\.\d+|msysgit\.\d+))?"  # .rc1 / .windows.1 / .msysgit.1
    r"(?:-rc\d+)?"  # -rc1
    r"(?: \([A-Za-z0-9 ._+-]+\))?"  # (Apple Git-157) / (Debian)
    r"$"
)


@unique
class GitSafetyFailure(str, Enum):
    """Categorical reason a Git-safety operation failed or refused.
    Callers branch on this; `GitSafetyError.message` is for humans and
    must never be parsed."""

    GIT_EXECUTABLE_UNAVAILABLE = "git_executable_unavailable"
    GIT_COMMAND_TIMEOUT = "git_command_timeout"
    GIT_VERSION_CHECK_FAILED = "git_version_check_failed"
    MALFORMED_VERSION_OUTPUT = "malformed_version_output"
    UNSUPPORTED_GIT_VERSION = "unsupported_git_version"
    UNRECOGNIZED_LAZY_FETCH_OPTION = "unrecognized_lazy_fetch_option"
    FILTER_ENUMERATION_UNAVAILABLE = "filter_enumeration_unavailable"
    FILTER_ENUMERATION_MALFORMED = "filter_enumeration_malformed"
    FILTER_ENUMERATION_LIMIT_EXCEEDED = "filter_enumeration_limit_exceeded"
    FILTER_PASSTHROUGH_UNAVAILABLE = "filter_passthrough_unavailable"
    ATTRIBUTE_OUTPUT_MALFORMED = "attribute_output_malformed"


class GitSafetyError(Exception):
    """A Git-safety operation failed or was refused.

    `reason` is the stable, matchable identifier. `message` is already
    sanitized: fixed categorical text only. Callers that need an
    `OperationalError` translate this once, at the boundary that
    records the occurrence (same split as
    `codeagent.checkpoint_ref.CheckpointRefError`).
    """

    def __init__(self, reason: GitSafetyFailure, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class AttributeRecord:
    """One `(path, attribute, value)` triple from `check-attr` output."""

    path: str
    attribute: str
    value: str

    def is_safe(self, *, configured_driver_names: Collection[str]) -> bool:
        """Classify this record.

        `configured_driver_names` is a required keyword argument even
        for non-filter attributes, which ignore it: a caller must not
        be able to classify a `filter` record without having enumerated
        the repository's configured drivers first (see
        `is_safe_filter_state` for why context-free classification of
        `filter` is unsound).
        """
        if self.attribute == FILTER_ATTRIBUTE_NAME:
            return is_safe_filter_state(
                self.value, configured_driver_names=configured_driver_names
            )
        return is_safe_non_filter_state(self.value)


@dataclass(frozen=True)
class FilterNeutralization:
    """The bounded set of `-c` overrides that neutralize every
    discovered filter driver's `clean`/`smudge`/`process` subkeys that
    are actually configured, plus the exact set of driver names that
    were discovered.

    `driver_names` is the context `is_safe_filter_state` needs, and is
    exposed as an immutable `frozenset` so a caller cannot mutate the
    set a classification decision was made against.
    """

    args: tuple[str, ...]
    driver_names: frozenset[str]

    @property
    def driver_count(self) -> int:
        return len(self.driver_names)


def git_environment() -> dict[str, str]:
    """The environment every governed Git invocation runs with: the
    parent environment minus every `GIT_*` variable, then
    `GIT_NO_LAZY_FETCH=1` set deliberately (overriding any inherited
    value, hostile or not).

    See `codeagent.checkpoint_ref.git_environment()` for the stripping
    rationale, which applies identically here.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env["GIT_NO_LAZY_FETCH"] = "1"
    return env


def _build_git_argv(repo_path: Path | str | None, *args: str) -> list[str]:
    """Build a full `git` argv with the hardened baseline applied, and
    `-C <repo_path>` inserted (after the baseline, before the
    subcommand) when a repository is given. `repo_path` is `None` for
    invocations that predate repository access, such as preflight.

    Private on purpose: `run_git` is the only public execution API, so
    an integrating production module is never handed a ready-made argv
    it could pass to a bare `subprocess` call, silently losing the
    sanitized environment, the timeout, and the error classification.
    """
    argv = ["git", *BASELINE_ARGS]
    if repo_path is not None:
        argv += ["-C", str(repo_path)]
    argv += list(args)
    return argv


def _run(
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """The single subprocess seam. `args` excludes the leading `git`.
    Structured argv only: never a shell, never a string-built command.
    Raises `GitSafetyError` for a failure to launch or a hung
    invocation; a nonzero exit is returned for the caller to classify.

    Output is decoded with `errors="surrogateescape"`: repository
    content (a filter driver name, a file path) is untrusted and can
    legally contain bytes that are not valid UTF-8 (Git's own config
    grammar and most filesystems allow this) — strict decoding would
    raise an uncaught `UnicodeDecodeError` instead of a categorical
    `GitSafetyError`, defeating the fail-closed discipline this module
    exists to provide.
    """
    try:
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            errors="surrogateescape",
            timeout=timeout,
            env=git_environment(),
            input=input_text,
        )
    except subprocess.TimeoutExpired:
        # `TimeoutExpired.__str__` embeds the full argv, which can
        # include a real host repository path (`-C <repo>`). The
        # exception is deliberately not chained as __cause__, so a bare
        # traceback print can never surface it.
        raise GitSafetyError(
            GitSafetyFailure.GIT_COMMAND_TIMEOUT,
            "a git command did not finish within its time limit",
        ) from None
    except OSError as exc:
        raise GitSafetyError(
            GitSafetyFailure.GIT_EXECUTABLE_UNAVAILABLE,
            "the git executable could not be launched",
        ) from exc


def run_git(
    repo_path: Path | str | None,
    *args: str,
    timeout: float = GIT_TIMEOUT_SECONDS,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """The single safe entrypoint for a hardened Git invocation: builds
    the full baseline argv via `_build_git_argv` and executes it through
    the sanitized-environment, timeout-bounded, categorically-erroring
    `_run` seam, in one call.

    A later integrating slice (`workspace.py`/`patch.py`) calls this
    rather than composing an argv with its own bare `subprocess` call — doing so would silently drop the sanitized
    environment, the timeout, or the categorical error handling, and
    reintroduce exactly the `GIT_*`-redirection or hung-process risk
    this module exists to close.
    """
    argv = _build_git_argv(repo_path, *args)
    return _run(argv[1:], timeout=timeout, input_text=input_text)


def _parse_git_version(text: str) -> tuple[int, int] | None:
    """Parse `major.minor` out of `git --version` output (e.g. "git
    version 2.54.0", "git version 2.54.0 (Apple Git-157)", "git version
    2.45.0.windows.1", "git version 2.45.0.rc1").

    Returns `None` — never a best guess — for anything that is not
    exactly one line matching that grammar, including trailing junk
    ("2.45evil") and extra lines. The version is compared as a tuple of
    ints, so "2.9" correctly sorts below "2.45".
    """
    stripped = text.strip()
    if "\n" in stripped:
        return None
    match = _VERSION_RE.match(stripped)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)))


def check_git_preflight() -> None:
    """Capability/platform preflight, run once before any repository
    access (ADR 0006 §6). Two independent checks, either failure is
    unsupported substrate:

    1. `git --version` must parse and report `>= 2.45`.
    2. `git --no-pager --no-lazy-fetch --version` must succeed,
       proving the option is actually recognized — a version number
       alone is not sufficient proof.
    """
    version_result = _run(["--version"])
    if version_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.GIT_VERSION_CHECK_FAILED,
            "git --version did not complete successfully",
        )
    version = _parse_git_version(version_result.stdout)
    if version is None:
        raise GitSafetyError(
            GitSafetyFailure.MALFORMED_VERSION_OUTPUT,
            "git --version output could not be parsed as a git version string",
        )
    if version < MIN_GIT_VERSION:
        raise GitSafetyError(
            GitSafetyFailure.UNSUPPORTED_GIT_VERSION,
            "the installed git version is older than the minimum supported version",
        )

    option_result = _run(["--no-pager", "--no-lazy-fetch", "--version"])
    if option_result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.UNRECOGNIZED_LAZY_FETCH_OPTION,
            "the installed git did not recognize the --no-lazy-fetch option",
        )


def is_safe_non_filter_state(value: str) -> bool:
    """Classify a **non-filter** attribute state (`text`, `eol`,
    `ident`, `working-tree-encoding`, legacy `crlf`): `unspecified` and
    `unset` are safe; `set` and any explicit value are unsafe.

    Deliberately not usable for `filter` — that attribute's value is
    resolved by Git as a configured driver name, so its classification
    needs context (`is_safe_filter_state`).
    """
    return value in SAFE_ATTRIBUTE_STATES


def is_safe_filter_state(value: str, *, configured_driver_names: Collection[str]) -> bool:
    """Classify a `filter` attribute state against the repository's
    configured driver names.

    `git check-attr` reports the literal string `unset` both for a
    genuinely unset attribute (`a.txt -filter`) and for an explicit
    assignment naming a driver called `unset` (`b.txt filter=unset`);
    the same collision exists for `unspecified`. Positive controls
    against a real repository confirmed the named drivers really do
    execute while the genuine cases do not, so the reported string
    alone cannot distinguish them.

    The rule is therefore driver-set dependent, and conservative when
    ambiguous: `unset`/`unspecified` is safe only when that exact
    string is not also a configured driver name. Any other value —
    `set` or an ordinary named driver — is unsafe regardless.
    """
    if value not in SAFE_ATTRIBUTE_STATES:
        return False
    # Ambiguous: this path may be genuinely unset/unspecified, or may
    # be an explicit assignment to a live driver of that exact name.
    # Refuse rather than guess.
    return value not in configured_driver_names


def _split_nul_records(raw: str, *, reason: GitSafetyFailure, description: str) -> list[str]:
    """Split NUL-framed Git output into records, requiring a terminal
    NUL so truncated output is never mistaken for complete output.
    Returns `[]` for genuinely empty output."""
    if raw == "":
        return []
    if not raw.endswith("\0"):
        raise GitSafetyError(reason, f"{description} was truncated: it did not end with a NUL")
    return raw.split("\0")[:-1]


def parse_check_attr_output(raw: str) -> list[AttributeRecord]:
    """Parse `git check-attr -z [--cached] --stdin <attrs...>` output:
    a flat sequence of NUL-terminated `<path>\\0<attribute>\\0<value>\\0`
    triples, never newline-split.

    Raises `GitSafetyError` if the output is truncated (no terminal
    NUL, even when the field count happens to divide by three), does
    not decompose into complete triples, or contains an empty path or
    attribute name.
    """
    fields = _split_nul_records(
        raw,
        reason=GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
        description="git check-attr output",
    )
    if len(fields) % 3 != 0:
        raise GitSafetyError(
            GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
            "git check-attr output did not decompose into complete path/attribute/value triples",
        )
    records = []
    for i in range(0, len(fields), 3):
        path, attribute, value = fields[i], fields[i + 1], fields[i + 2]
        if path == "":
            raise GitSafetyError(
                GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
                "git check-attr output contained a record with an empty path",
            )
        if attribute == "":
            raise GitSafetyError(
                GitSafetyFailure.ATTRIBUTE_OUTPUT_MALFORMED,
                "git check-attr output contained a record with an empty attribute name",
            )
        records.append(AttributeRecord(path=path, attribute=attribute, value=value))
    return records


def _parse_filter_config(raw: str) -> dict[str, dict[str, str]]:
    """Parse `git config -z --get-regexp '^filter\\.'` output into
    `{driver_name: {subkey: value}}`, NUL-safe throughout. A driver name
    is everything between the fixed `filter.` prefix and the final
    `.<subkey>` component, so a name containing dots (a valid Git
    config subsection) is never mis-split."""
    records = _split_nul_records(
        raw,
        reason=GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
        description="git config output",
    )

    drivers: dict[str, dict[str, str]] = {}
    for record in records:
        if "\n" in record:
            key, value = record.split("\n", 1)
        else:
            # A bare-boolean config entry (e.g. `[filter "x"]\n\tclean`
            # with no `=value`) is emitted as `key` alone, with no
            # embedded newline at all — a real, valid Git config shape
            # (Git's own canonical boolean-true string), not malformed
            # input.
            key, value = record, "true"
        if not key.startswith("filter."):
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
                "git config reported a key outside the filter.* namespace",
            )
        remainder = key[len("filter."):]
        if "." not in remainder:
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
                "git config reported a filter key with no driver name or subkey",
            )
        driver_name, subkey = remainder.rsplit(".", 1)
        if not driver_name:
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_MALFORMED,
                "git config reported a filter key with an empty driver name",
            )
        drivers.setdefault(driver_name, {})[subkey] = value
    return drivers


def enumerate_filter_neutralization(repo_path: Path | str) -> FilterNeutralization:
    """Enumerate `filter.*` configuration for `repo_path` and build the
    bounded set of `-c` overrides that neutralize every discovered
    driver's `clean`/`smudge`/`process` subkeys — only the subkeys
    actually configured, never a blind override, and never
    `required=false` (ADR 0006 §1/finding 10).

    Raises `GitSafetyError` for malformed output, or if the discovered
    configuration exceeds the fixed bounds: at most
    `MAX_FILTER_DRIVERS` distinct drivers, at most
    `MAX_FILTER_DRIVER_NAME_BYTES` bytes per driver name, and at most
    `MAX_FILTER_ARGV_BYTES` bytes of total generated argv payload.
    """
    result = run_git(repo_path, "config", "-z", "--get-regexp", r"^filter\.")
    if result.returncode == 1 and result.stdout == "":
        return FilterNeutralization(args=(), driver_names=frozenset())
    if result.returncode != 0:
        raise GitSafetyError(
            GitSafetyFailure.FILTER_ENUMERATION_UNAVAILABLE,
            "the repository's filter configuration could not be enumerated",
        )

    drivers = _parse_filter_config(result.stdout)

    if len(drivers) > MAX_FILTER_DRIVERS:
        raise GitSafetyError(
            GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED,
            "the repository configures more filter drivers than the fixed safety bound allows",
        )
    for name in drivers:
        # surrogateescape mirrors _run's decoding, so a driver name
        # containing non-UTF-8 bytes is measured by its real byte
        # length instead of raising UnicodeEncodeError on the escaped
        # surrogate codepoints.
        if len(name.encode("utf-8", "surrogateescape")) > MAX_FILTER_DRIVER_NAME_BYTES:
            raise GitSafetyError(
                GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED,
                "a configured filter driver name exceeds the fixed safety length bound",
            )

    needs_passthrough = any("clean" in subkeys or "smudge" in subkeys for subkeys in drivers.values())
    if needs_passthrough:
        _verify_fixed_cat_path()

    overrides: list[str] = []
    total_bytes = 0
    for name in sorted(drivers):
        subkeys = drivers[name]
        entries = []
        if "clean" in subkeys:
            entries.append(f"filter.{name}.clean={FIXED_CAT_PATH}")
        if "smudge" in subkeys:
            entries.append(f"filter.{name}.smudge={FIXED_CAT_PATH}")
        if "process" in subkeys:
            entries.append(f"filter.{name}.process=")
        for entry in entries:
            overrides.append("-c")
            overrides.append(entry)
            # Count the argument bytes plus a NUL terminator per
            # argument, matching ADR 0006 §1's "including encoded bytes
            # and argument terminators".
            total_bytes += len(b"-c\0") + len(entry.encode("utf-8", "surrogateescape")) + 1

    if total_bytes > MAX_FILTER_ARGV_BYTES:
        raise GitSafetyError(
            GitSafetyFailure.FILTER_ENUMERATION_LIMIT_EXCEEDED,
            "the generated filter-neutralization argv exceeds the fixed safety size bound",
        )

    return FilterNeutralization(args=tuple(overrides), driver_names=frozenset(drivers))


def _verify_fixed_cat_path() -> None:
    """Confirm `FIXED_CAT_PATH` exists, is a regular file (following
    symlinks — many systems symlink `/bin` to `/usr/bin`), and is
    executable, before it is embedded into any `-c filter.<name>.clean=`
    or `.smudge=` override. A missing or non-executable passthrough
    would otherwise only surface as a confusing failure from the later
    Git invocation that actually tries to run it."""
    if not (os.path.isfile(FIXED_CAT_PATH) and os.access(FIXED_CAT_PATH, os.X_OK)):
        raise GitSafetyError(
            GitSafetyFailure.FILTER_PASSTHROUGH_UNAVAILABLE,
            "the fixed filter passthrough executable is not available on this host",
        )
