"""Owner-scoped feedback and separately authenticated human evaluation."""
from uuid import UUID
from fastapi import APIRouter, Depends, Request, HTTPException
from pydantic import Field
from app.api.chat import credential, execute
from app.api.annotations import reviewer
from app.repositories import quality as repo, chat
from app.quality_model import Feedback, Review, Strict
from sqlalchemy.exc import SQLAlchemyError

router = APIRouter(prefix='/api/v1/agent-chat', tags=['私有人物问答反馈与评测'])
review_router = APIRouter(prefix='/api/v1/quality-review', tags=['独立人工问答评测'])


@router.get('/quality-report', summary='查看本人回答的自动检查、反馈与人工复核进度，不调用模型')
def report(request: Request, token: str = Depends(credential)):
    return execute(request, token, repo.report)


@router.get('/conversations/{conversation_id}/answers/{run_id}/feedback')
def history(request: Request, conversation_id: UUID, run_id: UUID, token: str = Depends(credential)):
    return execute(request, token, repo.feedback_history, conversation_id, run_id)


@router.post('/conversations/{conversation_id}/answers/{run_id}/feedback')
def feedback(request: Request, conversation_id: UUID, run_id: UUID, body: Feedback, token: str = Depends(credential)):
    return execute(request, token, repo.feedback, conversation_id, run_id, body)


class Consent(Strict):
    share_with_reviewers: bool = Field(strict=True)


@router.post('/conversations/{conversation_id}/answers/{run_id}/quality-case')
def freeze(request: Request, conversation_id: UUID, run_id: UUID, body: Consent, token: str = Depends(credential)):
    if not body.share_with_reviewers:
        raise HTTPException(422, detail={'code': 'review_consent', 'message': '请确认允许已授权标注员查看本条问题、回答、相关对话片段与原文。'})
    return execute(request, token, repo.create_case, conversation_id, run_id)


@router.get('/quality-cases/{case_id}')
def case(request: Request, case_id: UUID, token: str = Depends(credential)):
    return execute(request, token, owner_case, case_id)


def owner_case(c, user, case_id):
    return repo.case(c, case_id, user)


def reviewed(request, fn, *args):
    # Reuse the established reviewer credentials, never self-declared identities.
    engine = chat.engine_for(request.app.state.settings)
    try:
        with engine.begin() as c:
            return fn(c, *args)
    except chat.Missing:
        raise HTTPException(404, detail={'code': 'review_not_found', 'message': '评测案例不存在或已保留盲测。'}) from None
    except chat.Conflict as error:
        raise HTTPException(409, detail={'code': 'review_conflict', 'message': str(error)}) from None
    except (ValueError, chat.Invalid) as error:
        raise HTTPException(422, detail={'code': 'review_invalid', 'message': str(error)}) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail={'code': 'review_unavailable', 'message': '评测暂不可用，请检查数据库迁移。'}) from None
    finally:
        engine.dispose()


@review_router.get('/{case_id}')
def read_case(request: Request, case_id: UUID, actor: str = Depends(reviewer)):
    return reviewed(request, repo.reviewer_case, actor, case_id)


@review_router.post('/{case_id}')
def submit(request: Request, case_id: UUID, body: Review, actor: str = Depends(reviewer)):
    return reviewed(request, repo.review, actor, case_id, body)
