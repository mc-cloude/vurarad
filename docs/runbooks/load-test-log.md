# Load and soak test log

Operational record of the load scenarios in `tests/load/` (§10.8). These run
against the staging project on demand and before each phase cutover — **never
against production.** Results are recorded with the date and commit SHA so a
regression has a baseline to be compared against.

`viewer_burst.py` exists specifically to falsify two numbers the plan asserts:
the signed-URL latency budget and the egress-per-open budget. Both are
load-bearing for the cost model, and the load run is how they stop being
assumptions.

## Pass criteria

| Scenario | Shape | Pass criteria |
|---|---|---|
| `worklist_soak.py` | 5 users polling `GET /api/v1/studies` every 30 s for 8 h, 2 concurrently opening studies | p95 < 300 ms; zero 5xx; Firestore reads within 10% of the §4.4 derivation; Cloud Run scales back to 0 afterwards |
| `viewer_burst.py` | 4 radiologists each opening an 824-instance series and scrolling it end to end within 10 min | every `access-urls` chunk p95 ≤ 600 ms; zero `signBlob` quota errors; measured bytes/open within the 120 MB budget (§9.1.1) |
| `ingest_soak.py` | 3 concurrent 4 GB / 4,000-object studies | all three complete; no lease collision; no duplicate instances; wall clock within the §3.7 estimate of 4–6 min each |

<!-- Rows are appended after each load run against staging. The first row will
     appear after the initial baseline run on a deployed staging environment. -->

## Results

| Date (UTC) | Commit | Scenario | Key metric | Value | Pass? | Notes |
|---|---|---|---|---|---|---|
