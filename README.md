# Employee Attendance & Analytics API

FastAPI + MongoDB (6.0+; uses `$densify`, `$setWindowFields`, `$rank`). All code is in `app/main.py`.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or a local .env
uvicorn app.main:app --port 8000
```
Indexes are created at startup. Docs: `REVIEW.md` (Part A), `DECISIONS.md` (Part C).

Notes: reported rounding is done half-up on integers inside the pipelines; the trend's `moving_avg_7d`
averages the already-rounded 4-decimal rates. Concurrent PATCH losers get 409.
