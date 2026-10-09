from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from ..db import get_db
from ..agent.github_tracker import check_replies, TrackerError
from ..serializers import convo_out
from ..models import Conversation

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

@router.get("")
def list_conversations(unread: bool = Query(None), db: Session = Depends(get_db)):
    query = db.query(Conversation)
    if unread is not None:
        query = query.filter(Conversation.unread == unread)
    conversations = query.order_by(Conversation.updated_at.desc()).all()
    return [convo_out(c) for c in conversations]

@router.get("/unread-count")
def get_unread_count(db: Session = Depends(get_db)):
    count = db.query(Conversation).filter(Conversation.unread == True).count()
    return {"unread_count": count}

@router.get("/{conversation_id}")
def get_conversation(conversation_id: str, db: Session = Depends(get_db)):
    convo = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return convo_out(convo, include_messages=True)

@router.post("/check-replies")
def trigger_check_replies(db: Session = Depends(get_db)):
    try:
        results = check_replies(db)
        return {"status": "success", "results": results}
    except TrackerError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/{conversation_id}/read")
def mark_conversation_read(conversation_id: str, db: Session = Depends(get_db)):
    convo = db.query(Conversation).filter(Conversation.id == conversation_id).first()
    if not convo:
        raise HTTPException(status_code=404, detail="Conversation not found")
    convo.unread = False
    if convo.status == "NEW_REPLY":
        convo.status = "WAITING_FOR_ME"
    db.commit()
    return convo_out(convo)