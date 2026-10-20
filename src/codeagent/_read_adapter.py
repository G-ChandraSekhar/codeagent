"""Transitional legacy read path (ledger D4a; deleted in D4b with
`RepositoryReader`, `ReadResult` and `_dispatch_read_file`).

`RepoToolsReaderAdapter` implements today's `controller.RepositoryReader`
over the descriptor-relative `repo_tools.RepoTools`, replacing the deleted
`reader.WorktreeFileReader`. It deliberately does **not** catch
`tool_schema.ToolCleanupUnconfirmedError`: that error passes through to the
controller's single terminal route (D4 plan revision 5 §7.4–§7.5).
"""

from __future__ import annotations

from codeagent.controller import ReadResult
from codeagent.repo_tools import RepoTools


class RepoToolsReaderAdapter:
    def __init__(self, tools: RepoTools) -> None:
        if not isinstance(tools, RepoTools):
            raise TypeError("RepoToolsReaderAdapter requires RepoTools")
        self._tools = tools

    def read_file(self, relative_path: str) -> ReadResult:
        outcome = self._tools.read_whole_file(relative_path)
        if outcome.ok:
            assert outcome.payload is not None
            return ReadResult(
                success=True, content=outcome.payload["content"], byte_count=outcome.payload["byte_count"]
            )
        return ReadResult(success=False, content=None, byte_count=0, error=outcome.error)
