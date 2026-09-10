# Running on Replit

This project is a Python FastAPI backend with no separate frontend.

- Main workflow: `uvicorn app.main:app --host 0.0.0.0 --port 5000`
- Health check: `/health`
- Interactive API documentation: `/docs`
- Tests: `pytest`

No API keys are required. By default, the SQLite cache and food log use a local
file and may not survive a deployment replacement. For production persistence,
set `DUKE_NUTRITION_DB` to a path on persistent storage.