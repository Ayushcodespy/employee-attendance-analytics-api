<!-- # DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** Which indexes did you create, and which query or guarantee does each one serve? Which one did you consider and reject?
2. **Punch-in race.** Two identical punch-in requests arrive at the same instant. Walk through exactly what happens in your code.
3. **Ties.** Two employees are tied on late minutes at the cutoff of `limit`. What does your leaderboard return, and why?
4. **Headcount.** How does your department summary make sure employees with zero logs are counted?
5. **One thing you would change** if this had to serve 100x the data.

 -->


## Technical Decisions

1. **Indexes.** A unique `employees.emp_code` index prevents duplicate employee codes. A unique compound `attendance_logs (emp_code, date)` index guarantees at most one attendance record per employee per date and protects concurrent punch-ins. Date-based attendance indexes support date-range listing and sorting; `(emp_code, date)` supports employee/date lookups and monthly queries. The employee joined-date/department index supports headcount filtering. Index choices should be rechecked with `explain()` on representative data; a department-first index may help equality filters.

2. **Punch-in race.** Two identical requests may both pass the pre-check. Both attempt insertion, but the unique compound index permits only one insert. The other raises `DuplicateKeyError`, translated to `409 Conflict`. The database constraint is the final guarantee.

3. **Leaderboard ties.** The leaderboard uses standard competition ranking: tied employees share a rank and the next rank is skipped (for example, `1, 2, 2, 4`). `limit` applies to rank, not row count, so ties at the cutoff are all returned. Rows are ordered by total late minutes descending and then `emp_code` ascending for deterministic output.

4. **Headcount.** Department summary starts from employees who joined on or before the selected month's end and looks up their attendance logs. Employees with no logs still count toward headcount. The trend uses the same joined-date principle for each calendar day.

5. **100x scale.** I would run explain plans on representative data, compare keys/documents examined with returned rows, benchmark common queries, and tune compound-index order to actual filters. I would also review aggregation memory/use and add automated regression tests before further optimization.
