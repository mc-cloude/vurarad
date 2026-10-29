# Restore drill log

Operational record of the quarterly restore drill (`.github/workflows/restore-drill.yml`).
Each run appends one row: the date, the commit SHA under test, the measured
wall-clock RTO in minutes, the pass/fail result, and the GitHub Actions run id.

**Cadence:** quarterly. A missing quarter is itself the finding (§10.9) —
§164.308(a)(7)(ii)(B) requires a data recovery plan, and an untested restore
procedure is indistinguishable from no restore procedure.

**Pass criterion:** the drill completes end to end (scratch database created,
latest export imported, pinned image booted `--no-traffic`, smoke test green)
with a measured wall-clock RTO **under 240 minutes (4 h)**. Anything else is a
finding, recorded as `FAIL` and investigated before the next quarter.

<!-- Rows are appended automatically by .github/workflows/restore-drill.yml.
     The first row will appear after the initial post-apply drill run. -->

| Date (UTC) | Commit | RTO (min) | Result | Run ID |
|---|---|---|---|---|
