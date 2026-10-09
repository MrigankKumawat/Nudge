from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..db import get_db
from ..agent.github_search import search_and_save, GitHubSearchError
from ..serializers import scan_out
from ..models import Scan

router = APIRouter(prefix="/api/scans", tags=["scans"])

@router.get("/current")
def get_current_scan_endpoint(db: Session = Depends(get_db)):
    scan = db.scalar(
        select(Scan)
        .where(Scan.status == "COMPLETED")
        .order_by(Scan.id.desc())
        .limit(1)
    )
    if not scan:
        return {"current_scan": None}
    return {"current_scan": scan_out(db, scan)}

@router.post("")
def trigger_scan(
    keyword: str = Query(...),
    limit: int = Query(30),
    db: Session = Depends(get_db)
):
    try:
        result = search_and_save(db, keyword=keyword, limit=limit)
        return {"status": "success", "scan": result}
    except GitHubSearchError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))