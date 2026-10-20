"""ADR 0005 Amendment 1 section 6 (ledger D3): the pinned inventory of
production process launches, by individual call node (D3 plan section 6.2).

Every production launch must be one of exactly seven call nodes (L1-L7),
each passing the literal `start_new_session=True`; the owned-`Popen` abort
paths must kill the process group; aliased, imported, assigned and dynamic
spellings of a launch API are forbidden. A launch name computed at runtime
(a string built dynamically) is detectable only by code review.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

import codeagent

SRC = Path(codeagent.__file__).parent

LAUNCH_APIS = {
    "subprocess": {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
    "pty": {"spawn", "fork"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}
_OS_PREFIXES = ("spawn", "exec", "posix_spawn")
_OS_NAMES = {"system", "popen", "fork", "forkpty"}
LAUNCH_MODULES = {"subprocess", "os", "pty", "asyncio", "multiprocessing"}

EXPECTED = Counter(
    {
        ("_bounded_subprocess", "run_bounded_stdout", "Popen"): 1,  # L1
        ("executor", "DockerVerifier._start_and_inspect", "Popen"): 1,  # L2
        ("_git_safety", "_run", "run"): 1,  # L3
        ("_git_safety", "_read_bounded", "Popen"): 1,  # L4
        ("_git_safety", "_read_bounded_preview", "Popen"): 1,  # L5
        ("checkpoint_ref", "_run_git", "run"): 1,  # L6
        ("checkpoint_ref", "_RefTransaction.__enter__", "Popen"): 1,  # L7
    }
)
# The owned-Popen abort paths (L1, L2, L4, L5, L7) that must call os.killpg.
GROUP_KILL_SCOPES = {
    ("_bounded_subprocess", "_terminate_and_confirm"),  # L1
    ("executor", "_abort_attach_client"),  # L2 (the attach-client abort)
    ("_git_safety", "_terminate_and_confirm"),  # L4, L5
    ("checkpoint_ref", "_RefTransaction.__exit__"),  # L7
}
NON_CALL_ALLOWED = {"CompletedProcess", "PIPE", "DEVNULL", "TimeoutExpired"}


def _is_launch_attr(node: ast.Attribute) -> str | None:
    if not isinstance(node.value, ast.Name):
        return None
    module, attr = node.value.id, node.attr
    if module in LAUNCH_APIS and attr in LAUNCH_APIS[module]:
        return attr
    if module == "os" and (attr in _OS_NAMES or attr.startswith(_OS_PREFIXES)):
        return attr
    return None


class _Scan(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.scope: list[str] = []
        self.calls: list[tuple[str, str, str, int, ast.Call]] = []
        self.killpg_scopes: set[tuple[str, str]] = set()
        self.annotation_ids: set[int] = set()
        self.violations: list[str] = []

    def _qual(self) -> str:
        return ".".join(self.scope)

    def _mark_annotation(self, node) -> None:
        if node is not None:
            for sub in ast.walk(node):
                self.annotation_ids.add(id(sub))

    def visit_FunctionDef(self, node) -> None:
        for arg in node.args.args + node.args.kwonlyargs + node.args.posonlyargs:
            self._mark_annotation(arg.annotation)
        self._mark_annotation(node.returns)
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_AnnAssign(self, node) -> None:
        self._mark_annotation(node.annotation)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            api = _is_launch_attr(func)
            if api is not None:
                self.calls.append((self.module, self._qual(), api, node.lineno, node))
            if isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr == "killpg":
                self.killpg_scopes.add((self.module, self._qual()))
        if isinstance(func, ast.Name) and func.id in {"getattr", "__import__"}:
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    name = arg.value
                    launch_attr = any(name in apis for apis in LAUNCH_APIS.values()) or (
                        name in _OS_NAMES or name.startswith(_OS_PREFIXES)
                    )
                    if launch_attr or (func.id == "__import__" and name in LAUNCH_MODULES):
                        self.violations.append(f"dynamic {func.id} at {self.module}:{node.lineno}")
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "import_module"
            and any(isinstance(a, ast.Constant) and a.value in LAUNCH_MODULES for a in node.args)
        ):
            self.violations.append(f"importlib launch module at {self.module}:{node.lineno}")
        self.generic_visit(node)

    def visit_Import(self, node) -> None:
        for alias in node.names:
            root = alias.name.split(".")[0]
            if root in {"multiprocessing", "pty"}:
                self.violations.append(f"import {alias.name} in {self.module}")
            if root in {"subprocess", "os"} and alias.asname not in (None, root):
                self.violations.append(f"aliased import {alias.name} as {alias.asname} in {self.module}")

    def visit_ImportFrom(self, node) -> None:
        mod = (node.module or "").split(".")[0]
        names = {a.name for a in node.names}
        if mod in {"pty", "multiprocessing"}:
            self.violations.append(f"from {mod} import in {self.module}")
        if mod == "subprocess" and names & LAUNCH_APIS["subprocess"]:
            self.violations.append(f"from subprocess import launch API in {self.module}")
        if mod == "asyncio" and names & LAUNCH_APIS["asyncio"]:
            self.violations.append(f"from asyncio import launch API in {self.module}")
        if mod == "os" and any(n in _OS_NAMES or n.startswith(_OS_PREFIXES) for n in names):
            self.violations.append(f"from os import launch API in {self.module}")

    def visit_Assign(self, node) -> None:
        if isinstance(node.value, ast.Attribute) and _is_launch_attr(node.value):
            self.violations.append(f"launch API assigned to a name at {self.module}:{node.lineno}")
        self.generic_visit(node)


def _scan_all():
    scans = []
    for path in sorted(SRC.glob("*.py")):
        scan = _Scan(path.stem)
        tree = ast.parse(path.read_text())
        scan.visit(tree)
        scans.append((scan, tree))
    return scans


def test_exact_launch_call_node_multiplicity():
    """M26a: a second launch inside a listed function makes its count 2."""
    calls = [c for scan, _ in _scan_all() for c in scan.calls]
    assert Counter((m, s, api) for m, s, api, _, _ in calls) == EXPECTED
    assert len(calls) == 7


def test_every_launch_passes_literal_start_new_session_true():
    """M2/M3/M26f: the literal constant True, never a variable or **kwargs."""
    for scan, _ in _scan_all():
        for module, scope, api, line, node in scan.calls:
            assert not any(k.arg is None for k in node.keywords), f"**kwargs at {module}:{line}"
            values = [k.value for k in node.keywords if k.arg == "start_new_session"]
            assert len(values) == 1, f"{module}.{scope}:{line} lacks start_new_session"
            assert isinstance(values[0], ast.Constant) and values[0].value is True, f"{module}:{line}"


def test_owned_popen_abort_paths_kill_the_process_group():
    """M4."""
    found = set().union(*(scan.killpg_scopes for scan, _ in _scan_all()))
    assert GROUP_KILL_SCOPES <= found


def test_no_aliased_imported_assigned_or_dynamic_launch_spelling():
    """M26b-M26e."""
    violations = [v for scan, _ in _scan_all() for v in scan.violations]
    assert violations == []


def test_non_call_launch_references_are_annotations_or_allowed_names():
    for scan, tree in _scan_all():
        call_funcs = {id(c[4].func) for c in scan.calls}
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "subprocess":
                if node.attr in NON_CALL_ALLOWED or id(node) in call_funcs:
                    continue
                assert id(node) in scan.annotation_ids, f"subprocess.{node.attr} at {scan.module}:{node.lineno}"
            if isinstance(node, ast.Attribute) and _is_launch_attr(node) and node.value.id != "subprocess":
                assert id(node) in call_funcs, f"{node.value.id}.{node.attr} at {scan.module}:{node.lineno}"


def test_detector_recognizes_each_forbidden_spelling():
    """The scanner itself catches the six M26 mutation shapes."""
    samples = {
        "from subprocess import Popen as _P\n_P(['x'], start_new_session=True)\n": "violation",
        "import subprocess as sp\nsp.run(['x'])\n": "violation",
        "import subprocess\nlauncher = subprocess.Popen\n": "violation",
        "import subprocess\ngetattr(subprocess, 'Popen')(['x'])\n": "violation",
        "import multiprocessing\n": "violation",
    }
    for source, _ in samples.items():
        scan = _Scan("sample")
        scan.visit(ast.parse(source))
        assert scan.violations, source
    scan = _Scan("sample")
    scan.visit(ast.parse("import subprocess\nflag = True\nsubprocess.Popen(['x'], start_new_session=flag)\n"))
    node = scan.calls[0][4]
    assert not isinstance([k for k in node.keywords if k.arg == "start_new_session"][0].value, ast.Constant)
