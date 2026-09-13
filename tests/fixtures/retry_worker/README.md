# retry_worker fixture

A minimal, intentionally buggy toy repository used only by
Milestone 1 slice B's integration tests. It is never used directly —
tests copy this directory into a temporary location and run `git init`
there to create a real, throwaway Git repository per test. No `.git`
directory is committed here.
