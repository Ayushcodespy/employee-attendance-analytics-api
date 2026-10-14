# REVIEW.md

This review records defects identified while implementing and testing the Employee Attendance & Analytics API. It separates confirmed fixes from follow-up checks; passing individual tests does not certify the entire API.

## 1. Defects Found and Fixed

| # | Where | What was wrong | How to notice | Fix / current status |
|---:|---|---|---|---|
| 1 | `GET /employees` pagination | Offset could skip the first page if calculated as `page * page_size`. | Request `page=1&page_size=20`. | Use `(page - 1) * page_size`. |
| 2 | `GET /employees` total | Filtered results could have an unfiltered total. | Compare filtered and unfiltered requests. | Apply the same query to `count_documents()` and record retrieval. |
| 3 | `GET /employees` sorting | Employee codes were not explicitly ordered. | Inspect returned code order. | Sort by `emp_code` ascending. |
| 4 | Employee/attendance listing validation | Invalid page/page-size values were not constrained. | Try `page=0` or `page_size=101`. | FastAPI constraints: `page >= 1`, `1 <= page_size <= 100`; invalid requests return `422`. |
| 5 | Punch-in employee lookup | Unknown employees could lead to missing shift details. | Punch in with unknown `emp_code`. | Check employee first; return `404`. |
| 6 | Punch-in missing timestamp | A falsy timestamp could be mistaken for a missing value. | Submit `punched_at: 0`. | Check `is None`; model validation rejects zero because it is outside the epoch-millisecond range (`422`). |
| 7 | Punch timestamp validation | Seconds-based or invalid timestamps must not be treated as epoch milliseconds. | Submit seconds instead of milliseconds or invalid types/ranges. | Strict integer and range validation; seconds-based timestamp test returned `422`. Not every malformed representation has been retested. |
| 8 | Punch-in attendance date | UTC date could differ from the local attendance date. | Test a timestamp near UTC midnight. | Convert instant to IST before deriving the attendance date. |
| 9 | Lateness calculation | UTC shift comparison could calculate the wrong delay. | For a 09:30 shift, punch at 10:43 IST. | Calculate using IST; observed lateness was 73 minutes. Grace applies only when delay is strictly greater than 10 minutes. |
| 10 | Timestamp precision | Fractional seconds could affect consistent calculations/storage. | Inspect fractional-second timestamps. | Truncate relevant timestamps to whole seconds. A dedicated precision regression test remains advisable. |
| 11 | Overnight shift date | After-midnight punch could be assigned to the wrong attendance date. | Test before the end of an overnight shift. | Assign the previous calendar date under the overnight-shift condition. Dedicated end-to-end test remains pending. |
| 12 | Punch-in status | Unsupported status strings could be accepted. | Submit `HOLIDAY`. | Restrict to `PRESENT`, `WFH`, `ON_DUTY`; invalid status returned `422`. |
| 13 | Startup indexes | Application-level checks alone do not protect against concurrent duplicates. | Inspect MongoDB indexes and try duplicates. | Unique `employees.emp_code` and unique `attendance_logs (emp_code, date)` indexes. |
| 14 | Duplicate punch-in race | Concurrent requests could both pass a pre-insert check. | Repeat punch-in for same employee/date. | Unique compound index is final guard; catch `DuplicateKeyError` and return `409`. Duplicate test returned `409`. |
| 15 | Punch-out validation order | Earlier punch-out initially returned `404` because lookup excluded the record before time validation. | Submit punch-out earlier than punch-in. | Find open record before validating elapsed time. Earlier timestamp returned `422`; valid punch-out `200`; repeat punch-out `409`. |
| 16 | Punch-out concurrent close | Two requests could try to close the same open record. | Submit simultaneous punch-out requests. | Conditional atomic update; return `409` if another request already closed it. Race has not been stress-tested. |
| 17 | Attendance PATCH null status | Explicit `status: null` was accepted and could be saved. | PATCH with null status. | Reject with `422 status cannot be null`; test passed and record was restored to `PRESENT`. |
| 18 | Attendance PATCH no-op | A correction changing nothing could create a misleading audit entry. | Submit unchanged status/times. | Reject with `422 no attendance changes detected`; test passed. |
| 19 | Attendance PATCH consistency/audit | Invalid status/time combinations must not persist, and changes need an audit trail. | Test `ABSENT` with times, missing punch-in for `PRESENT`, date mismatch, status changes. | Validate combinations and append actor/reason/time/changed fields to history. Nine PATCH scenarios below passed. |
| 20 | Department trend final sorting | Sort on `calendar_date` after projection removed that field. | Inspect final `$sort`/`$project` order. | Sort before final projection; tested output was ascending. |
| 21 | Admin explain department trend | `$documents` pipeline start was incompatible with the chosen collection-based explain setup. | Call trend explain with valid filters. | Seed from `employees` and explain that aggregation; request succeeded. |
| 22 | Admin explain attendance employee filter | `emp_code` was ignored by the explain branch. | Explain attendance list with `emp_code=EMP001` and dates. | Add employee filter. Retest showed `IXSCAN` on `emp_code_1_date_1`, one key and one document examined/returned. |
| 23 | Admin explain combined filters | Employee and department filters could disagree unless both were enforced. | `emp_code=EMP001&department=Research` when EMP001 is not in Research. | Verify membership; mismatch becomes `$in: []`. Explain returned zero documents examined/returned. |

## 2. Attendance PATCH Tests

| # | Scenario | Expected | Observed |
|---:|---|---|---|
| 1 | `PRESENT` → `WFH` | `200`, history entry | Passed |
| 2 | Repeat unchanged status | `422` no-op | Passed |
| 3 | Set `ABSENT` and clear times | `200`, times cleared, history entry | Passed |
| 4 | Non-null punch time with `ABSENT` | `422` | Passed |
| 5 | Set `PRESENT` without punch-in when none exists | `422` | Passed |
| 6 | Restore `PRESENT` with valid punch-in | `200`, history entry | Passed |
| 7 | Punch-in timestamp does not match URL date | `422` | Passed |
| 8 | Seconds-based timestamp | `422` | Passed |
| 9 | Explicit null status | `422` | Passed after fix |

## 3. Department Trend Verification

Tested: `GET /analytics/departments/Engineering/trend?from=2026-10-10&to=2026-10-13`

| Date | Working day | Headcount | Present count | Late count | Attendance rate | Moving average |
|---|---|---:|---:|---:|---:|---:|
| 2026-10-10 | No | 2 | 1 | 1 | `null` | `null` |
| 2026-10-11 | No | 2 | 0 | 0 | `null` | `null` |
| 2026-10-12 | Yes | 4 | 2 | 1 | 0.5 | 0.5 |
| 2026-10-13 | Yes | 4 | 0 | 0 | 0 | 0.25 |

- Date range is inclusive and rows were in ascending order.
- Weekend attendance rates are `null`.
- 2026-10-12 rate: `2 / 4 = 0.5`; 2026-10-13 rate: `0 / 4 = 0`.
- Moving average on 2026-10-13: `(0.5 + 0) / 2 = 0.25`, using available non-null rates inside the requested range.
- Seven-day range returned seven rows; 92-day inclusive range returned 92 rows.
- Range over 92 days and reversed range returned `422`; unknown department returned `404`.

## 4. Admin Explain Verification

### Attendance list

| Scenario | Observed result |
|---|---|
| `emp_code=EMP001` plus October date range | `200`; `IXSCAN` on `emp_code_1_date_1`; 1 key and 1 document examined/returned. |
| Engineering plus October date range | `200`; `IXSCAN` on `date_-1_emp_code_1`; 4 documents returned/examined. |
| `emp_code=EMP001` plus `department=Research` | `200`; always-false query; zero documents examined/returned. |
| Reversed date range | `422`, expected validation. |

The attendance-list explain branch uses `page_size`, not the separate `limit` parameter; `page_size=20` therefore appears as `limitAmount: 20`. This is consistent with the list pagination logic.

### Employee monthly

- Valid October request for `EMP001`: `200`, `IXSCAN` on `emp_code_1_date_1`.
- Missing `emp_code`: `422`.
- Department mismatch: always-false query with zero results.

### Department summary

- Valid October Engineering explain: `200`.
- Employee stage used an index scan on the joined-date/department index in the observed plan.
- Attendance lookup used `emp_code_1_date_1` and reported zero collection scans.
- Missing month: `422`.

### Department trend

- Valid explain returned execution statistics and used the `employees` collection.
- Lookup reported use of `emp_code_1_date_1`.
- The seed stage used a collection scan with a limit of one; examining one document on a small dataset is not, by itself, proof of a performance defect.

### Late leaderboard

- October Engineering explain returned `200`; attendance lookup used `emp_code_1_date_1`.
- On the small development dataset, MongoDB selected a collection scan for the initial department match. Re-check with representative data before treating this as a scalability defect.

## 5. Analytics Behavior Tested

- **Employee monthly:** October metrics were checked; November with no attendance returned zero metrics; unknown employee returned `404`; invalid month returned `422`.
- **Department summary:** month and department filters were checked; missing/invalid month returned `422`; an unknown department for a month with no matches returned `items: []`.
- **Late leaderboard:** ordering, department filtering, positive late totals, limit behavior and empty future-month behavior were exercised. The observed response did not include `late_count`; compare the response with `openapi.yaml` and resolve before submission.
- **Department trend:** date filling, weekends, headcount, rate, moving average, invalid range, maximum range and unknown department were exercised.

## 6. Reviewed but Not Classified as Defects

- No-op PATCH returning `422` is intentional validation.
- Repeated punch-out returning `409` is intentional conflict handling.
- Invalid timestamps/statuses returning `422` is expected validation.
- `404` when no open attendance record exists is expected and does not by itself prove a date-validation bug.
- Rejecting punch times for `ABSENT`/`LEAVE` prevents inconsistent records.
- Weekend trend rate `null` follows the contract.
- A collection scan on a tiny dataset is not alone proof of a defect; inspect execution statistics and representative data.
- Attendance-list explain uses `page_size` as its pagination limit; the `limit` parameter is for leaderboard semantics.

## 7. Remaining Checks Before Submission

1. Run dedicated timestamp precision and overnight-shift regression tests.
2. Compare every response shape with `openapi.yaml`, especially timestamp serialization consistency between `GET /attendance` and punch/PATCH responses.
3. Confirm the leaderboard response contains every contract field, especially `late_count`.
4. Run a compact regression suite for all endpoints and required error cases after final code changes.
5. Re-check explain plans against representative data if available.
6. Review README, requirements, OpenAPI and repository contents. Never commit `.env`, credentials or virtual environments.
