# DECISIONS.md

1. **Indexes.** On attendance logs I made four. The first is unique on employee and date. It stops double punch-ins and speeds up per-employee lookups, the monthly summary and the trend. The second is on date then employee, for date ranges, the default sort, the department summary and the leaderboard. The third starts with status, for the status filter. The fourth is employee then punch-in time, so punch-out can find the open record quickly. On employees I added a unique emp_code index and one on department plus joined date. I did not add an index on late minutes, because the month range already narrows the data and an extra index would slow down writes.

2. **Punch-in race.** Both requests try to insert. The unique index lets only one of them in. The other gets a duplicate-key error and I return 409. I never check first and insert later.

3. **Ties.** I rank everyone first, and only then apply the limit. If two people tie at the cutoff, both are returned, so the list can be longer than the limit. The next rank after a tie is skipped.

4. **Headcount.** The department summary starts from the employees collection, not the logs, and then looks up each person's logs. Someone with no logs still counts once.

5. **100x data.** I would store daily totals per employee when people punch in or out, so analytics read small summary records instead of scanning every raw log.