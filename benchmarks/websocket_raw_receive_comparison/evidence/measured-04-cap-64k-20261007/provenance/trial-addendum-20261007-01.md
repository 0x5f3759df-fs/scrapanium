# Preregistration and validation addendum

This note supplements trial-plan-20261007-01.md. It does not change the frozen runner, workload definitions, or thresholds.

The exact lifecycle-check progression is:
1. Initial focused collection: 133 passed, one failed in the stale lifecycle harness; original stdout/stderr was not saved.
2. After restoring the matching raw-receive test harness from the frozen candidate, the focused lifecycle test passed once under its ASan/UBSan wrapper; no separate raw output log was retained.
3. The cap-specific direct-read case was then added to that lifecycle invocation. The expanded invocation passed once under ASan/UBSan; the final saved rerun is operation-reuse-final-sanitized-02.log (1 passed in 2.08 seconds). This is one lifecycle test result, not 134 new passes.

The first extra attempt to save the final test output used system Python without the matched package PYTHONPATH and exited before collection with "No module named pytest"; its complete one-line output is retained separately. The successful rerun used the explicit local package snapshot path shown in targeted-validation-20261007-01.md.

The trial worktree has a local untracked .deps symlink to the frozen candidate worktree's .deps dependency links so the unchanged streaming helper and comparison runner can resolve the matched Python binding at candidate/.deps/venv-matched/bin/python. The dependency target is read-only and resolves to the same published curl/Bun/matched-Python inputs listed in the plan. The comparison runner records this candidate git status; its source inventory does not hash .deps, and it independently pins the installed package and DSO.
