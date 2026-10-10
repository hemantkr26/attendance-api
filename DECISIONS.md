# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date desc)` on logs: stops duplicate punch-ins and serves per-employee lists, the monthly summary and the trend. `(date desc, emp_code)` serves date ranges, the default sort, the summary and the leaderboard. `(status, date, emp_code)` serves the status filter. `(emp_code, punch_in)` finds the record to punch out. Employees: unique `emp_code`, `(department, joined_on)`. I rejected a separate `late_minutes` index: the monthly range already narrows the data.
2. **Punch-in race.** Both requests try `insert_one`. The unique index lets one succeed (201); the other gets `DuplicateKeyError` and I return 409. No read happens first.
3. **Ties.** I rank with `$rank` (1, 2, 2, 4) and only then keep `rank <= limit`, so a tie at the cutoff returns every tied person, sometimes more than `limit` rows.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and looks up each person's logs, so someone with zero logs still counts once.
5. **100x data.** Pre-aggregate daily and monthly totals per employee (written at punch time) instead of scanning raw logs for every analytics call.
