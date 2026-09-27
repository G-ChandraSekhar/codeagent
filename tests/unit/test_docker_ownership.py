"""Tests for `codeagent._docker_ownership` (Milestone 3 Slice 3B-6).

Generalizes the strict `docker ps -a`/`docker inspect` parsing-grammar
matrix that previously existed only as `codeagent.reconciliation`'s own
private tests. `codeagent.reconciliation`'s own test file keeps only
small call-site/delegator regression tests (it still issues its own
`run_bounded_stdout` call and delegates parsing here)."""

from __future__ import annotations

import pytest

from codeagent import _docker_ownership as do
from codeagent._bounded_subprocess import BoundedProcessError, BoundedProcessFailure, BoundedProcessResult


def _fake_run(result: BoundedProcessResult):
    def _run(argv, **kwargs):
        return result

    return _run


# ---------------------------------------------------------------------------
# parse_ps_all_output
# ---------------------------------------------------------------------------


def test_parse_ps_all_output_empty_is_valid():
    name_to_id, id_to_name = do.parse_ps_all_output(b"")
    assert name_to_id == {}
    assert id_to_name == {}


def test_parse_ps_all_output_parses_strict_two_field_rows():
    live_id = "9" * 64
    raw = f"{live_id}\tsome-name\n".encode()
    name_to_id, id_to_name = do.parse_ps_all_output(raw)
    assert name_to_id == {"some-name": live_id}
    assert id_to_name == {live_id: "some-name"}


def test_parse_ps_all_output_rejects_duplicate_id():
    live_id = "a" * 64
    raw = f"{live_id}\tname-one\n{live_id}\tname-two\n".encode()
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


def test_parse_ps_all_output_rejects_duplicate_name():
    raw = f"{'a' * 64}\tsame-name\n{'b' * 64}\tsame-name\n".encode()
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


def test_parse_ps_all_output_rejects_missing_trailing_newline():
    raw = f"{'a' * 64}\tname\n{'b' * 64}\tname2".encode()
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


def test_parse_ps_all_output_rejects_leading_blank_row():
    raw = f"\n{'a' * 64}\tname\n".encode()
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


def test_parse_ps_all_output_rejects_internal_blank_row():
    raw = f"{'a' * 64}\tname-one\n\n{'b' * 64}\tname-two\n".encode()
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


def test_parse_ps_all_output_rejects_wrong_field_count():
    raw = f"{'a' * 64}\tname\textra\n".encode()
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


def test_parse_ps_all_output_rejects_malformed_id():
    raw = f"{'a' * 63}\tname\n".encode()  # 63 chars, too short
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


@pytest.mark.parametrize("bad_name", ["", " leading-space", "has\rcr", "has\x00nul", "-leading-dash"])
def test_parse_ps_all_output_rejects_malformed_name(bad_name):
    raw = f"{'a' * 64}\t{bad_name}\n".encode()
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(raw)


def test_parse_ps_all_output_rejects_invalid_utf8():
    with pytest.raises(do.DockerListingError):
        do.parse_ps_all_output(b"\xff\xfe\n")


# ---------------------------------------------------------------------------
# parse_inspect_output
# ---------------------------------------------------------------------------


def test_parse_inspect_output_parses_strict_three_field_row():
    candidate_id = "b" * 64
    raw = f'{candidate_id}\t/codeagent-baseline-{"c" * 32}\t{{"codeagent.lifecycle.schema":"1"}}\n'.encode()
    proof = do.parse_inspect_output(raw)
    assert proof.id == candidate_id
    assert proof.name == f"codeagent-baseline-{'c' * 32}"
    assert proof.labels == {"codeagent.lifecycle.schema": "1"}


def test_parse_inspect_output_treats_null_labels_as_empty_dict():
    candidate_id = "d" * 64
    raw = f"{candidate_id}\t/some-name\tnull\n".encode()
    proof = do.parse_inspect_output(raw)
    assert proof.labels == {}


def test_parse_inspect_output_rejects_missing_leading_slash():
    raw = f'{"a" * 64}\tno-leading-slash\t{{}}\n'.encode()
    with pytest.raises(do.DockerInspectError):
        do.parse_inspect_output(raw)


def test_parse_inspect_output_rejects_carriage_return():
    raw = f'{"a" * 64}\t/name\r\t{{}}\n'.encode()
    with pytest.raises(do.DockerInspectError):
        do.parse_inspect_output(raw)


def test_parse_inspect_output_rejects_non_object_labels():
    raw = f'{"a" * 64}\t/name\t["not","an","object"]\n'.encode()
    with pytest.raises(do.DockerInspectError):
        do.parse_inspect_output(raw)


def test_parse_inspect_output_rejects_invalid_json_labels():
    raw = f'{"a" * 64}\t/name\t{{not valid json\n'.encode()
    with pytest.raises(do.DockerInspectError):
        do.parse_inspect_output(raw)


def test_parse_inspect_output_rejects_wrong_line_count():
    raw = f'{"a" * 64}\t/name\t{{}}\nextra-line\n'.encode()
    with pytest.raises(do.DockerInspectError):
        do.parse_inspect_output(raw)


# ---------------------------------------------------------------------------
# docker_ps_all_id_name_pairs / docker_inspect_ownership (run + parse)
# ---------------------------------------------------------------------------


def test_docker_ps_all_id_name_pairs_nonzero_exit_raises(monkeypatch):
    monkeypatch.setattr(do, "run_bounded_stdout", _fake_run(BoundedProcessResult(returncode=1, stdout=b"")))
    with pytest.raises(do.DockerListingError):
        do.docker_ps_all_id_name_pairs()


def test_docker_ps_all_id_name_pairs_launch_failure_raises(monkeypatch):
    def _boom(argv, **kwargs):
        raise BoundedProcessError(BoundedProcessFailure.LAUNCH_FAILED, "nope")

    monkeypatch.setattr(do, "run_bounded_stdout", _boom)
    with pytest.raises(do.DockerListingError):
        do.docker_ps_all_id_name_pairs()


def test_docker_inspect_ownership_nonzero_exit_never_confirms_absence(monkeypatch):
    monkeypatch.setattr(do, "run_bounded_stdout", _fake_run(BoundedProcessResult(returncode=1, stdout=b"")))
    with pytest.raises(do.DockerInspectError):
        do.docker_inspect_ownership("a" * 64)


def test_docker_ps_all_id_name_pairs_success_delegates_to_parser(monkeypatch):
    live_id = "9" * 64
    text = f"{live_id}\tsome-name\n"
    monkeypatch.setattr(
        do, "run_bounded_stdout", _fake_run(BoundedProcessResult(returncode=0, stdout=text.encode()))
    )
    name_to_id, id_to_name = do.docker_ps_all_id_name_pairs()
    assert name_to_id == {"some-name": live_id}
    assert id_to_name == {live_id: "some-name"}
