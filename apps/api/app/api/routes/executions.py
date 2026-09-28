import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.schemas.execution import ExecutionDetail
from app.services.runs import load_execution_detail

router = APIRouter(prefix="/executions", tags=["executions"])


@router.get(
    "/{execution_id}",
    response_model=ExecutionDetail,
    summary="Get one execution with every node's result",
    responses={404: {"description": "Execution not found (or not yours)"}},
)
async def get_execution(execution_id: uuid.UUID, db: DbSession, user: CurrentUser) -> ExecutionDetail:
    detail = await load_execution_detail(db, execution_id, user)
    if detail is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return detail
