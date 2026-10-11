import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ..db import get_db
from ..agent.github_tracker import check_replies, mark_read, TrackerError
from .. import serializers as S
from ..models import Conversation

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/conversations", tags=["conversations"])

# These routes only read Nudge's own database, plus the existing read-only reply check. They never post, reply or react on GitHub.
# Every response goes through serializers.convo_out(db, convo), the same shape the UI already receives from /api/bootstrap.

def _get_or_404(db: Session, conversation_id: int) -> Conversation:
    convo = db.get(Conversation, conversation_id)
    if convo is None: raise HTTPException(status_code=404, detail="Conversation not found")
    return convo

@router.get("")
def list_conversations(unread: bool | None = Query(None), db: Session = Depends(get_db)):
    """All conversations, unread GitHub ones first, then newest activity. ?unread=true is the Unread tab, ?unread=false the rest."""
    return S.convos(db, unread)

@router.get("/unread-count")
def get_unread_count(db: Session = Depends(get_db)):
    return {"unread_count": S.unread_count(db)}

@router.get("/{conversation_id}")
def get_conversation(conversation_id: int, db: Session = Depends(get_db)):
    """One conversation. A GitHub one carries its full comment timeline in `messages`."""
    return S.convo_out(db, _get_or_404(db, conversation_id))

@router.post("/check-replies")
def trigger_check_replies(db: Session = Depends(get_db)):
    try:
        results = check_replies(db)
        return {"status": "success", "results": results}
    except TrackerError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception:
        db.rollback()
        log.exception("Reply check failed")   # the traceback stays in the server log; the client gets a plain message
        raise HTTPException(status_code=500, detail="The reply check failed. See the Nudge server log for details.")

@router.post("/{conversation_id}/read")
def mark_conversation_read(conversation_id: int, db: Session = Depends(get_db)):
    """Mark a conversation as read: clears `unread` and moves NEW_REPLY to WAITING_FOR_ME (same rule the reply tracker uses)."""
    return S.convo_out(db, mark_read(db, _get_or_404(db, conversation_id)))