# REVIEW.md

Problems I found in the starter `app/main.py`, and how I fixed each one.

| # | Where | What was wrong | How you'd notice | How I fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It rounded minutes down first and then checked "more than 10", so 09:40:01 counted as 0 instead of 10. | Shift 09:30, punch 09:40:01: expected 10, got 0. | I now compare in seconds (more than 600), then divide by 60. |
| 2 | `compute_late_minutes` | It used the server's local time and date, never IST, and ignored overnight shifts. | On a UTC server, a 10:05 IST punch looks like 04:35. | I convert the punch to IST and build the shift start on the attendance date. |
| 3 | `compute_work_hours` | Python's `round()` is not round-half-up. | 18 seconds of work should give 0.01 hours, but `round` gives 0.0. | I use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It had no 30-minute minimum, put an overnight shift's end on the wrong day, mixed time-zone-aware and naive times (a crash), and no endpoint used it. | 10 minutes after shift end returned 10 instead of 0. | I rewrote it: shift end in IST (next day if overnight), whole minutes, 30 or more or else 0. |
| 5 | `punch_in` | An unknown `emp_code` crashed with a 500. | Send `emp_code: "NOPE"`. | It now returns 404. |
| 6 | `punch_in` | It checked first and inserted later, so two simultaneous requests both got in. | Send 10 identical requests at once. | I added a unique index on employee and date, and return 409 on a duplicate key. |
| 7 | `punch_in` | It used naive local time for the date, kept milliseconds, and treated `punched_at = 0` as "now". | A server in UTC, punch at 23:30 IST: wrong date. | I convert to UTC-aware time cut to whole seconds, compute the date by the IST rule, and check for `None`. |
| 8 | `PunchInIn` | It accepted seconds-looking timestamps, floats, negatives, and any status such as ABSENT. | `punched_at: 1783312500` or `status: "ABSENT"` returned 201. | Strict whole number in milliseconds, and status limited to PRESENT, WFH or ON_DUTY (else 422). |
| 9 | `punch_in` response | It returned raw dates and an `id`, which the contract doesn't have. | Compare the response with `openapi.yaml`. | A serializer returns epoch milliseconds and no id. |
| 10 | `create_employee` | Same check-then-insert race on `emp_code`. | Send parallel duplicate creates. | Unique index, and 409 on a duplicate key. |
| 11 | `create_employee` | No validation of code format, email, times or dates, and `created_at` was local time shown as text. | `shift_start` equal to `shift_end`, or `joined_on: "2026-02-30"`. | Pydantic patterns and validators; `created_at` is stored in UTC and returned in milliseconds. |
| 12 | `list_employees` | Page 1 skipped the first page, `total` ignored the filter, there was no sort, and page values weren't checked. | `?department=Sales` shows the total of everyone. | Correct skip, filtered count, sort by `emp_code`, and page limits. |
| 13 | `list_attendance` | It loaded every record into memory and sorted in Python, with no tie-break, no input checks and no defaults for old records. | Slow and heavy at 100,000 records. | Database sort, skip and limit with an index, validation, and defaults for missing fields. |
| 14 | `/health` | It always said ok, even with MongoDB down. | Stop MongoDB and call `/health`. | It pings MongoDB and returns 503 on failure. |
| 15 | `MongoClient` | PyMongo returned times without a time zone, and they were read as local time. | Times shift by the server's offset. | `tz_aware=True` plus `to_ms` and `from_ms` helpers everywhere. |
| 16 | Whole file | No indexes, and many endpoints were missing. | `explain` shows COLLSCAN. | Indexes are created at startup, and all endpoints are built. |

**Looked at and fine:** `load_dotenv()` (by default it doesn't override real environment variables), the 201 status codes and the 409 on duplicates, comparing dates as `YYYY-MM-DD` strings (they sort correctly), and `doc.pop("_id")` (works, but I serialize explicitly instead).s