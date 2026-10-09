from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from app.db import get_db
from app.models import Activity
from app.serializers import activity_out

router = APIRouter(prefix="/api/activity", tags=["activity"])

@router.get("")
def get_activity_feed(limit: int = 50, db: Session = Depends(get_db)):
    """
    Retrieves the activity timeline ordered from newest to oldest.
    Covers comment detection, replies, mentions, and follow-ups.
    """
    activities = (
        db.query(Activity)
        .order_by(Activity.timestamp.desc())
        .limit(limit)
        .all()
    )
    return [activity_out(a) for a in activities]