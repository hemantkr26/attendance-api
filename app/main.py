"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (a local .env is loaded for convenience; real environment variables win)

Everything lives in this one file, in this order:
  1. configuration and indexes      4. rule helpers (R1 - R5)        7. attendance endpoints
  2. API <-> MongoDB conversions    5. request models                8. analytics endpoints
  3. validation helpers             6. employee endpoints            9. admin / explain
"""
import calendar
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Any, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # override=False (default): real environment variables take priority over .env
log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back aware UTC datetimes instead of naive ones, so nothing is
# ever silently interpreted in the server's local time zone.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MIN_MS, MAX_MS = 100_000_000_000, 4_102_444_800_000

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]                       # R6
ALL_STATUS = ["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]

YMD = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
YM = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"

# Index names are used as hints so the plan (and the explain endpoint) is deterministic.
IX_EMP_UNIQUE = "emp_code_uq"
IX_EMP_DEPT_CODE = "dept_emp"
IX_EMP_DEPT_JOINED = "dept_joined"
IX_EMP_JOINED = "joined"
IX_LOG_EMP_DATE = "emp_date_uq"
IX_LOG_DATE_EMP = "date_emp"
IX_LOG_STATUS = "status_date_emp"
IX_LOG_EMP_PUNCH = "emp_punchin"


def ensure_indexes() -> None:
    """Idempotent: creating an index that already exists with the same spec is a no-op."""
    wanted = [
        (db.employees, [("emp_code", ASCENDING)], dict(unique=True, name=IX_EMP_UNIQUE)),
        (db.employees, [("department", ASCENDING), ("emp_code", ASCENDING)], dict(name=IX_EMP_DEPT_CODE)),
        (db.employees, [("department", ASCENDING), ("joined_on", ASCENDING)], dict(name=IX_EMP_DEPT_JOINED)),
        (db.employees, [("joined_on", ASCENDING)], dict(name=IX_EMP_JOINED)),
        # one record per (employee, day): the guarantee behind race-safe punch-in; also serves
        # per-employee lists/ranges, the monthly summary and the trend ($in on emp_code)
        (db.attendance_logs, [("emp_code", ASCENDING), ("date", DESCENDING)], dict(unique=True, name=IX_LOG_EMP_DATE)),
        # date range (+ the default sort of GET /attendance), department summary, leaderboard
        (db.attendance_logs, [("date", DESCENDING), ("emp_code", ASCENDING)], dict(name=IX_LOG_DATE_EMP)),
        # status filter with the same sort
        (db.attendance_logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], dict(name=IX_LOG_STATUS)),
        # punch-out: "latest record of this employee with punch_in <= t"
        (db.attendance_logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], dict(name=IX_LOG_EMP_PUNCH)),
    ]
    for coll, keys, opts in wanted:
        try:
            coll.create_index(keys, **opts)
        except PyMongoError as exc:  # keep the app up; /health still reports DB problems
            log.error("could not create index %s: %s", opts.get("name"), exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_, exc: PyMongoError):
    log.error("mongodb error: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Conversions between the API (epoch ms) and MongoDB (BSON datetimes)
# --------------------------------------------------------------------------- #
def from_ms(ms: int) -> datetime:
    """Epoch ms -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(milliseconds=ms - ms % 1000)


def ensure_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return (ensure_utc(dt) - EPOCH) // timedelta(milliseconds=1)


def now_instant() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def resolve_instant(punched_at: Optional[int]) -> datetime:
    return now_instant() if punched_at is None else from_ms(punched_at)


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"], "name": doc["name"], "email": doc["email"],
        "department": doc["department"], "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"], "joined_on": doc["joined_on"],
        "created_at": to_ms(doc.get("created_at")),
    }


def _history_value(key: str, value: Any) -> Any:
    return to_ms(value) if key in ("punch_in", "punch_out") and isinstance(value, datetime) else value


def serialize_record(doc: dict) -> dict:
    """Stored attendance document -> API record. Fills the defaults legacy records omit."""
    history = []
    for h in doc.get("history") or []:
        history.append({
            "at": to_ms(h["at"]), "by": h["by"], "reason": h["reason"],
            "changes": {
                k: {"from": _history_value(k, v.get("from")), "to": _history_value(k, v.get("to"))}
                for k, v in (h.get("changes") or {}).items()
            },
        })
    return {
        "emp_code": doc["emp_code"], "date": doc["date"], "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")), "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# 3. Validation helpers
# --------------------------------------------------------------------------- #
def invalid(where: str, name: str, msg: str, value: Any = None) -> RequestValidationError:
    """A 422 in FastAPI's standard shape, for checks the type system cannot express."""
    return RequestValidationError([{"type": "value_error", "loc": (where, name), "msg": msg, "input": value}])


def parse_ymd(value: str, name: str, where: str = "query") -> date:
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise invalid(where, name, "must be a valid calendar date YYYY-MM-DD", value)


def month_bounds(month: str) -> tuple[str, str, date, date]:
    try:
        year, mon = int(month[:4]), int(month[5:7])
        first = date(year, mon, 1)
    except ValueError:
        raise invalid("query", "month", "must be a valid month YYYY-MM", month)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return first.isoformat(), last.isoformat(), first, last


def employee_or_404(emp_code: str) -> dict:
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 4. Business-rule helpers (R1 - R5). All instants are aware datetimes truncated to seconds.
# --------------------------------------------------------------------------- #
def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # "HH:MM" strings compare correctly


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: IST calendar date of the punch-in; an overnight-shift punch before shift_end
    belongs to the shift that began the previous day."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end) and local.time() < _hm(shift_end):
        d -= timedelta(days=1)
    return d


def compute_late_minutes(punch_in: datetime, att_date: date, shift_start: str) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor(minutes since start)."""
    start = datetime.combine(att_date, _hm(shift_start), tzinfo=IST)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def shift_end_instant(att_date: date, shift_start: str, shift_end: str) -> datetime:
    end = datetime.combine(att_date, _hm(shift_end), tzinfo=IST)
    return end + timedelta(days=1) if is_overnight(shift_start, shift_end) else end


def compute_overtime(punch_out: datetime, end: datetime) -> int:
    """R3: floor(minutes after shift end), only when >= 30."""
    minutes = int((punch_out - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up (Decimal, so no float or banker's rounding)."""
    seconds = int((punch_out - punch_in).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(emp: dict, att_date: date, status: str, punch_in: Optional[datetime],
           punch_out: Optional[datetime]) -> dict:
    """All derived fields for a record, per the table in DATA_MODEL.md."""
    zero = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return zero
    zero["late_minutes"] = compute_late_minutes(punch_in, att_date, emp["shift_start"])
    if punch_out is None:
        return zero
    wh = compute_work_hours(punch_in, punch_out)
    end = shift_end_instant(att_date, emp["shift_start"], emp["shift_end"])
    return {
        "work_hours": wh, "late_minutes": zero["late_minutes"],
        "overtime_minutes": compute_overtime(punch_out, end), "half_day": wh < 4.5,  # R5, rounded value
    }


def check_punch_pair(punch_in: datetime, punch_out: datetime) -> None:
    if punch_out <= punch_in:
        raise HTTPException(422, "punch_out must be after punch_in")
    if punch_out - punch_in > timedelta(hours=24):
        raise HTTPException(422, "punch_out must be within 24 hours of punch_in")


# --------------------------------------------------------------------------- #
# 5. Request models
# --------------------------------------------------------------------------- #
EpochMs = Annotated[int, Field(strict=True, ge=MIN_MS, le=MAX_MS)]  # strict: no floats / strings / bools


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=HHMM)
    shift_end: str = Field("18:30", pattern=HHMM)
    joined_on: str = Field(pattern=YMD)

    @field_validator("joined_on")
    @classmethod
    def _real_date(cls, v: str) -> str:
        date.fromisoformat(v)  # rejects 2026-02-30 etc.
        return v

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _no_explicit_nulls(self):
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null; omit it to leave it unchanged")
        return self
    # work_hours / late_minutes / ... are not declared: pydantic ignores them if a client sends them


Page = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Month = Annotated[str, Query(pattern=YM)]


# --------------------------------------------------------------------------- #
# 6. System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "mongodb unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_instant()
    try:  # uniqueness is enforced by the unique index, not by a check-then-insert
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q: dict = {"department": department} if department is not None else {}
    total = db.employees.count_documents(q)
    items = (db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
             .skip((page - 1) * page_size).limit(page_size))
    return {"items": [serialize_employee(d) for d in items], "total": total,
            "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 7. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee_or_404(body.emp_code)
    ts = resolve_instant(body.punched_at)
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code, "date": att_date.isoformat(), "status": body.status,
        "punch_in": ts, "punch_out": None,
        **derive(emp, att_date, body.status, ts, None),
        "history": [],
    }
    try:  # the unique (emp_code, date) index decides the race: exactly one insert wins
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_record(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee_or_404(body.emp_code)
    ts = resolve_instant(body.punched_at)
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)], hint=IX_LOG_EMP_PUNCH,
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    pin = ensure_utc(rec["punch_in"])
    check_punch_pair(pin, ts)
    fields = derive(emp, date.fromisoformat(rec["date"]), rec["status"], pin, ts)
    # Compare-and-set: only the request that still sees punch_out == null can win.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None, "punch_in": rec["punch_in"], "status": rec["status"]},
        {"$set": {"punch_out": ts, **fields}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return serialize_record(updated)


def build_attendance_list(emp_code, date_from, date_to, status, page, page_size) -> dict:
    lo = parse_ymd(date_from, "date_from") if date_from else None
    hi = parse_ymd(date_to, "date_to") if date_to else None
    if lo and hi and lo > hi:
        raise invalid("query", "date_from", "date_from must not be after date_to", date_from)
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if lo or hi:
        q["date"] = {}
        if lo:
            q["date"]["$gte"] = lo.isoformat()
        if hi:
            q["date"]["$lte"] = hi.isoformat()
    if status:
        q["status"] = status
    hint = IX_LOG_EMP_DATE if emp_code else IX_LOG_STATUS if status else IX_LOG_DATE_EMP
    return {"kind": "find", "collection": "attendance_logs", "filter": q,
            "sort": {"date": -1, "emp_code": 1}, "skip": (page - 1) * page_size,
            "limit": page_size, "hint": hint}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Annotated[Optional[str], Query(pattern=YMD)] = None,
    date_to: Annotated[Optional[str], Query(pattern=YMD)] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    cursor = (db.attendance_logs.find(spec["filter"], {"_id": 0})
              .sort(list(spec["sort"].items())).skip(spec["skip"]).limit(spec["limit"]).hint(spec["hint"]))
    total = db.attendance_logs.count_documents(spec["filter"])
    return {"items": [serialize_record(d) for d in cursor], "total": total,
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: Annotated[str, Field(pattern=YMD)], body: RegularizeIn):  # noqa: A002
    rec_date = parse_ymd(date, "date", "path")
    emp = employee_or_404(emp_code)
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": rec_date.isoformat()})
    if rec is None:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status or rec["status"]
    old_in = ensure_utc(rec["punch_in"]) if rec.get("punch_in") else None
    old_out = ensure_utc(rec["punch_out"]) if rec.get("punch_out") else None
    new_in = from_ms(body.punch_in) if body.punch_in is not None else old_in
    new_out = from_ms(body.punch_out) if body.punch_out is not None else old_out

    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, f"status {new_status} cannot carry punch times")
        new_in = new_out = None  # these statuses clear both punch times
    else:
        if new_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if body.punch_in is not None and attendance_date(
                new_in, emp["shift_start"], emp["shift_end"]) != rec_date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_out is not None:
            check_punch_pair(new_in, new_out)

    old = {
        "status": rec["status"], "punch_in": old_in, "punch_out": old_out,
        "work_hours": rec.get("work_hours"), "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0, "half_day": bool(rec.get("half_day", False)),
    }
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out,
           **derive(emp, rec_date, new_status, new_in, new_out)}

    def same(a, b) -> bool:
        return (to_ms(a) == to_ms(b)) if isinstance(a, datetime) or isinstance(b, datetime) else a == b

    changes = {k: {"from": old[k], "to": new[k]} for k in old if not same(old[k], new[k])}
    if not any(k in changes for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "the request changes nothing")

    entry = {"at": datetime.now(UTC), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    history = rec.get("history")
    # Optimistic concurrency: the write only applies if the record is exactly as we read it
    # (same fields, same history length). A concurrent correction makes the loser get a 409
    # instead of overwriting - and so losing - the other one's history entry.
    guard = {
        "_id": rec["_id"], "status": rec["status"],
        "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out"),
        "history": {"$size": len(history)} if isinstance(history, list) else None,
    }
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": {**new, "history": (history if isinstance(history, list) else []) + [entry]}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; retry")
    return serialize_record(updated)


# --------------------------------------------------------------------------- #
# 8. Analytics (all aggregation pipelines)
# --------------------------------------------------------------------------- #
def half_up_div(num: Any, den: Any) -> dict:
    """Pipeline expression: round-half-up(num / den) to a whole number, for non-negative integers.
    $round rounds half-to-even and doubles can't represent x.xx5, so the rounding is done on
    integers (hundredths / ten-thousandths): floor((2*num + den) / (2*den))."""
    return {"$floor": {"$divide": [{"$add": [{"$multiply": [2, num]}, den]}, {"$multiply": [2, den]}]}}


_DATE_OBJ = {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
_IS_WEEKDAY = {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}  # $dayOfWeek: Sunday=1 ... Saturday=7
_IS_HALF = {"$eq": [{"$ifNull": ["$half_day", False]}, True]}
_LATE = {"$ifNull": ["$late_minutes", 0]}
_HAS_HOURS = {"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}


def _day_of_week_stage() -> dict:
    return {"$addFields": {"dow": {"$dayOfWeek": _DATE_OBJ}}}


def _stats_group(group_id: Any) -> dict:
    """Counters computed from the STORED derived values (never recomputed from punch times).
    p2 = present days x 2 (a half day = 1, a full day = 2, Mon-Fri only) so everything stays an integer;
    ws = sum of work_hours in hundredths over present-status records that have work_hours."""
    return {
        "_id": group_id,
        "p2": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, {"$cond": [_IS_HALF, 1, 2]}, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        "late_min": {"$sum": _LATE},
        "ot_min": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "wn": {"$sum": {"$cond": [_HAS_HOURS, 1, 0]}},
        "ws": {"$sum": {"$cond": [_HAS_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}},
    }


def working_days(first: date, last: date, joined_on: date) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last, inclusive. At most 31 calendar days."""
    d, n = max(first, joined_on), 0
    while d <= last:
        n += d.weekday() < 5
        d += timedelta(days=1)
    return n


def build_employee_monthly(emp_code: str, month: str) -> dict:
    start, end, _, _ = month_bounds(month)
    return {"kind": "aggregate", "collection": "attendance_logs", "hint": IX_LOG_EMP_DATE, "pipeline": [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        _day_of_week_stage(),
        {"$group": _stats_group(None)},
    ]}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: Month):
    emp = employee_or_404(emp_code)
    _, _, first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(
        (s := build_employee_monthly(emp_code, month))["pipeline"], hint=s["hint"]))
    r = rows[0] if rows else {}
    wd = working_days(first, last, date.fromisoformat(emp["joined_on"]))
    p2 = r.get("p2", 0)
    pct = None
    if wd:  # p2/2 / wd * 100 = p2 * 50 / wd ; exact Decimal arithmetic, half-up (R8)
        pct = float((Decimal(p2) * 50 / Decimal(wd)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
    return {
        "emp_code": emp_code, "month": month, "working_days": wd, "present_days": p2 / 2,
        "leave_days": r.get("leave", 0), "late_count": r.get("late_count", 0),
        "total_late_minutes": r.get("late_min", 0), "total_overtime_minutes": r.get("ot_min", 0),
        "attendance_pct": pct,
    }


def build_department_summary(month: str, department: Optional[str]) -> dict:
    start, end, _, _ = month_bounds(month)
    emp_match: dict = {"joined_on": {"$lte": end}}  # R9: joined by the end of the month, logs or not
    if department is not None:
        emp_match["department"] = department
    return {"kind": "aggregate", "collection": "employees",
            "hint": IX_EMP_DEPT_JOINED if department is not None else IX_EMP_JOINED, "pipeline": [
        {"$match": emp_match},
        # Per employee: the month's stats from attendance_logs (uses the unique emp_code/date index).
        # Employees without logs get an empty array, so they still count towards headcount.
        {"$lookup": {
            "from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code", "as": "a",
            "pipeline": [
                {"$match": {"date": {"$gte": start, "$lte": end}}},
                _day_of_week_stage(),
                {"$group": _stats_group(None)},
            ],
        }},
        {"$addFields": {"s": {"$ifNull": [{"$arrayElemAt": ["$a", 0]}, {}]}}},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1},
            "p2": {"$sum": "$s.p2"}, "late_count": {"$sum": "$s.late_count"}, "late_min": {"$sum": "$s.late_min"},
            "leave": {"$sum": "$s.leave"}, "on_duty": {"$sum": "$s.on_duty"},
            "wn": {"$sum": "$s.wn"}, "ws": {"$sum": "$s.ws"},
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1,
            "present_days": {"$divide": ["$p2", 2]},
            # mean over RECORDS (sum of hundredths / record count), half-up to 2 decimals
            "avg_work_hours": {"$cond": [{"$gt": ["$wn", 0]}, {"$divide": [half_up_div("$ws", "$wn"), 100]}, None]},
            "late_count": 1, "total_late_minutes": "$late_min",
            "leave_count": "$leave", "on_duty_count": "$on_duty",
        }},
    ]}


@app.get("/analytics/departments/summary")
def department_summary(month: Month, department: Optional[str] = None):
    spec = build_department_summary(month, department)
    items = list(db.employees.aggregate(spec["pipeline"], hint=spec["hint"]))
    return {"month": month, "items": items}


def build_late_leaderboard(month: str, limit: int, department: Optional[str]) -> dict:
    start, end, _, _ = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$emp_code", "total": {"$sum": _LATE},
                    "cnt": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}}}},
        {"$match": {"total": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs of unknown employees are dropped here
    ]
    if department is not None:
        pipeline.append({"$match": {"e.department": department}})  # rank inside the department only
    pipeline += [
        # $rank = standard competition ranking: ties share a rank, the next one is skipped (1, 2, 2, 4)
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit is applied AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name",
                      "department": "$e.department", "total_late_minutes": "$total", "late_count": "$cnt"}},
    ]
    return {"kind": "aggregate", "collection": "attendance_logs", "hint": IX_LOG_DATE_EMP, "pipeline": pipeline}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: Month, limit: Annotated[int, Query(ge=1, le=50)] = 10,
                     department: Optional[str] = None):
    spec = build_late_leaderboard(month, limit, department)
    return {"month": month, "items": list(db.attendance_logs.aggregate(spec["pipeline"], hint=spec["hint"]))}


def build_department_trend(department: str, frm: str, to: str) -> dict:
    d_from, d_to = parse_ymd(frm, "from"), parse_ymd(to, "to")
    if d_to < d_from:
        raise invalid("query", "to", "to must not be before from", to)
    if (d_to - d_from).days + 1 > 92:
        raise invalid("query", "to", "the range must not exceed 92 days", to)
    people = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not people:
        raise HTTPException(404, "department not found")
    codes = [p["emp_code"] for p in people]
    joined = sorted(p["joined_on"] for p in people)
    lo = datetime.combine(d_from, time(), tzinfo=UTC)
    hi = datetime.combine(d_to + timedelta(days=1), time(), tzinfo=UTC)  # $densify upper bound is exclusive
    is_working = {"$and": [{"$gte": ["$dow", 2]}, {"$lte": ["$dow", 6]}]}
    pipeline = [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": frm, "$lte": to}}},
        {"$group": {  # one row per day that has records; p2 = presence count x 2 (half day = 1)
            "_id": "$date",
            "p2": {"$sum": {"$cond": [_IS_PRESENT, {"$cond": [_IS_HALF, 1, 2]}, 0]}},
            "late": {"$sum": {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}},
        }},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # gap filling: one row for every calendar day in [from, to], weekends and empty days included
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},
        {"$addFields": {
            "p2": {"$ifNull": ["$p2", 0]}, "late": {"$ifNull": ["$late", 0]},
            "ds": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}}, "dow": {"$dayOfWeek": "$d"},
        }},
        {"$addFields": {
            "working": is_working,
            "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j",
                                                "cond": {"$lte": ["$$j", "$ds"]}}}},
        }},
        # attendance_rate in ten-thousandths (integer, half-up): (p2/2)/headcount*10000 = p2*5000/headcount
        {"$addFields": {"rate": {"$cond": [{"$and": ["$working", {"$gt": ["$headcount", 0]}]},
                                           half_up_div({"$multiply": ["$p2", 5000]}, "$headcount"), None]}}},
        {"$addFields": {"has_rate": {"$cond": [{"$eq": ["$rate", None]}, 0, 1]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "w_sum": {"$sum": "$rate", "window": {"documents": [-6, "current"]}},
            "w_cnt": {"$sum": "$has_rate", "window": {"documents": [-6, "current"]}},
        }}},
        {"$sort": {"d": 1}},
        {"$project": {
            "_id": 0, "date": "$ds", "is_working_day": "$working", "headcount": 1,
            "present_count": {"$divide": ["$p2", 2]}, "late_count": "$late",
            "attendance_rate": {"$cond": [{"$eq": ["$rate", None]}, None, {"$divide": ["$rate", 10000]}]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$w_cnt", 0]},
                                        {"$divide": [half_up_div("$w_sum", "$w_cnt"), 10000]}, None]},
        }},
    ]
    return {"kind": "aggregate", "collection": "attendance_logs", "hint": IX_LOG_EMP_DATE, "pipeline": pipeline}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str,
                     from_: Annotated[str, Query(alias="from", pattern=YMD)],
                     to: Annotated[str, Query(pattern=YMD)]):
    spec = build_department_trend(department, from_, to)
    return {"department": department,
            "items": list(db.attendance_logs.aggregate(spec["pipeline"], hint=spec["hint"]))}


# --------------------------------------------------------------------------- #
# 9. Admin: explain
# --------------------------------------------------------------------------- #
def _require(**params: Any) -> None:
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise RequestValidationError([
            {"type": "missing", "loc": ("query", k), "msg": "Field required", "input": None} for k in missing])


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=YM)] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Annotated[Optional[str], Query(pattern=YMD)] = None,
    date_to: Annotated[Optional[str], Query(pattern=YMD)] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from", pattern=YMD)] = None,
    to: Annotated[Optional[str], Query(pattern=YMD)] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    """Runs explain('executionStats') on exactly the query/pipeline the real endpoint builds."""
    if endpoint == "attendance_list":
        spec = build_attendance_list(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "employee_monthly":
        _require(emp_code=emp_code, month=month)
        spec = build_employee_monthly(emp_code, month)
    elif endpoint == "department_summary":
        _require(month=month)
        spec = build_department_summary(month, department)
    elif endpoint == "late_leaderboard":
        _require(month=month)
        spec = build_late_leaderboard(month, limit, department)
    else:
        _require(department=department, **{"from": from_, "to": to})
        spec = build_department_trend(department, from_, to)

    if spec["kind"] == "find":
        command = {"find": spec["collection"], "filter": spec["filter"], "sort": spec["sort"],
                   "skip": spec["skip"], "limit": spec["limit"], "hint": spec["hint"]}
    else:
        command = {"aggregate": spec["collection"], "pipeline": spec["pipeline"], "cursor": {},
                   "hint": spec["hint"]}
    result = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": spec["collection"],
            "explain": json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))}
