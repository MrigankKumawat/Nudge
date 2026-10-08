import logging, os, sqlite3
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

log = logging.getLogger(__name__)

DB_URL = os.getenv("DATABASE_URL", "sqlite:///./outreach.db")
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