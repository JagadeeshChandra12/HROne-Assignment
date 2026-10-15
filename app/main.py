"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real env vars win)
"""
import bisect
import math
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

EMP_CODE_RE = r"^EMP\d{4,6}$"
EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"

PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = ("PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY")

EPOCH_MIN = 100_000_000_000
EPOCH_MAX = 4_102_444_800_000

EpochMillis = Annotated[int, Field(strict=True, ge=EPOCH_MIN, le=EPOCH_MAX)]


def create_indexes():
    db.employees.create_index("emp_code", unique=True, name="uniq_emp_code")
    db.employees.create_index("department", name="idx_department")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="uniq_emp_date")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_date_emp")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_emp_punchin")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers (R1)
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Truncate to whole seconds, return a naive UTC datetime (R1)."""
    sec = ms // 1000
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(tzinfo=None)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def now_utc_naive() -> datetime:
    return ms_to_dt(now_ms())


def to_ist(dt_utc: datetime) -> datetime:
    return dt_utc + timedelta(hours=5, minutes=30)


def round_half_up(value: float, decimals: int) -> float:
    quant = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP))


def attendance_date_for_punch_in(punch_in_dt: datetime, shift_start: str, shift_end: str) -> str:
    ist = to_ist(punch_in_dt)
    overnight = shift_end <= shift_start
    if overnight:
        eh, em = map(int, shift_end.split(":"))
        if (ist.hour, ist.minute, ist.second) < (eh, em, 0):
            ist = ist - timedelta(days=1)
    return ist.date().isoformat()


def compute_late_minutes(punch_in_dt: datetime, attendance_date: str, shift_start: str) -> int:
    sh, sm = map(int, shift_start.split(":"))
    shift_start_ist = datetime.combine(date.fromisoformat(attendance_date), time(sh, sm))
    ist = to_ist(punch_in_dt)
    diff_seconds = (ist - shift_start_ist).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def shift_end_ist_instant(attendance_date: str, shift_start: str, shift_end: str) -> datetime:
    eh, em = map(int, shift_end.split(":"))
    end_date = date.fromisoformat(attendance_date)
    if shift_end <= shift_start:
        end_date += timedelta(days=1)
    return datetime.combine(end_date, time(eh, em))


def compute_overtime_minutes(punch_out_dt: datetime, attendance_date: str, shift_start: str, shift_end: str) -> int:
    end_ist = shift_end_ist_instant(attendance_date, shift_start, shift_end)
    ist = to_ist(punch_out_dt)
    diff_minutes = math.floor((ist - end_ist).total_seconds() / 60)
    return diff_minutes if diff_minutes >= 30 else 0


def compute_work_hours(punch_in_dt: datetime, punch_out_dt: datetime) -> float:
    seconds = (punch_out_dt - punch_in_dt).total_seconds()
    return round_half_up(seconds / 3600, 2)


def month_bounds(month: str) -> tuple[str, str]:
    y, m = map(int, month.split("-"))
    start = f"{y:04d}-{m:02d}-01"
    if m == 12:
        end = date(y, 12, 31)
    else:
        end = date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)
    return start, end.isoformat()


def count_working_days(start_str: str, end_str: str) -> int:
    start = date.fromisoformat(start_str)
    end = date.fromisoformat(end_str)
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, rem = divmod(days, 7)
    count = full_weeks * 5
    for i in range(rem):
        d = start + timedelta(days=full_weeks * 7 + i)
        if d.weekday() < 5:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: Annotated[str, Field(pattern=EMP_CODE_RE)]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=EMAIL_RE)]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=TIME_RE)] = "09:30"
    shift_end: Annotated[str, Field(pattern=TIME_RE)] = "18:30"
    joined_on: Annotated[str, Field(pattern=DATE_RE)]

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        try:
            date.fromisoformat(v)
        except ValueError:
            raise ValueError("joined_on must be a real calendar date")
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


# --------------------------------------------------------------------------- #
# Serialization
# --------------------------------------------------------------------------- #
def serialize_employee(doc) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc["created_at"]),
    }


def serialize_history_entry(h) -> dict:
    changes = {}
    for key, pair in (h.get("changes") or {}).items():
        frm, to = pair.get("from"), pair.get("to")
        if key in ("punch_in", "punch_out"):
            frm = dt_to_ms(frm)
            to = dt_to_ms(to)
        changes[key] = {"from": frm, "to": to}
    return {"at": dt_to_ms(h["at"]), "by": h["by"], "reason": h["reason"], "changes": changes}


def serialize_attendance(doc) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day") or False),
        "history": [serialize_history_entry(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared between real endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def build_attendance_filter(emp_code, date_from, date_to, status) -> dict:
    for field_name, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            try:
                date.fromisoformat(value)
            except ValueError:
                raise HTTPException(422, f"{field_name} must be a real calendar date")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")
    filt: dict = {}
    if emp_code:
        filt["emp_code"] = emp_code
    if date_from or date_to:
        rng = {}
        if date_from:
            rng["$gte"] = date_from
        if date_to:
            rng["$lte"] = date_to
        filt["date"] = rng
    if status:
        filt["status"] = status
    return filt


def build_employee_monthly_pipeline(emp_code: str, month: str) -> list:
    start, end = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start, "$lte": end}}},
        {
            "$addFields": {
                "is_weekday": {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 2, 3, 4, 5]]},
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "hd": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$is_weekday"]}, {"$cond": ["$hd", 0.5, 1]}, 0]}},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def build_department_agg_pipeline(emp_codes: list, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": start, "$lte": end}}},
        {
            "$addFields": {
                "is_weekday": {"$in": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [1, 2, 3, 4, 5]]},
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "hd": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {"$sum": {"$cond": [{"$and": ["$is_present", "$is_weekday"]}, {"$cond": ["$hd", 0.5, 1]}, 0]}},
                "work_hours_sum": {"$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, "$work_hours", 0]}},
                "work_hours_count": {"$sum": {"$cond": [{"$and": ["$is_present", {"$ne": ["$work_hours", None]}]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]


def build_leaderboard_pipeline(emp_codes: Optional[list], start: str, end: str) -> list:
    match: dict = {"date": {"$gte": start, "$lte": end}}
    if emp_codes is not None:
        match["emp_code"] = {"$in": emp_codes}
    return [
        {"$match": match},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
    ]


def build_trend_pipeline(emp_codes: list, start: str, end: str) -> list:
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": start, "$lte": end}}},
        {
            "$addFields": {
                "is_present": {"$in": ["$status", list(PRESENCE_STATUSES)]},
                "hd": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": {"$cond": ["$is_present", {"$cond": ["$hd", 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            }
        },
    ]


def explain_find(collection_name: str, filt: dict, sort=None, skip=0, limit=0) -> dict:
    cmd: dict = {"find": collection_name, "filter": filt}
    if sort is not None:
        cmd["sort"] = sort
    if skip:
        cmd["skip"] = skip
    if limit:
        cmd["limit"] = limit
    return db.command({"explain": cmd, "verbosity": "executionStats"})


def explain_aggregate(collection_name: str, pipeline: list) -> dict:
    cmd = {"aggregate": collection_name, "pipeline": pipeline, "cursor": {}}
    return db.command({"explain": cmd, "verbosity": "executionStats"})


# --------------------------------------------------------------------------- #
# System
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = now_utc_naive()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filt: dict = {}
    if department:
        filt["department"] = department
    total = db.employees.count_documents(filt)
    skip = (page - 1) * page_size
    cursor = db.employees.find(filt).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [serialize_employee(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    ms = body.punched_at if body.punched_at is not None else now_ms()
    punch_in_dt = ms_to_dt(ms)
    attendance_date = attendance_date_for_punch_in(punch_in_dt, emp["shift_start"], emp["shift_end"])
    late_minutes = compute_late_minutes(punch_in_dt, attendance_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_dt,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_minutes,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return serialize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    ms = body.punched_at if body.punched_at is not None else now_ms()
    punch_out_dt = ms_to_dt(ms)

    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "no open punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_dt = record["punch_in"]
    delta = punch_out_dt - punch_in_dt
    if delta <= timedelta(0) or delta > timedelta(hours=24):
        raise HTTPException(422, "punched_at must be after punch_in and within 24 hours")

    work_hours = compute_work_hours(punch_in_dt, punch_out_dt)
    overtime_minutes = compute_overtime_minutes(punch_out_dt, record["date"], emp["shift_start"], emp["shift_end"])
    half_day = work_hours < 4.50

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": punch_out_dt, "work_hours": work_hours, "overtime_minutes": overtime_minutes, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if not updated:
        raise HTTPException(409, "already punched out")
    return serialize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    filt = build_attendance_filter(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(filt)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(filt).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [serialize_attendance(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: Annotated[str, Path(pattern=DATE_RE)],
    body: RegularizeRequest,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, "no attendance record for that date")

    new_status = body.status if body.status is not None else record["status"]
    clearing = new_status in ("ABSENT", "LEAVE")

    if clearing and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "cannot set punch_in/punch_out when status is ABSENT or LEAVE")

    if clearing:
        new_punch_in = None
        new_punch_out = None
    else:
        new_punch_in = ms_to_dt(body.punch_in) if body.punch_in is not None else record.get("punch_in")
        new_punch_out = ms_to_dt(body.punch_out) if body.punch_out is not None else record.get("punch_out")
        if new_punch_in is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        new_date = attendance_date_for_punch_in(new_punch_in, emp["shift_start"], emp["shift_end"])
        if new_date != date:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if new_punch_out is not None:
            delta = new_punch_out - new_punch_in
            if delta <= timedelta(0) or delta > timedelta(hours=24):
                raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if clearing:
        new_late, new_overtime, new_work_hours, new_half_day = 0, 0, None, False
    else:
        new_late = compute_late_minutes(new_punch_in, date, emp["shift_start"])
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime = compute_overtime_minutes(new_punch_out, date, emp["shift_start"], emp["shift_end"])
            new_half_day = new_work_hours < 4.50
        else:
            new_work_hours, new_overtime, new_half_day = None, 0, False

    old_values = {
        "status": record["status"],
        "punch_in": record.get("punch_in"),
        "punch_out": record.get("punch_out"),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day") or False),
    }
    new_values = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    changes = {k: {"from": old_values[k], "to": new_values[k]} for k in old_values if old_values[k] != new_values[k]}
    if not changes:
        raise HTTPException(422, "request does not change anything")

    history_entry = {"at": now_utc_naive(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    result = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {"$set": new_values, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if not result:
        raise HTTPException(409, "record was modified concurrently, retry")
    return serialize_attendance(result)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "unknown employee")

    month_start, month_end = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(build_employee_monthly_pipeline(emp_code, month)))
    agg = rows[0] if rows else {}

    working_days = count_working_days(max(emp["joined_on"], month_start), month_end)
    present_days = agg.get("present_days", 0.0)
    attendance_pct = round_half_up(present_days / working_days * 100, 4) if working_days > 0 else None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def default_dept_stats() -> dict:
    return {
        "present_days": 0.0,
        "work_hours_sum": 0.0,
        "work_hours_count": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    }


def resolve_department_summary_inputs(month: str, department: Optional[str]):
    start, end = month_bounds(month)
    emp_filter: dict = {"joined_on": {"$lte": end}}
    if department:
        emp_filter["department"] = department
    headcount_map: dict = {}
    for row in db.employees.aggregate([{"$match": emp_filter}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}]):
        headcount_map[row["_id"]] = row["headcount"]

    code_filter = {"department": department} if department else {}
    dept_by_code = {e["emp_code"]: e["department"] for e in db.employees.find(code_filter, {"emp_code": 1, "department": 1})}
    emp_codes = list(dept_by_code)
    return start, end, headcount_map, dept_by_code, emp_codes


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    start, end, headcount_map, dept_by_code, emp_codes = resolve_department_summary_inputs(month, department)
    if not headcount_map:
        return {"month": month, "items": []}

    stats = {dept: default_dept_stats() for dept in headcount_map}
    for row in db.attendance_logs.aggregate(build_department_agg_pipeline(emp_codes, start, end)):
        dept = dept_by_code.get(row["_id"])
        if dept is None or dept not in stats:
            continue
        s = stats[dept]
        s["present_days"] += row["present_days"]
        s["work_hours_sum"] += row["work_hours_sum"]
        s["work_hours_count"] += row["work_hours_count"]
        s["late_count"] += row["late_count"]
        s["total_late_minutes"] += row["total_late_minutes"]
        s["leave_count"] += row["leave_count"]
        s["on_duty_count"] += row["on_duty_count"]

    items = []
    for dept in sorted(headcount_map):
        s = stats[dept]
        avg = round_half_up(s["work_hours_sum"] / s["work_hours_count"], 2) if s["work_hours_count"] > 0 else None
        items.append(
            {
                "department": dept,
                "headcount": headcount_map[dept],
                "present_days": s["present_days"],
                "avg_work_hours": avg,
                "late_count": s["late_count"],
                "total_late_minutes": s["total_late_minutes"],
                "leave_count": s["leave_count"],
                "on_duty_count": s["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, end = month_bounds(month)
    emp_codes = None
    if department:
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        if not emp_codes:
            return {"month": month, "items": []}

    rows = list(db.attendance_logs.aggregate(build_leaderboard_pipeline(emp_codes, start, end)))
    if not rows:
        return {"month": month, "items": []}

    infos = {
        e["emp_code"]: e
        for e in db.employees.find({"emp_code": {"$in": [r["_id"] for r in rows]}}, {"emp_code": 1, "name": 1, "department": 1})
    }
    enriched = []
    for r in rows:
        info = infos.get(r["_id"])
        if not info:
            continue
        enriched.append(
            {
                "emp_code": r["_id"],
                "name": info["name"],
                "department": info["department"],
                "total_late_minutes": r["total_late_minutes"],
                "late_count": r["late_count"],
            }
        )
    enriched.sort(key=lambda x: (-x["total_late_minutes"], x["emp_code"]))

    items = []
    rank = 0
    prev_minutes = None
    for position, row in enumerate(enriched, start=1):
        if row["total_late_minutes"] != prev_minutes:
            rank = position
            prev_minutes = row["total_late_minutes"]
        if rank > limit:
            break
        items.append({**row, "rank": rank})
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    try:
        from_date = date.fromisoformat(from_)
        to_date = date.fromisoformat(to)
    except ValueError:
        raise HTTPException(422, "from/to must be real calendar dates")

    if to_date < from_date:
        raise HTTPException(422, "to must not be before from")
    total_days = (to_date - from_date).days + 1
    if total_days > 92:
        raise HTTPException(422, "range exceeds 92 days")

    employees = list(db.employees.find({"department": department}, {"emp_code": 1, "joined_on": 1}))
    if not employees:
        raise HTTPException(404, "unknown department")

    emp_codes = [e["emp_code"] for e in employees]
    joined_dates = sorted(e["joined_on"] for e in employees)

    daily = {row["_id"]: row for row in db.attendance_logs.aggregate(build_trend_pipeline(emp_codes, from_, to))}

    items = []
    rates: list = []
    for i in range(total_days):
        d = from_date + timedelta(days=i)
        d_str = d.isoformat()
        is_working = d.weekday() < 5
        headcount = bisect.bisect_right(joined_dates, d_str)
        row = daily.get(d_str, {})
        present_count = row.get("present_count", 0.0)
        late_count = row.get("late_count", 0)

        rate = round_half_up(present_count / headcount, 4) if (is_working and headcount > 0) else None
        rates.append(rate)

        window = [r for r in rates[max(0, i - 6) : i + 1] if r is not None]
        moving_avg = round_half_up(sum(window) / len(window), 4) if window else None

        items.append(
            {
                "date": d_str,
                "is_working_day": is_working,
                "headcount": headcount,
                "present_count": present_count,
                "late_count": late_count,
                "attendance_rate": rate,
                "moving_avg_7d": moving_avg,
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        filt = build_attendance_filter(emp_code, date_from, date_to, status)
        explain = explain_find(
            "attendance_logs", filt, sort={"date": -1, "emp_code": 1}, skip=(page - 1) * page_size, limit=page_size
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline)}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        start, end, _, _, emp_codes = resolve_department_summary_inputs(month, department)
        pipeline = build_department_agg_pipeline(emp_codes, start, end)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline)}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        start, end = month_bounds(month)
        emp_codes = None
        if department:
            emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        pipeline = build_leaderboard_pipeline(emp_codes, start, end)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline)}

    if endpoint == "department_trend":
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from and to are required")
        emp_codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"emp_code": 1})]
        pipeline = build_trend_pipeline(emp_codes, from_, to)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", pipeline)}

    raise HTTPException(422, "unknown endpoint")
