"""Milestone 1 slice C: the fixture's one real test.

Stdlib unittest only — no third-party dependency, so the verification
container never needs to install anything. Fails against the current
buggy jobs.worker.process_job (retry_count is incremented even on a
call that turns out to be a duplicate); passes once the patch moves the
increment after the duplicate check.
"""

import unittest

from jobs.worker import process_job


class _FakeJob:
    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        self.retry_count = 0


class ProcessJobTests(unittest.TestCase):
    def test_duplicate_call_does_not_inflate_retry_count(self) -> None:
        job = _FakeJob("job-1")
        already_processed: set[str] = set()

        first = process_job(job, already_processed)
        self.assertEqual(first, "processed")
        self.assertEqual(job.retry_count, 1)

        second = process_job(job, already_processed)
        self.assertEqual(second, "duplicate")
        # The bug: retry_count keeps climbing even when the call is
        # immediately short-circuited as a duplicate. A duplicate
        # detection is not a genuine retry attempt.
        self.assertEqual(job.retry_count, 1)


if __name__ == "__main__":
    unittest.main()
