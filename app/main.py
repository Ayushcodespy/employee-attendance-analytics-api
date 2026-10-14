"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from bson import json_util
import json
from datetime import datetime, timedelta, timezone, time, date
from bson.decimal128 import Decimal128
from typing import Optional, Literal
from decimal import Decimal, ROUND_HALF_UP

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from contextlib import asynccontextmanager

load_dotenv()

mongo_uri = os.getenv("MONGO_URI")

if not mongo_uri:
    raise RuntimeError("MONGO_URI is not configured")

client = MongoClient(
    mongo_uri,
    serverSelectionTimeoutMS=3000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
# db = client[os.getenv("MONGO_DB", "attendance_db")]

print("Mongo hosts:", client.nodes)

IST = timezone(timedelta(hours=5, minutes=30))

# app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.employees.create_index("emp_code", unique=True)

    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True
    )

    # Index for attendance list sorting
    db.attendance_logs.create_index([
        ("date", -1),
        ("emp_code", 1),
    ])

    db.attendance_logs.create_index([
        ("date", 1),
        ("emp_code", 1),
    ])
    
    db.employees.create_index([
        ("joined_on", 1),
        ("department", 1),
    ])

    yield



app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)



# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def compute_late_minutes(punch_in_ist: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))

    start = punch_in_ist.replace(
        hour=h,
        minute=m,
        second=0,
        microsecond=0,
    )

    minutes_after_start = int(
        (punch_in_ist - start).total_seconds() // 60
    )

    return minutes_after_start if minutes_after_start > 10 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    hours = Decimal(str(seconds)) / Decimal("3600")

    return float(
        hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def compute_half_day(work_hours: Optional[float]) -> bool:
    if work_hours is None:
        return False

    return work_hours < 4.50


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    overnight: bool = False,
) -> int:
    # Stored punch_out is UTC-naive; convert to IST.
    punch_out_ist = (
        punch_out.replace(tzinfo=timezone.utc)
        .astimezone(IST)
        .replace(tzinfo=None)
    )

    h, m = map(int, shift_end.split(":"))
    shift_end_dt = datetime.fromisoformat(date_str).replace(
        hour=h, minute=m, second=0, microsecond=0
    )

    if overnight:
        shift_end_dt += timedelta(days=1)

    minutes_after_end = int(
        (punch_out_ist - shift_end_dt).total_seconds() // 60
    )

    # Count overtime only when at least 30 full minutes have elapsed.
    return minutes_after_end if minutes_after_end >= 30 else 0


def build_department_trend_pipeline(
    department: str,
    from_date: str,
    to_date: str,
):
    start_date = date.fromisoformat(from_date)
    end_date = date.fromisoformat(to_date)

    start_dt = datetime.combine(start_date, time.min)
    end_exclusive = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
    )

    present_statuses = ["PRESENT", "WFH", "ON_DUTY"]

    # Decimal arithmetic for ROUND_HALF_UP to 4 decimal places.
    scale = Decimal128("10000")
    half = Decimal128("0.5")

    def round_half_up_4(expression):
        scaled = {
            "$multiply": [
                {"$toDecimal": expression},
                scale,
            ]
        }
        rounded_integer = {
            "$floor": {
                "$add": [scaled, half]
            }
        }
        return {
            "$divide": [rounded_integer, scale]
        }

    return [
        # Start from one employee in the requested department.
        {"$match": {"department": department}},
        {"$limit": 1},
        {
            "$project": {
                "_id": 0,
                "calendar_date": {"$literal": start_dt},
            }
        },
        {
            "$densify": {
                "field": "calendar_date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_dt, end_exclusive],
                },
            }
        },

        # Calculate daily department statistics.
        {
            "$lookup": {
                "from": "employees",
                "let": {
                    "day": {
                        "$dateToString": {
                            "format": "%Y-%m-%d",
                            "date": "$calendar_date",
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "department": department,
                            "$expr": {
                                "$lte": ["$joined_on", "$$day"]
                            },
                        }
                    },
                    {
                        "$lookup": {
                            "from": "attendance_logs",
                            "let": {
                                "code": "$emp_code",
                                "day": "$$day",
                            },
                            "pipeline": [
                                {
                                    "$match": {
                                        "$expr": {
                                            "$and": [
                                                {
                                                    "$eq": [
                                                        "$emp_code",
                                                        "$$code",
                                                    ]
                                                },
                                                {
                                                    "$eq": [
                                                        "$date",
                                                        "$$day",
                                                    ]
                                                },
                                            ]
                                        }
                                    }
                                }
                            ],
                            "as": "logs",
                        }
                    },
                    {
                        "$unwind": {
                            "path": "$logs",
                            "preserveNullAndEmptyArrays": True,
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "headcount": {"$sum": 1},
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$logs.status",
                                                present_statuses,
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {
                                                    "$eq": [
                                                        "$logs.half_day",
                                                        True,
                                                    ]
                                                },
                                                0.5,
                                                1.0,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                "$logs.late_minutes",
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "daily",
            }
        },
        {
            "$set": {
                "daily": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$daily", 0]},
                        {
                            "headcount": 0,
                            "present_count": 0,
                            "late_count": 0,
                        },
                    ]
                }
            }
        },
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$calendar_date",
                    }
                },
                "headcount": "$daily.headcount",
                "present_count": "$daily.present_count",
                "late_count": "$daily.late_count",
                "is_working_day": {
                    "$and": [
                        {
                            "$gte": [
                                {
                                    "$dayOfWeek": "$calendar_date"
                                },
                                2,
                            ]
                        },
                        {
                            "$lte": [
                                {
                                    "$dayOfWeek": "$calendar_date"
                                },
                                6,
                            ]
                        },
                    ]
                },
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        round_half_up_4({"$divide": [{"$toDecimal": "$present_count"}, {"$toDecimal": "$headcount"}]}),
                        None,
                    ]
                }
            }
        },

        # Rolling window includes the current date and previous six rows.
        {
            "$setWindowFields": {
                "sortBy": {"calendar_date": 1},
                "output": {
                    "rate_window": {
                        "$push": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$set": {
                "valid_rates": {
                    "$filter": {
                        "input": "$rate_window",
                        "as": "rate",
                        "cond": {"$ne": ["$$rate", None]},
                    }
                }
            }
        },
        {
            "$set": {
                "moving_avg_7d": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_rates"}, 0]},
                        round_half_up_4(
                            {"$avg": "$valid_rates"}
                        ),
                        None,
                    ]
                }
            }
        },

        # Sort before removing calendar_date from the documents.
        {"$sort": {"calendar_date": 1}},

        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {"$ne": ["$attendance_rate", None]},
                        {"$toDouble": "$attendance_rate"},
                        None,
                    ]
                },
                "moving_avg_7d": {
                    "$cond": [
                        {"$ne": ["$moving_avg_7d", None]},
                        {"$toDouble": "$moving_avg_7d"},
                        None,
                    ]
                },
            }
        },
    ]



# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1)
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")

        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self



class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"



class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )



class AttendancePatchIn(BaseModel):
    status: Optional[Literal[
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    ]] = None

    punch_in: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )

    punch_out: Optional[StrictInt] = Field(
        default=None,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )

    by: str = Field(min_length=1)
    reason: str = Field(min_length=1)



# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #


@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="database unavailable",
        )


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc



@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(default=1, ge=1), page_size: int = Query(default=20, ge=1, le=100),):
    q = {}
    if department:
        q["department"] = department
    skip = (page-1) * page_size             # 
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip).
        limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}



@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = (
        datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
        .replace(tzinfo=None, microsecond=0)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    )

    d = ts.replace(tzinfo=timezone.utc).astimezone(IST).date().isoformat()
    
    punch_in_ist = ts.replace(tzinfo=timezone.utc).astimezone(IST).replace(tzinfo=None)

    
    punch_time = punch_in_ist.time()
    if emp["shift_end"] <= emp["shift_start"] and punch_time < datetime.strptime(emp["shift_end"], "%H:%M").time():
        d = (datetime.fromisoformat(d) - timedelta(days=1)).date().isoformat()


    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punch_in_ist, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        res = db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="already punched in for this date",
        )

    doc.pop("_id", None)
    return doc



@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    # Validate date filters
    for field_name, value in (
        ("date_from", date_from),
        ("date_to", date_to),
    ):
        if value is not None:
            try:
                parsed_date = datetime.strptime(value, "%Y-%m-%d")
                if parsed_date.date().isoformat() != value:
                    raise ValueError
            except ValueError:
                raise HTTPException(
                    status_code=422,
                    detail=f"{field_name} must use YYYY-MM-DD format",
                )

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must be less than or equal to date_to",
        )

    # Validate status filter
    allowed_statuses = {
        "PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"
    }
    if status is not None and status not in allowed_statuses:
        raise HTTPException(
            status_code=422,
            detail="invalid attendance status",
        )

    # Build MongoDB filters
    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    if status is not None:
        query["status"] = status

    # Count matching records before pagination
    total = db.attendance_logs.count_documents(query)

    # Fetch only the requested page from MongoDB
    skip = (page - 1) * page_size

    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )

    items = list(cursor)

    # Convert MongoDB documents into API response format
    def to_epoch_ms(value):
        if value is None:
            return None

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)

        return int(value.timestamp() * 1000)

    for item in items:
        item["id"] = str(item.pop("_id"))

        item["punch_in"] = to_epoch_ms(item.get("punch_in"))
        item["punch_out"] = to_epoch_ms(item.get("punch_out"))

        for entry in item.get("history", []):
            entry["at"] = to_epoch_ms(entry.get("at"))

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
    }



@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    # Validate employee
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    # Use the supplied timestamp, or the current UTC time
    if body.punched_at is None:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)
    else:
        punch_out_utc = datetime.fromtimestamp(
            body.punched_at / 1000,
            tz=timezone.utc,
        ).replace(microsecond=0)

    # Store timestamps as naive UTC datetimes, consistent with punch-in
    punch_out_utc_naive = punch_out_utc.replace(tzinfo=None)

    
    # Find the latest open punch-in for this employee
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None},
            "punch_out": None,
        },
        sort=[("punch_in", -1)],
    )

    if record is None:
        closed_record = db.attendance_logs.find_one(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None},
                "punch_out": {"$ne": None},
            },
            sort=[("punch_in", -1)],
        )

        if closed_record is not None:
            raise HTTPException(
                status_code=409,
                detail="attendance already punched out",
            )

        raise HTTPException(
            status_code=404,
            detail="no matching punch-in found",
        )


    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is not None:
        punch_in_utc = punch_in_utc.astimezone(timezone.utc).replace(tzinfo=None)

    elapsed = (punch_out_utc_naive - punch_in_utc).total_seconds()

    if elapsed <= 0 or elapsed > 24 * 60 * 60:
        raise HTTPException(
            status_code=422,
            detail="punched_at must be after punch-in and within 24 hours",
        )

    # Calculate work duration
    work_hours = compute_work_hours(punch_in_utc, punch_out_utc_naive)
    half_day = compute_half_day(work_hours)

    # Calculate overtime using the employee's shift and attendance date
    ist = timezone(timedelta(hours=5, minutes=30))
    punch_out_ist = punch_out_utc.astimezone(ist)
    attendance_date = record["date"]
    shift_end = employee["shift_end"]
    overnight = shift_end <= employee["shift_start"]

    overtime_minutes = compute_overtime(
        punch_out_utc_naive,
        shift_end,
        attendance_date,
        overnight=overnight,
    )

    # Atomic update: only one concurrent request can close this record
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "punch_out": None,
            "punch_in": punch_in_utc,
        },
        {
            "$set": {
                "punch_out": punch_out_utc_naive,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": half_day,
            }
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="attendance already punched out",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    if updated is None:
        raise HTTPException(
            status_code=500,
            detail="failed to retrieve updated attendance",
        )

    updated.pop("_id", None)

    # Return epoch milliseconds for timestamp fields
    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    return updated



@app.patch("/attendance/{emp_code}/{date}")
def patch_attendance(
    emp_code: str,
    date: str,
    body: AttendancePatchIn,
):
    # Check whether the employee exists
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="employee not found",
        )

    # Check whether the attendance record exists
    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date}
    )
    if record is None:
        raise HTTPException(
            status_code=404,
            detail="attendance record not found",
        )
    
    changes_requested = body.model_dump(exclude_unset=True)
    
    if "status" in changes_requested and changes_requested["status"] is None:
        raise HTTPException(
            status_code=422,
            detail="status cannot be null"
        )


    allowed_fields = {"status", "punch_in", "punch_out", "by", "reason"}

    if not changes_requested:
        raise HTTPException(
            status_code=422,
            detail="at least one field must be provided",
        )

    by = changes_requested.get("by")
    reason = changes_requested.get("reason")

    if not by or not reason or not by.strip() or not reason.strip():
        raise HTTPException(
            status_code=422,
            detail="by and reason are required",
        )

    changes_requested.pop("by", None)
    changes_requested.pop("reason", None)

    
    invalid_fields = set(changes_requested) - {
        "status", "punch_in", "punch_out"
    }

    if invalid_fields:
        raise HTTPException(
            status_code=422,
            detail="unsupported attendance fields",
        )

    if (
        changes_requested.get("status") in {"ABSENT", "LEAVE"}
        and (
            changes_requested.get("punch_in") is not None
            or changes_requested.get("punch_out") is not None
        )
    ):
        raise HTTPException(
            status_code=422,
            detail="ABSENT and LEAVE cannot have punch times",
        )


    if not changes_requested:
        raise HTTPException(
            status_code=422,
            detail="at least one attendance field must be changed",
        )
    
    # Keep the existing values unless the request explicitly changes them.
    new_status = changes_requested.get("status", record["status"])
    new_punch_in = record.get("punch_in")
    new_punch_out = record.get("punch_out")

    if "punch_in" in changes_requested:
        new_punch_in = changes_requested["punch_in"]

    if "punch_out" in changes_requested:
        new_punch_out = changes_requested["punch_out"]


    # ABSENT and LEAVE records cannot contain punch times.
    if new_status in {"ABSENT", "LEAVE"}:
        new_punch_in = None
        new_punch_out = None

    # Presence statuses require a punch-in timestamp.
    elif new_status in {"PRESENT", "WFH", "ON_DUTY"}:
        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="presence status requires punch_in",
            )


    # Convert epoch milliseconds to naive UTC datetimes.
    def to_utc_datetime(value):
        if value is None:
            return None
        return datetime.fromtimestamp(
            value / 1000,
            tz=timezone.utc,
        ).replace(tzinfo=None, microsecond=0)

    if isinstance(new_punch_in, int):
        new_punch_in = to_utc_datetime(new_punch_in)

    if isinstance(new_punch_out, int):
        new_punch_out = to_utc_datetime(new_punch_out)

    if new_punch_in is not None and new_punch_out is not None:
        elapsed = (new_punch_out - new_punch_in).total_seconds()

        if elapsed <= 0 or elapsed > 24 * 60 * 60:
            raise HTTPException(
                status_code=422,
                detail="punch_out must be after punch_in and within 24 hours",
            )

    # Validate the attendance date using the punch-in time in IST.
    if new_punch_in is not None:
        punch_in_ist = (
            new_punch_in.replace(tzinfo=timezone.utc)
            .astimezone(IST)
        )

        expected_date = punch_in_ist.date().isoformat()
        punch_time = punch_in_ist.time().replace(tzinfo=None)

        shift_end_time = datetime.strptime(
            employee["shift_end"], "%H:%M"
        ).time()

        if (
            employee["shift_end"] <= employee["shift_start"]
            and punch_time < shift_end_time
        ):
            expected_date = (
                punch_in_ist.date() - timedelta(days=1)
            ).isoformat()

        if expected_date != date:
            raise HTTPException(
                status_code=422,
                detail="punch_in does not match attendance date",
            )


    # Calculate derived attendance fields.
    if new_punch_in is not None and new_punch_out is not None:
        work_hours = compute_work_hours(new_punch_in, new_punch_out)
    else:
        work_hours = None

    half_day = compute_half_day(work_hours)

    if new_punch_in is not None:
        punch_in_ist = new_punch_in.replace(
            tzinfo=timezone.utc
        ).astimezone(IST).replace(tzinfo=None)

        late_minutes = compute_late_minutes(
            punch_in_ist,
            employee["shift_start"],
        )
    else:
        late_minutes = 0

    if new_punch_out is not None:
        overnight = employee["shift_end"] <= employee["shift_start"]

        overtime_minutes = compute_overtime(
            new_punch_out,
            employee["shift_end"],
            date,
            overnight=overnight,
        )
    else:
        overtime_minutes = 0



    # Build the fields that will be updated in MongoDB.
    update_fields = {
        "status": new_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }

    # Record only the fields whose values actually changed.
    history_changes = {}

    for field, new_value in update_fields.items():
        old_value = record.get(field)

        if old_value != new_value:
            if field in {"punch_in", "punch_out"}:
                old_value = (
                    int(old_value.replace(tzinfo=timezone.utc).timestamp() * 1000)
                    if old_value is not None
                    else None
                )
                new_value_for_history = (
                    int(new_value.replace(tzinfo=timezone.utc).timestamp() * 1000)
                    if new_value is not None
                    else None
                )
            else:
                new_value_for_history = new_value

            history_changes[field] = {
                "from": old_value,
                "to": new_value_for_history,
            }

    if not history_changes:
        raise HTTPException(
            status_code=422,
            detail="no attendance changes detected",
        )

    history_entry = {
        "at": datetime.now(timezone.utc).replace(microsecond=0),
        "by": by.strip(),
        "reason": reason.strip(),
        "changes": history_changes,
    }

    # Update and append history atomically.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {
            "$set": update_fields,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count != 1:
        raise HTTPException(
            status_code=409,
            detail="attendance record changed concurrently; retry correction",
        )

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    updated.pop("_id", None)

    for field in ("punch_in", "punch_out"):
        value = updated.get(field)
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            updated[field] = int(value.timestamp() * 1000)

    for entry in updated.get("history", []):
        value = entry.get("at")
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            entry["at"] = int(value.timestamp() * 1000)

    return updated



@app.get("/analytics/monthly")
def monthly_analytics(month: str):
    # Validate YYYY-MM
    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
        if parsed_month.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    month_start = parsed_month.date().replace(day=1)

    if parsed_month.month == 12:
        next_month = month_start.replace(
            year=parsed_month.year + 1, month=1
        )
    else:
        next_month = month_start.replace(
            month=parsed_month.month + 1
        )

    month_end = next_month - timedelta(days=1)
    month_end_str = month_end.isoformat()
    month_start_str = month_start.isoformat()

    # Employees who had joined by the end of the requested month
    employees = list(
        db.employees.find(
            {"joined_on": {"$lte": month_end_str}},
            {"_id": 0},
        ).sort("emp_code", 1)
    )

    # Attendance records within the requested month
    attendance = list(
        db.attendance_logs.find(
            {
                "date": {
                    "$gte": month_start_str,
                    "$lte": month_end_str,
                }
            },
            {"_id": 0},
        )
    )

    attendance_by_employee = {}
    for record in attendance:
        attendance_by_employee.setdefault(
            record["emp_code"], []
        ).append(record)

    # Count Monday-Friday working days from each employee's join date
    # through the end of the requested month.
    def count_weekdays(start_date, end_date):
        days = 0
        current = start_date
        while current <= end_date:
            if current.weekday() < 5:
                days += 1
            current += timedelta(days=1)
        return days

    employee_summaries = []

    total_present_days = 0.0
    total_leave_days = 0
    total_late_minutes = 0
    total_overtime_minutes = 0

    for employee in employees:
        emp_code = employee["emp_code"]
        joined_on = datetime.strptime(
            employee["joined_on"], "%Y-%m-%d"
        ).date()

        effective_start = max(joined_on, month_start)

        working_days = (
            count_weekdays(effective_start, month_end)
            if effective_start <= month_end
            else 0
        )

        records = attendance_by_employee.get(emp_code, [])

        present_days = 0.0
        leave_days = 0
        late_minutes = 0
        overtime_minutes = 0

        for record in records:
            status = record.get("status")

            if status in {"PRESENT", "WFH", "ON_DUTY"}:
                present_days += (
                    0.5 if record.get("half_day") is True else 1.0
                )
            elif status == "LEAVE":
                leave_days += 1

            # Weekend attendance can still contribute to these totals.
            late_minutes += record.get("late_minutes") or 0
            overtime_minutes += record.get("overtime_minutes") or 0

        total_present_days += present_days
        total_leave_days += leave_days
        total_late_minutes += late_minutes
        total_overtime_minutes += overtime_minutes

        employee_summaries.append({
            "emp_code": emp_code,
            "name": employee["name"],
            "department": employee["department"],
            "working_days": working_days,
            "present_days": present_days,
            "leave_days": leave_days,
            "late_minutes": late_minutes,
            "overtime_minutes": overtime_minutes,
        })

    return {
        "month": month,
        "headcount": len(employees),
        "present_days": total_present_days,
        "leave_days": total_leave_days,
        "late_minutes": total_late_minutes,
        "overtime_minutes": total_overtime_minutes,
        "employees": employee_summaries,
    }



@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    # Validate the requested month.
    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
        if parsed_month.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    month_start = parsed_month.date().replace(day=1)

    if parsed_month.month == 12:
        next_month = month_start.replace(
            year=parsed_month.year + 1,
            month=1,
        )
    else:
        next_month = month_start.replace(
            month=parsed_month.month + 1,
        )

    month_end = next_month - timedelta(days=1)
    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()

    # Start with employees who joined by the end of this month.
    employee_match = {
        "joined_on": {"$lte": month_end_str}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},

        # Retrieve this employee's attendance records for the month.
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$employee_code",
                                        ]
                                    },
                                    {
                                        "$gte": [
                                            "$date",
                                            month_start_str,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$date",
                                            month_end_str,
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },

        # Determine whether the attendance date is Monday-Friday.
        {
            "$set": {
                "logs": {
                    "$cond": [
                        {"$isArray": "$logs"},
                        {
                            "$map": {
                                "input": "$logs",
                                "as": "log",
                                "in": {
                                    "$mergeObjects": [
                                        "$$log",
                                        {
                                            "is_working_day": {
                                                "$cond": [
                                                    {
                                                        "$ne": [
                                                            "$$log.date",
                                                            None,
                                                        ]
                                                    },
                                                    {
                                                        "$in": [
                                                            {
                                                                "$dayOfWeek": {
                                                                    "$dateFromString": {
                                                                        "dateString": "$$log.date"
                                                                    }
                                                                }
                                                            },
                                                            [2, 3, 4, 5, 6],
                                                        ]
                                                    },
                                                    False,
                                                ]
                                            }
                                        },
                                    ]
                                },
                            }
                        },
                        [],
                    ]
                }
            }
        },

        # Keep employees who have no attendance records.
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True,
            }
        },

        # First aggregate per employee so multiple attendance records
        # do not inflate the department headcount.
        {
            "$group": {
                "_id": {
                    "emp_code": "$emp_code",
                    "department": "$department",
                },
                "headcount": {"$first": 1},

                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$eq": ["$logs.is_working_day", True]},
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$logs.half_day", True]},
                                    0.5,
                                    1.0,
                                ]
                            },
                            0,
                        ]
                    }
                },

                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            "$logs.work_hours",
                            0,
                        ]
                    }
                },

                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$logs.status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {"$ne": ["$logs.work_hours", None]},
                                ]
                            },
                            1,
                            0,
                        ]
                    }
                },

                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$logs.late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },

                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$logs.late_minutes", 0]}
                },

                "leave_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },

                "on_duty_count": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$logs.status", "ON_DUTY"]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },

        # Then aggregate the employee summaries by department.
        {
            "$group": {
                "_id": "$_id.department",
                "headcount": {"$sum": "$headcount"},
                "present_days": {"$sum": "$present_days"},
                "work_hours_sum": {"$sum": "$work_hours_sum"},
                "work_hours_count": {"$sum": "$work_hours_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        },

        {"$sort": {"_id": 1}},

        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "work_hours_sum": 1,
                "work_hours_count": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
    ]

    results = list(db.employees.aggregate(pipeline))

    items = []

    for result in results:
        count = result.pop("work_hours_count")
        hours_sum = result.pop("work_hours_sum")

        if count == 0:
            avg_work_hours = None
        else:
            average = Decimal(str(hours_sum)) / Decimal(count)
            avg_work_hours = float(
                average.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP,
                )
            )

        items.append({
            "department": result["department"],
            "headcount": result["headcount"],
            "present_days": result["present_days"],
            "avg_work_hours": avg_work_hours,
            "late_count": result["late_count"],
            "total_late_minutes": result["total_late_minutes"],
            "leave_count": result["leave_count"],
            "on_duty_count": result["on_duty_count"],
        })

    return {
        "month": month,
        "items": items,
    }



@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    # Validate YYYY-MM format.
    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
        if parsed_month.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    month_start = parsed_month.date().replace(day=1)

    if parsed_month.month == 12:
        next_month = month_start.replace(
            year=parsed_month.year + 1,
            month=1,
        )
    else:
        next_month = month_start.replace(
            month=parsed_month.month + 1,
        )

    month_end = next_month - timedelta(days=1)
    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()

    employee_match = {}
    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start_str]},
                                    {"$lte": ["$date", month_end_str]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                        }
                    },
                ],
                "as": "late_summary",
            }
        },
        {
            "$set": {
                "total_late_minutes": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$late_summary.total_late_minutes", 0]},
                        0,
                    ]
                }
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                },
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": 1,
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
            }
        },
    ]

    items = list(db.employees.aggregate(pipeline))

    return {
        "month": month,
        "items": items,
    }


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly_analytics(emp_code: str, month: str):
    # Validate month format: YYYY-MM
    try:
        parsed_month = datetime.strptime(month, "%Y-%m")
        if parsed_month.strftime("%Y-%m") != month:
            raise ValueError
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="month must use YYYY-MM format",
        )

    month_start = parsed_month.date().replace(day=1)

    if parsed_month.month == 12:
        next_month = month_start.replace(
            year=parsed_month.year + 1,
            month=1,
        )
    else:
        next_month = month_start.replace(
            month=parsed_month.month + 1,
        )

    month_end = next_month - timedelta(days=1)
    month_start_str = month_start.isoformat()
    month_end_str = month_end.isoformat()

    # Find the employee, including employees with no attendance logs.
    employee = db.employees.find_one(
        {"emp_code": emp_code},
        {"_id": 0},
    )

    if employee is None:
        raise HTTPException(
            status_code=404,
            detail="employee not found",
        )

    # Working days begin at the later of joining date and month start.
    joined_on = date.fromisoformat(employee["joined_on"])
    working_start = max(joined_on, month_start)

    working_days = 0
    current_day = working_start

    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    # Aggregate attendance metrics in MongoDB.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start_str,
                    "$lte": month_end_str,
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {
                                        "$in": [
                                            "$status",
                                            ["PRESENT", "WFH", "ON_DUTY"],
                                        ]
                                    },
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {
                                                        "dateString": "$date"
                                                    }
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    # An employee may have no attendance records for this month.
    stats = results[0] if results else {}

    present_days = stats.get("present_days", 0)

    attendance_pct = None
    if working_days > 0:
        attendance_pct = float(
            (
                Decimal(str(present_days))
                / Decimal(working_days)
                * Decimal("100")
            ).quantize(
                Decimal("0.0001"),
                rounding=ROUND_HALF_UP,
            )
        )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": stats.get("leave_days", 0),
        "late_count": stats.get("late_count", 0),
        "total_late_minutes": stats.get("total_late_minutes", 0),
        "total_overtime_minutes": stats.get(
            "total_overtime_minutes", 0
        ),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(alias="from"),
    to_date: str = Query(alias="to"),
):
    # Validate dates.
    try:
        start_date = date.fromisoformat(from_date)
        end_date = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    # Reject invalid calendar date representations.
    if (
        start_date.isoformat() != from_date
        or end_date.isoformat() != to_date
    ):
        raise HTTPException(
            status_code=422,
            detail="Dates must use YYYY-MM-DD format",
        )

    if start_date > end_date:
        raise HTTPException(
            status_code=422,
            detail="'from' must be on or before 'to'",
        )

    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    # Confirm that the department exists.
    if db.employees.count_documents(
        {"department": department}
    ) == 0:
        raise HTTPException(
            status_code=404,
            detail="department not found",
        )

    pipeline = build_department_trend_pipeline(
        department,
        from_date,
        to_date,
    )

    # $documents is a database-level aggregation stage.
    # items = list(db.aggregate(pipeline))
    items = list(db.employees.aggregate(pipeline))

    return {
        "department": department,
        "from": from_date,
        "to": to_date,
        "items": items,
    }



@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    month: Optional[str] = None,
    department: Optional[str] = None,
    emp_code: Optional[str] = None,

    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),

    from_date: Optional[str] = Query(default=None, alias="from"),
    to_date: Optional[str] = Query(default=None, alias="to"),

    limit: int = Query(default=10, ge=1, le=50),
):
    import json
    from bson import json_util

    def validate_month(value):
        if value is None:
            raise HTTPException(
                status_code=422,
                detail="month is required for this endpoint",
            )

        try:
            parsed = datetime.strptime(value, "%Y-%m")
            if parsed.strftime("%Y-%m") != value:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail="month must use YYYY-MM format",
            )

        return parsed

    def validate_date(value, field_name):
        if value is None:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} is required for this endpoint",
            )

        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
            if parsed.strftime("%Y-%m-%d") != value:
                raise ValueError
        except ValueError:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} must use YYYY-MM-DD format",
            )

        return parsed.date()

    def explain_find(
        collection_name,
        filter_query,
        sort=None,
        skip=None,
        find_limit=None,
    ):
        command = {
            "find": collection_name,
            "filter": filter_query,
        }

        if sort:
            command["sort"] = dict(sort)

        if skip is not None:
            command["skip"] = skip

        if find_limit is not None:
            command["limit"] = find_limit

        return db.command(
            "explain",
            command,
            verbosity="executionStats",
        )

    def explain_aggregate(collection_name, pipeline):
        return db.command(
            "explain",
            {
                "aggregate": collection_name,
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    collection_name = ""
    explain_result = {}


    # 1. Attendance list
    if endpoint == "attendance_list":
        collection_name = "attendance_logs"
        query = {}

        # Apply employee-code filter when supplied.
        if emp_code is not None:
            if not emp_code.strip():
                raise HTTPException(
                    status_code=422,
                    detail="emp_code cannot be empty",
                )

            query["emp_code"] = emp_code.strip()

        # Apply department filter when supplied.
        if department is not None:
            employee_codes = [
                item["emp_code"]
                for item in db.employees.find(
                    {"department": department},
                    {"_id": 0, "emp_code": 1},
                )
            ]

            # Combine department and emp_code filters safely.
            if emp_code is not None:
                if emp_code.strip() not in employee_codes:
                    query["emp_code"] = {"$in": []}
            else:
                query["emp_code"] = {"$in": employee_codes}

        # Apply an inclusive date range when supplied.
        if from_date is not None or to_date is not None:
            start_date = validate_date(from_date, "from")
            end_date = validate_date(to_date, "to")

            if end_date < start_date:
                raise HTTPException(
                    status_code=422,
                    detail="'from' must be on or before 'to'",
                )

            query["date"] = {
                "$gte": start_date.isoformat(),
                "$lte": end_date.isoformat(),
            }

        explain_result = explain_find(
            collection_name,
            query,
            sort=[("date", -1), ("emp_code", 1)],
            skip=(page - 1) * page_size,
            find_limit=page_size,
        )


    # 2. Monthly employee analytics
    elif endpoint == "employee_monthly":
        parsed = validate_month(month)
        month_start = parsed.date().replace(day=1)

        if parsed.month == 12:
            next_month = month_start.replace(
                year=parsed.year + 1,
                month=1,
            )
        else:
            next_month = month_start.replace(
                month=parsed.month + 1,
            )

        month_end = next_month - timedelta(days=1)

        # Employee code is required for this endpoint.
        if emp_code is None or not emp_code.strip():
            raise HTTPException(
                status_code=422,
                detail="emp_code is required for this endpoint",
            )

        # Build the monthly attendance filter.
        match = {
            "date": {
                "$gte": month_start.isoformat(),
                "$lte": month_end.isoformat(),
            },
            "emp_code": emp_code.strip(),
        }

        # If department is supplied, verify that the employee
        # belongs to that department without replacing emp_code.
        if department is not None:
            employee = db.employees.find_one(
                {
                    "emp_code": emp_code.strip(),
                    "department": department,
                },
                {"_id": 0, "emp_code": 1},
            )

            if employee is None:
                # No employee matches both filters.
                match["emp_code"] = {"$in": []}

        collection_name = "attendance_logs"

        explain_result = explain_aggregate(
            collection_name,
            [
                {"$match": match},
                {"$sort": {"date": 1, "emp_code": 1}},
            ],
        )

    # 3. Department summary
    elif endpoint == "department_summary":
        parsed = validate_month(month)
        month_start = parsed.date().replace(day=1)

        if parsed.month == 12:
            next_month = month_start.replace(
                year=parsed.year + 1,
                month=1,
            )
        else:
            next_month = month_start.replace(
                month=parsed.month + 1,
            )

        month_end = next_month - timedelta(days=1)

        employee_match = {
            "joined_on": {"$lte": month_end.isoformat()}
        }

        if department is not None:
            employee_match["department"] = department

        collection_name = "employees"

        explain_result = explain_aggregate(
            collection_name,
            [
                {"$match": employee_match},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"employee_code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$emp_code",
                                                    "$$employee_code",
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    "$date",
                                                    month_start.isoformat(),
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    "$date",
                                                    month_end.isoformat(),
                                                ]
                                            },
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "logs",
                    }
                },
                {"$sort": {"department": 1}},
            ],
        )

    # 4. Late leaderboard
    elif endpoint == "late_leaderboard":
        parsed = validate_month(month)
        month_start = parsed.date().replace(day=1)

        if parsed.month == 12:
            next_month = month_start.replace(
                year=parsed.year + 1,
                month=1,
            )
        else:
            next_month = month_start.replace(
                month=parsed.month + 1,
            )

        month_end = next_month - timedelta(days=1)

        employee_match = {}
        if department is not None:
            employee_match["department"] = department

        collection_name = "employees"

        explain_result = explain_aggregate(
            collection_name,
            [
                {"$match": employee_match},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {"employee_code": "$emp_code"},
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$eq": [
                                                    "$emp_code",
                                                    "$$employee_code",
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    "$date",
                                                    month_start.isoformat(),
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    "$date",
                                                    month_end.isoformat(),
                                                ]
                                            },
                                        ]
                                    }
                                }
                            },
                            {
                                "$group": {
                                    "_id": None,
                                    "total_late_minutes": {
                                        "$sum": {
                                            "$ifNull": [
                                                "$late_minutes",
                                                0,
                                            ]
                                        }
                                    },
                                }
                            },
                        ],
                        "as": "late_summary",
                    }
                },
                {
                    "$set": {
                        "total_late_minutes": {
                            "$ifNull": [
                                {
                                    "$arrayElemAt": [
                                        "$late_summary.total_late_minutes",
                                        0,
                                    ]
                                },
                                0,
                            ]
                        }
                    }
                },
                {"$match": {"total_late_minutes": {"$gt": 0}}},
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {
                    "$sort": {
                        "total_late_minutes": -1,
                        "emp_code": 1,
                    }
                },
            ],
        )

    # 5. Department trend
    elif endpoint == "department_trend":
        start_date = validate_date(from_date, "from")
        end_date = validate_date(to_date, "to")

        if end_date < start_date:
            raise HTTPException(
                status_code=422,
                detail="'from' must be on or before 'to'",
            )

        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        if department is None or not department.strip():
            raise HTTPException(
                status_code=422,
                detail="department is required for this endpoint",
            )

        if db.employees.count_documents(
            {"department": department}
        ) == 0:
            raise HTTPException(
                status_code=404,
                detail="department not found",
            )

        pipeline = build_department_trend_pipeline(
            department,
            from_date,
            to_date,
        )

        # collection_name = "database-level aggregation"
        collection_name = "employees"

        # Explain the same collectionless aggregation with execution stats.
        explain_result = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {},
            },
            verbosity="executionStats",
        )

    return json.loads(
        json_util.dumps(
            {
                "endpoint": endpoint,
                "collection": collection_name,
                "explain": explain_result,
            }
        )
    )
