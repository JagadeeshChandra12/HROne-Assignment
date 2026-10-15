# HROne Attendance Management API

A FastAPI + MongoDB backend for an HR attendance system: employees punch in and out, HR can
regularize (correct) records, and a set of MongoDB aggregation pipelines answer analytics
questions over the data (who is late, how a department is trending, and so on).

Built for the HROne Software Engineer Trainee engineering assignment. The full contract —
every path, status code, and business rule (R1–R10) — lives in [`openapi.yaml`](openapi.yaml);
the MongoDB document shapes live in [`DATA_MODEL.md`](DATA_MODEL.md).

## Overview

The service exposes a single FastAPI application (`app/main.py`) backed by two MongoDB
collections, `employees` and `attendance_logs`. It supports:

- Employee onboarding and lookup.
- Punch-in / punch-out, with automatic computation of lateness, overtime, work hours, and
  half-day status.
- Manual regularization of a day's record, with a full audit trail.
- Four analytics endpoints, each implemented as a MongoDB aggregation pipeline rather than
  by loading documents into Python.
- An `/admin/explain` endpoint that returns the MongoDB `explain()` output for an endpoint's
  main query, so index usage can be verified directly.

## Key features

- **IST-aware attendance dates** — a record's calendar date follows the employee's shift in
  IST, including correct handling of overnight shifts (e.g. 22:00–06:00).
- **Derived fields computed server-side** — `late_minutes`, `overtime_minutes`, `work_hours`,
  and `half_day` are always computed by the API from stored punch times; clients cannot set
  them directly.
- **Race-safe writes** — duplicate employee creation and duplicate punch-ins are rejected via
  MongoDB unique indexes (not check-then-insert); punch-out and regularization use atomic
  `find_one_and_update` so concurrent requests cannot silently clobber each other.
- **Append-only audit trail** — every regularization appends one entry to a record's
  `history` array; nothing is ever edited or removed.
- **Aggregation-based analytics** — monthly employee summaries, department summaries, a late
  leaderboard with standard competition ranking, and a department trend with gap-filling and
  a moving average.
- **Index-backed queries** — every list and analytics endpoint is designed to run through an
  index rather than a collection scan, and this can be checked directly via
  `GET /admin/explain/{endpoint}`.

## Technology stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Web framework | [FastAPI](https://fastapi.tiangolo.com/) |
| ASGI server | [Uvicorn](https://www.uvicorn.org/) |
| Database | MongoDB 6.0+ |
| Database driver | [PyMongo](https://pymongo.readthedocs.io/) (synchronous) |
| Validation | [Pydantic v2](https://docs.pydantic.dev/) |
| Config | `python-dotenv` (optional local `.env`) |

## Architecture and request flow

```
Client (HTTP/JSON)
   │
   ▼
FastAPI routes (app/main.py)
   │  - Pydantic models validate/parse the request body and query params
   │  - Path/query parameters are pattern- and range-checked (dates, epoch ms, enums)
   ▼
Business logic (pure functions in app/main.py)
   │  - IST conversion, late/overtime/work-hours calculation (R1–R5)
   │  - Filter and aggregation pipeline builders, shared with /admin/explain
   ▼
PyMongo (synchronous driver)
   │  - Unique indexes enforce identity/uniqueness instead of check-then-insert
   │  - find_one_and_update used for atomic, race-safe updates
   ▼
MongoDB (employees, attendance_logs)
```

Synchronous PyMongo calls are made from regular `def` (not `async def`) route handlers, so
FastAPI runs each request in its own worker thread — blocking database calls don't block the
event loop. All indexes are created idempotently at startup via FastAPI's `lifespan` handler,
so a fresh clone needs no manual database setup step.

## Project structure

```
.
├── app/
│   ├── __init__.py
│   └── main.py              # The entire application: models, routes, aggregation pipelines
├── sample_data/
│   ├── employees.json       # 6 sample employees (MongoDB Extended JSON)
│   └── attendance_logs.json # 9 sample attendance records covering every documented case
├── sample_seed.py           # Loads sample_data/ into MongoDB
├── openapi.yaml             # HTTP contract: paths, schemas, status codes, rules R1–R10
├── DATA_MODEL.md            # MongoDB collection/field reference with annotated samples
├── REVIEW.md                # Part A: defects found in the starter code and how they were fixed
├── DECISIONS.md             # Part C: index choices, concurrency, ranking, and design trade-offs
├── PROBLEM_STATEMENT.docx   # The original assignment brief
└── requirements.txt
```

## Prerequisites

- Python 3.11+
- A MongoDB 6.0+ instance (local, Docker, or Atlas free tier) — the database can be empty;
  all required indexes are created automatically on startup

## Installation and setup

```bash
git clone https://github.com/JagadeeshChandra12/HROne-Assignment.git
cd HROne-Assignment

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## Environment variables

The app reads two environment variables at startup. A local `.env` file is loaded for
convenience, but real environment variables always take priority over it.

| Variable | Default | Purpose |
|---|---|---|
| `MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `MONGO_DB` | `attendance_db` | Database name |

Example `.env` (placeholders only — never commit a real one):

```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

## Running the application

```bash
# optional: load the 9 sample documents described in DATA_MODEL.md
python sample_seed.py

uvicorn app.main:app --port 8000
```

`GET /health` returns `200` once MongoDB answers a ping (and `503` otherwise), and all
indexes are created on startup — there are no manual migration steps.

## API endpoint reference

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Readiness probe; `200` only if MongoDB responds to a ping |
| POST | `/employees` | Create an employee (`emp_code` unique, enforced by a DB index) |
| GET | `/employees` | List employees, filterable by `department`, paginated |
| POST | `/attendance/punch-in` | Record a punch-in; computes the attendance date (R1) and lateness (R2) |
| POST | `/attendance/punch-out` | Close the day's record; computes work hours, overtime, half-day (R3–R5) |
| GET | `/attendance` | List attendance records; filter by employee, date range, status; paginated |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize one day's record; recomputes derived fields and appends to `history` |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly attendance summary for one employee |
| GET | `/analytics/departments/summary` | Department-level summary for a month (headcount, presence, lateness, overtime) |
| GET | `/analytics/leaderboard/late` | Late-arrival leaderboard for a month, with competition ranking |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend for a department over a date range |
| GET | `/admin/explain/{endpoint}` | Returns MongoDB's `explain()` output for an endpoint's main query/pipeline |

Full request/response schemas, required fields, and status codes are defined in
[`openapi.yaml`](openapi.yaml).

## Example requests and responses

**Create an employee**

```bash
curl -X POST http://localhost:8000/employees \
  -H "Content-Type: application/json" \
  -d '{
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
      }'
```

```json
{
  "emp_code": "EMP0001",
  "name": "Asha Rao",
  "email": "asha@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05",
  "created_at": 1767587400000
}
```

**Punch in**

```bash
curl -X POST http://localhost:8000/attendance/punch-in \
  -H "Content-Type: application/json" \
  -d '{"emp_code": "EMP0001"}'
```

```json
{
  "emp_code": "EMP0001",
  "date": "2026-07-06",
  "status": "PRESENT",
  "punch_in": 1783310280000,
  "punch_out": null,
  "work_hours": null,
  "late_minutes": 0,
  "overtime_minutes": 0,
  "half_day": false,
  "history": []
}
```

All instants (`punch_in`, `punch_out`, `created_at`, `history[].at`) are epoch
**milliseconds** in requests and responses; MongoDB stores them as BSON dates (UTC).
Calendar dates (`date`, `joined_on`) and shift times (`shift_start`, `shift_end`) are plain
`"YYYY-MM-DD"` / `"HH:MM"` strings in IST.

## Attendance calculation rules

These are implemented exactly as specified in `openapi.yaml` (rules R1–R5):

| Rule | Behaviour |
|---|---|
| **Attendance date (R1)** | A record's `date` is the IST calendar date of its `punch_in`. For an overnight shift (`shift_end <= shift_start`, e.g. 22:00–06:00), a punch-in earlier than `shift_end` belongs to the shift that started the **previous** day. |
| **Late arrival (R2)** | A 10-minute grace period applies. `late_minutes` is the whole number of minutes (floor) since `shift_start`, counted only if the punch-in is strictly more than 10 minutes after `shift_start`; otherwise `0`. |
| **Overtime (R3)** | Whole minutes (floor) from `shift_end` to `punch_out`, counted only if that is at least 30 minutes; otherwise `0`. For an overnight shift, `shift_end` falls on the next calendar day. |
| **Work hours (R4)** | `(punch_out - punch_in)` in seconds, divided by 3600, rounded to 2 decimals **half-up** — not banker's rounding. `null` until punch-out. |
| **Half day (R5)** | `half_day` is `true` when the rounded `work_hours` is below 4.50. A half day counts as 0.5 of a present day in analytics. |

All instants are truncated to whole seconds before any calculation, and that truncated value
is what's stored and returned.

## MongoDB indexes and concurrency handling

Indexes are created idempotently at startup (`create_indexes()`, wired into FastAPI's
`lifespan`):

| Collection | Index | Purpose |
|---|---|---|
| `employees` | `emp_code` (unique) | Enforces employee identity without a check-then-insert race |
| `employees` | `department` | Department lookups (summary, leaderboard, trend) |
| `attendance_logs` | `(emp_code, date)` (unique) | One record per employee per day; backs punch-in's duplicate check and the record's natural key lookup |
| `attendance_logs` | `(date desc, emp_code asc)` | The default attendance listing and date-range-only analytics |
| `attendance_logs` | `(emp_code, punch_in desc)` | Finds an employee's most recent record during punch-out |

**Concurrency** is handled at the database layer, not with application-level locks:

- **Duplicate employee / duplicate punch-in**: both requests pass validation and attempt an
  insert; the unique index lets MongoDB pick exactly one winner, and the loser's
  `DuplicateKeyError` is turned into a `409`.
- **Punch-out / regularization**: both use `find_one_and_update` with a filter that matches
  the record's state at read time; if another request has already changed it, the update
  matches zero documents and the loser gets a `409` instead of silently overwriting the
  winner's change.

See [`DECISIONS.md`](DECISIONS.md) for the full reasoning behind each index and race.

## Analytics features

All four analytics endpoints are implemented as MongoDB aggregation pipelines (not as
Python loops over fetched documents):

- **Employee monthly summary** — working days, present days (half-day aware), leave days,
  late count, total late/overtime minutes, and attendance percentage for one employee.
- **Department summary** — headcount (including employees with zero logs, excluding those
  who haven't joined yet), present days, average work hours, lateness, leave, and on-duty
  counts, aggregated across `employees` and `attendance_logs`.
- **Late leaderboard** — ranks employees by total late minutes using **standard competition
  ranking** (ties share a rank, e.g. 1, 2, 2, 4); `limit` is applied after ranking, so a tie
  at the cutoff can return more rows than `limit`.
- **Department trend** — exactly one row per calendar day in a date range (including
  weekends and days with no records), with a moving average over the preceding window.

## Testing and verification

The implementation was exercised manually against a real MongoDB instance (not mocks):

- The sample dataset in `sample_data/` was loaded via `sample_seed.py` and its results
  compared against the worked examples documented in `DATA_MODEL.md`.
- All endpoints were exercised directly over HTTP, including validation failures (`422`),
  not-found (`404`), and conflict (`409`) paths.
- Concurrent requests (parallel employee creation, punch-ins, punch-outs, and
  regularizations) were sent to confirm exactly-one-winner semantics and that no history
  entries are lost under contention.
- Index usage for every endpoint behind `/admin/explain` was checked directly via MongoDB's
  `explain()` output.

There is no bundled automated test suite in this repository; the assignment does not require
one, and the checks above were run ad hoc against a disposable local database.

## Assignment documentation

- [`REVIEW.md`](REVIEW.md) — Part A: the defects found in the original starter code, how
  each one would surface, and how it was fixed.
- [`DECISIONS.md`](DECISIONS.md) — Part C: which indexes were created and why, what happens
  under concurrent punch-ins, how leaderboard ties and department headcount are handled, and
  what would change at 100x the data.
