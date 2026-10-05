# Outreach Agent backend
FastAPI + SQLAlchemy + SQLite. Single user, no auth. **The agent only creates drafts; approving a draft never sends anything** (no external integrations exist yet).

```
cd backend
python -m venv .venv
.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python seed.py
uvicorn app.main:app --reload --port 8000
```
API docs: http://localhost:8000/docs. Frontend: `cd frontend && npm run dev` (reads `VITE_API_URL`, default `http://localhost:8000/api`; falls back to built-in mock data if the backend is down).

Optional env: `AGENT_INTERVAL_MINUTES=30` (auto-run), `AGENT_STEP_DELAY=0.4`, `AGENT_MAX_NEW=4`, `DATABASE_URL`.
Add a discovery source by subclassing `DiscoveryAdapter` in `app/agent/adapters.py` and appending it to `ADAPTERS`.
