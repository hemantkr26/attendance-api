# Employee Attendance & Analytics API

FastAPI + MongoDB. All code is in `app/main.py`. Needs Python 3.11+ and MongoDB 6.0+
(it uses `$densify`, `$setWindowFields` and `$rank`).

## Run

    python -m venv .venv
    .venv\Scripts\activate          (Mac/Linux: source .venv/bin/activate)
    pip install -r requirements.txt
    set MONGO_URI=mongodb://localhost:27017
    set MONGO_DB=attendance_db      (Mac/Linux: use export instead of set)
    uvicorn app.main:app --port 8000

Indexes are created automatically at startup. Interactive docs: http://localhost:8000/docs

## Files

- `REVIEW.md`: defects found in the starter code (Part A)
- `DECISIONS.md`: design answers (Part C)

## Notes

- Reported numbers are rounded half-up using integer math inside the pipelines, because
  MongoDB's `$round` rounds half to even.
- `moving_avg_7d` in the trend averages the already-rounded 4-decimal attendance rates.
- If two corrections (PATCH) hit the same record at once, the second gets 409.

## Testing

Checked by hand on a local MongoDB with the sample data and extra cases: all analytics
endpoints, ties on the leaderboard, an employee with no logs, a weekend record, an
overnight shift, parallel punch-in / punch-out / create (exactly one success each), and
`/admin/explain` for all five endpoints (index scans, no collection scans).

Not tested: the full ~100,000-record dataset, and simultaneous PATCH requests.