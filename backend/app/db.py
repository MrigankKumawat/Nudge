import logging, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker, DeclarativeBase

log = logging.getLogger(__name__)

# ---- stable database location ---------------------------------------------------------------------------------------------------------------
# The default used to be "sqlite:///./outreach.db", which SQLite resolves against the directory the server was STARTED from: launching uvicorn from
# the repo root instead of backend/ silently opened (and created) a different, empty database. The default is now anchored to this file: always
# <repo>/backend/outreach.db, the same file as before when started from backend/ (the README way). Nothing is moved, copied, renamed or created here.
BACKEND_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = BACKEND_DIR / "outreach.db"

def _resolve_db_url() -> str:
    env = os.getenv("DATABASE_URL")
    if not env: return f"sqlite:///{DEFAULT_DB.as_posix()}"
    url = make_url(env)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:" and not Path(url.database).is_absolute():
        url = url.set(database=(BACKEND_DIR / url.database).as_posix())   # a relative path in DATABASE_URL is anchored to backend/ too, not to the cwd
    return url.render_as_string(hide_password=False)

def _check_default_location() -> None:
    """Only for the default location. A populated database in a place the app USED to look (cwd/outreach.db, nudge.db) is never ignored silently:
    - the stable file does not exist yet  -> refuse to start (otherwise a brand-new empty database would be created next to your real data);
    - both exist                          -> start with the stable file and warn, naming the others."""
    if os.getenv("DATABASE_URL"): return
    others = []
    for p in (Path.cwd() / "outreach.db", Path.cwd() / "nudge.db", BACKEND_DIR / "nudge.db"):
        try: p = p.resolve()
        except OSError: continue
        if p != DEFAULT_DB and p.is_file() and p.stat().st_size > 0 and p not in others: others.append(p)
    have = DEFAULT_DB.is_file() and DEFAULT_DB.stat().st_size > 0
    if others and not have:
        raise RuntimeError(f"Nudge's database should be {DEFAULT_DB}, which does not exist, but a database was found at {', '.join(map(str, others))}. "
                           f"Nothing was created or changed. Stop the server, copy that file to {DEFAULT_DB} (or set the DATABASE_URL environment variable to it), then start again.")
    if others: log.warning("Using %s. Other database file(s) exist and are being IGNORED: %s", DEFAULT_DB, ", ".join(map(str, others)))
    try:
        from dotenv import dotenv_values
        if dotenv_values(BACKEND_DIR / ".env").get("DATABASE_URL"): log.warning("backend/.env sets DATABASE_URL, but it is not applied to the database: set it as a real environment variable instead.")
    except Exception: pass

_check_default_location()
DB_URL = _resolve_db_url()
engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ---- safe schema upgrade (SQLite) ---------------------------------------------------------------------------------------------------------
# Base.metadata.create_all() creates MISSING TABLES but never adds columns to a table that already exists. This project has no Alembic, so the
# columns added after the first release are listed here and added with ALTER TABLE ... ADD COLUMN, only when they are missing. Nothing is dropped,
# renamed, rewritten or reset; existing rows keep every value and simply get NULL / the stated default in the new columns.
# Keep these in step with the models: (column, SQLite column definition). New columns must be nullable or carry a DEFAULT.
NEW_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "conversations": [
        ("source", "VARCHAR(60)"),
        ("issue_url", "VARCHAR(400)"),
        ("post_id", "INTEGER REFERENCES post_opportunities(id)"),
        ("last_message_at", "DATETIME"),
        ("last_message_author", "VARCHAR(120)"),
        ("last_message_from_me", "BOOLEAN"),
        ("unread", "BOOLEAN NOT NULL DEFAULT 0"),
        ("last_checked_at", "DATETIME"),
    ],
    "scans": [   # per-stage pipeline counts. Nullable on purpose: scans saved before this change keep NULL ("not tracked") instead of invented zeros.
        ("duplicate_count", "INTEGER"),
        ("contacted_count", "INTEGER"),
        ("too_old_count", "INTEGER"),
        ("irrelevant_count", "INTEGER"),
        ("same_author_count", "INTEGER"),
        ("over_limit_count", "INTEGER"),
        ("fresh_hours", "FLOAT"),
    ],
}
# (index name, table, column) created with IF NOT EXISTS once the column is there. On a brand-new database create_all() builds the same index from the model.
UNIQUE_INDEXES = [("ix_conversations_issue_url", "conversations", "issue_url")]

def db_file() -> Path | None:
    """Absolute path of the SQLite file this process is using, or None for a non-file / non-SQLite database."""
    if engine.dialect.name != "sqlite": return None
    name = engine.url.database
    return Path(name).resolve() if name and name != ":memory:" else None

def _backup(path: Path) -> Path:
    """Consistent one-off copy of the live database (uses SQLite's own backup API, so it is safe even if the file is mid-write). Existing backups are never touched."""
    dest = path.with_name(f"{path.name}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    src, dst = sqlite3.connect(path), sqlite3.connect(dest)
    try: src.backup(dst)
    finally: dst.close(); src.close()
    return dest

def migrate() -> None:
    """Call once at startup, BEFORE Base.metadata.create_all(). Idempotent: on an up-to-date database it only logs the file location and changes nothing.
    If columns are missing it first copies the database file to <name>.bak-<timestamp>, then adds ONLY those columns. A failure part-way is safe to retry:
    the next start re-checks and adds whatever is still missing."""
    path = db_file()
    if path is None: return
    existed = path.exists()
    log.info("Database file: %s (%s)", path, "existing" if existed else "will be created")
    if not existed: return                                   # brand-new database: create_all() builds the current schema directly
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    missing = {t: [(c, ddl) for c, ddl in cols if c not in {x["name"] for x in insp.get_columns(t)}] for t, cols in NEW_COLUMNS.items() if t in tables}
    missing = {t: cols for t, cols in missing.items() if cols}
    if missing:
        log.info("Schema upgrade needed: %s", ", ".join(f"{t}(+{', '.join(c for c, _ in cols)})" for t, cols in missing.items()))
        log.info("Backup written to %s", _backup(path))
        with engine.begin() as conn:
            for t, cols in missing.items():
                for c, ddl in cols:
                    conn.execute(text(f"ALTER TABLE {t} ADD COLUMN {c} {ddl}"))
        log.info("Schema upgrade finished")
    with engine.begin() as conn:                             # indexes: safe to repeat, and only for tables that already exist (a new table gets its index from create_all)
        for name, table, col in UNIQUE_INDEXES:
            if table in tables: conn.execute(text(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({col})"))


def init_db() -> None:
    """Runs idempotent schema migrations, builds any missing tables, and cleans up stale scan states."""
    migrate()
    Base.metadata.create_all(bind=engine)
    
    # Mark any scan stuck in 'RUNNING' from a previous crash as 'FAILED'
    with SessionLocal() as session:
        try:
            session.execute(
                text("UPDATE scans SET status = 'FAILED', error = 'server restarted' WHERE status = 'RUNNING'")
            )
            session.commit()
        except Exception:
            session.rollback()