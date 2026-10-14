<!-- # Candidate kit

Read in this order:

1. `PROBLEM_STATEMENT.docx`: what you are asked to do, how it is evaluated.
2. `openapi.yaml`: the API contract and the business rules (R1 - R10).
3. `DATA_MODEL.md`: what is stored in MongoDB, with nine sample documents.
4. `app/main.py`: the starter. Put **all** your code in this one file. It must start with `uvicorn app.main:app --port 8000`.
5. `REVIEW.md`, `DECISIONS.md`: templates to fill in.

`sample_data/` holds the nine sample documents as MongoDB Extended JSON; `sample_seed.py` loads them. The grader's dataset is
much larger and contains cases they do not show.

## Local setup
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # set MONGO_URI / MONGO_DB (local docker: docker run -p 27017:27017 mongo:7)
python sample_seed.py
uvicorn app.main:app --port 8000 --reload
```
MongoDB **6.0 or newer** is required (the grader uses 7.0). MongoDB Atlas free tier (M0) works.

## Submit
A public Git repository with `app/main.py`, `requirements.txt`, `REVIEW.md`, `DECISIONS.md` and a short `README.md`.
No `.env`, no secrets, no Dockerfile. -->



# Employee Attendance & Analytics API

A FastAPI and MongoDB API for employee records, attendance punch-in/punch-out, audited corrections, and attendance analytics.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (local or MongoDB Atlas)
- Git

## Setup

Install dependencies:

```powershell
uv pip install -r requirements.txt
```

Create a local `.env` file (do not commit it). The current `app/main.py` reads `MONGO_URI` and optional `MONGO_DB`; the default database is `attendance_db`.

```dotenv
MONGO_URI=your_mongodb_connection_string
MONGO_DB=attendance_db
```

Keep the real connection string private. Do not commit `.env`.

Start the API from the repository root:

```powershell
uv run uvicorn app.main:app --reload --port 8000
```

Interactive API documentation: `http://127.0.0.1:8000/docs`.

## Health check

Request `GET /health`.

Successful response when MongoDB is reachable:

```json
{
  "status": "ok"
}
```

If the database ping fails, the endpoint returns HTTP `503` with `database unavailable`. This is a basic application-to-database connectivity check, not a load test.

## Endpoint groups

- **Employees:** create and list employee records with department filtering and pagination.
- **Attendance:** list attendance, punch in, punch out, and correct records with an audit trail.
- **Analytics:** employee monthly metrics, department summary, late-arrival leaderboard, and daily department trend.
- **Admin explain:** MongoDB `executionStats` explain output for supported query/aggregation shapes.

Use `/docs` and `openapi.yaml` for exact request and response schemas.

## Implementation notes

- Punch timestamps are epoch milliseconds; instants are stored as UTC datetimes and attendance dates/shifts are interpreted in IST.
- Punch-in status is restricted to `PRESENT`, `WFH`, and `ON_DUTY`.
- A unique compound index prevents duplicate attendance records for one employee/date.
- Attendance corrections validate status/time combinations and append audit history.
- Analytics use MongoDB aggregation pipelines for monthly, department, leaderboard and trend calculations.

## Documentation and status

- `REVIEW.md` records defects, fixes, observed tests and remaining checks.
- `DECISIONS.md` explains design choices and trade-offs.
- `openapi.yaml` is the API contract.

Several routes and validation/analytics/explain scenarios have been manually tested through Swagger. See `REVIEW.md` for details. The project should not be described as fully verified until its remaining checks are completed.
