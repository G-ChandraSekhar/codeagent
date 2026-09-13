"""Toy job-processing worker used only as a Milestone 1 slice B fixture.

Not a real dependency of codeagent — copied into a temporary directory
and turned into its own throwaway Git repository by the integration
tests that use it. The bug marker below is what the fixture's single
controlled patch operation replaces.
"""


def process_job(job, already_processed):
    # BUG: the retry path re-processes a job whose idempotency key was
    # already recorded, because the check happens after the retry
    # counter is incremented instead of before.
    job.retry_count += 1
    if job.idempotency_key in already_processed:
        return "duplicate"
    already_processed.add(job.idempotency_key)
    return "processed"
