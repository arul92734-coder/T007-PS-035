# Strict EXPRESS-first route sequencing – verification note (T007 / PS-035)

## Finding
The uploaded project **already enforces** strict priority; no production code needed changing.

* **Who owns the final order:** `ps035/route_sequencing.py::sequence_route`. `ps035/assignment.py` never reorders:
  it calls `sequence_route` for each candidate rider and publishes `RiderAssignment.parcel_ids = route.parcel_ids`.
* **How the guarantee holds (route_sequencing.py):**
  1. parcels are split into an EXPRESS group and a STANDARD group, each ordered by `window_end`, then `parcel_id`;
  2. the EXPRESS block is built first with nearest insertion over positions `0..express_count`;
  3. STANDARD parcels may only use positions `express_count..len(route)`, i.e. at/after the end of the EXPRESS block;
  4. every candidate is re-timed from the hub (windows, 09:30 rule for an EXPRESS first stop, return by shift_end + 60 min);
  5. an EXPRESS parcel that fits nowhere goes to `unsequenced` – it is never retried behind STANDARD stops;
  6. an internal safety net raises `RuntimeError` if the final route is ever not `[EXPRESS…] + [STANDARD…]`.
* **Assignment layer:** parcels are processed EXPRESS first; a rider is feasible only if `sequence_route` sequences all of
  that rider's parcels, so the sequencer's strict order is what ends up in the result.

## Evidence
* Independent fuzz (outside the project tests): 1,500 random sequencer problems + 300 random fleet pools → **0 violations**.
  The same harness against the pre-fix (non-strict) sequencer reports 613 "STANDARD before/between EXPRESS", 386 "first stop not EXPRESS"
  and 324 assignment-level violations, so it can detect the bug.
* `tests/test_strict_priority.py` (new, 18 tests): 13 of them fail against the pre-fix sequencer; all 18 pass on the current code.

## Coverage added (gaps found in the existing tests)
* nearest insertion *positions* inside the EXPRESS group, and inside the STANDARD group (also an independent
  oracle re-deriving the order for 300 random slack-window problems, ties to the earlier position);
* a STANDARD parcel is not inserted **between** two EXPRESS stops even when that is clearly shorter (the existing tests covered only "in front");
* STANDARD insertion never changes the EXPRESS block; an infeasible EXPRESS parcel neither blocks other EXPRESS/STANDARD parcels nor lets STANDARD jump ahead;
* assignment level: order is passed through unchanged, an unassignable EXPRESS does not disturb other parcels, the early rider receives the EXPRESS parcel,
  a STANDARD-only route may start after 09:30, and random pools respect capacity with every route strict.

## Results (real pytest 9.1.1, Python 3.13)
```
python -m pytest tests/test_strict_priority.py -q                                        -> 18 passed in 1.38s
python -m pytest tests/test_route_sequencing.py tests/test_assignment.py tests/test_strict_priority.py -q  -> 131 passed in 37.97s
python -m pytest -q                                                                      -> 665 passed in 48.03s   (647 + 18 new)
```
Unchanged and verified: `ps035/route_sequencing.py`, `ps035/assignment.py`, all existing tests, data CSVs (SHA256SUMS OK),
`commands.txt`. **No Weekly Payout / TDS file was modified** (`weekly_payout.py`, `test_weekly_payout.py`,
`test_weekly_payout_sample.py` are byte-identical to the previously delivered TDS-correction versions).

## Assumptions / known behaviour (documented, not changed)
* Greedy heuristic: an EXPRESS parcel is processed by `window_end` and tried against the route built so far. One that cannot be a
  first stop by 09:30 *and* is processed before any other EXPRESS stop exists is refused even if it would have fitted behind a later EXPRESS stop.
* Strict priority can make the tour longer than an unrestricted one (that is the approved trade-off).
