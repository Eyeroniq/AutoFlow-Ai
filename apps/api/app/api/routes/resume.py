import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from flowforge_engine import ExecutionServices
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DbSession
from app.models.resume import ResumeEmail, ResumeRefinement
from app.schemas.resume import EmailRead, RefinementCreate, RefinementRead, RefinementSummary, StageRead
from app.services.files import owned_file
from app.services.providers import get_execution_services
from app.services.resume_refinement import (
    STAGE_ORDER,
    EmailUnavailable,
    RefinementError,
    create_refinement,
    document_name,
    documents,
    email_refinement,
    owned_refinement,
)
from app.services.task_queue import EnqueueFailed, RefineQueue, get_refine_queue

router = APIRouter(prefix="/resume-refinements", tags=["resume refinement"])

RefineQueueDep = Annotated[RefineQueue, Depends(get_refine_queue)]
Services = Annotated[ExecutionServices, Depends(get_execution_services)]
_NOT_FOUND = {404: {"description": "No such refinement, or it isn't yours"}}


async def _owned(db: DbSession, user: CurrentUser, refinement_id: uuid.UUID) -> ResumeRefinement:
    refinement = await owned_refinement(db, user.id, refinement_id)
    if refinement is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Refinement not found")
    return refinement


async def _email_counts(db: DbSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = await db.execute(
        select(ResumeEmail.refinement_id, func.count()).where(ResumeEmail.refinement_id.in_(ids)).group_by(ResumeEmail.refinement_id)
    )
    return {rid: n for rid, n in rows.all()}


def _summary(row: ResumeRefinement, emails_sent: int) -> RefinementSummary:
    return RefinementSummary(
        id=row.id, filename=row.filename, version=row.version, status=row.status,
        has_job_description=bool(row.job_description.strip()), created_at=row.created_at, finished_at=row.finished_at,
        error_message=row.error_message, emails_sent=emails_sent,
    )


async def _read(db: DbSession, row: ResumeRefinement) -> RefinementRead:
    await db.refresh(row)
    emails = list(await db.scalars(select(ResumeEmail).where(ResumeEmail.refinement_id == row.id).order_by(ResumeEmail.created_at.desc())))
    stages = [StageRead.model_validate(row.stages_json[k]) for k in STAGE_ORDER if k in row.stages_json]
    return RefinementRead(
        **_summary(row, len(emails)).model_dump(), job_description=row.job_description, stages=stages, result=row.result_json,
        emails=[EmailRead.model_validate(e) for e in emails],
    )


@router.post(
    "", response_model=RefinementRead, status_code=status.HTTP_202_ACCEPTED,
    summary="Start refining a resume PDF with the five-agent pipeline",
    responses={404: {"description": "No such file"}, 415: {"description": "Not a PDF"}, 503: {"description": "The task queue is down"}},
)
async def start(body: RefinementCreate, db: DbSession, user: CurrentUser, queue: RefineQueueDep) -> RefinementRead:
    record = await owned_file(db, user.id, body.file_id)
    if record is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="File not found")
    if record.content_type != "application/pdf":
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Upload the resume as a PDF")
    refinement = await create_refinement(db, user, record, body.job_description)
    await db.commit()
    try:
        await queue.enqueue_refinement(refinement.id)
    except EnqueueFailed as exc:
        refinement.status, refinement.error_message = "failed", f"Could not queue the run: {exc}"
        await db.commit()
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail="The task queue is unavailable; try again shortly") from exc
    return await _read(db, refinement)


@router.get("", response_model=list[RefinementSummary], summary="Your refinements, newest first")
async def list_refinements(db: DbSession, user: CurrentUser) -> list[RefinementSummary]:
    rows = list(await db.scalars(
        select(ResumeRefinement).where(ResumeRefinement.owner_id == user.id).order_by(ResumeRefinement.created_at.desc(), ResumeRefinement.version.desc()).limit(50)
    ))
    counts = await _email_counts(db, [r.id for r in rows])
    return [_summary(r, counts.get(r.id, 0)) for r in rows]


@router.get("/{refinement_id}", response_model=RefinementRead, responses=_NOT_FOUND,
            summary="A refinement: every agent's stage (with raw replies) and the result")
async def get_refinement(refinement_id: uuid.UUID, db: DbSession, user: CurrentUser) -> RefinementRead:
    return await _read(db, await _owned(db, user, refinement_id))


@router.get(
    "/{refinement_id}/download", responses={**_NOT_FOUND, 409: {"description": "Not finished"}},
    summary="The refined resume as a PDF or DOCX file",
)
async def download(refinement_id: uuid.UUID, db: DbSession, user: CurrentUser, format: Literal["pdf", "docx"] = "pdf") -> Response:
    row = await _owned(db, user, refinement_id)
    try:
        data, content_type = documents(row)[format]
    except RefinementError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return Response(
        data, media_type=content_type, headers={"Content-Disposition": f'attachment; filename="{document_name(row, format)}"'}
    )


@router.post(
    "/{refinement_id}/email", response_model=EmailRead, status_code=status.HTTP_201_CREATED,
    responses={**_NOT_FOUND, 409: {"description": "Not finished"}, 424: {"description": "No email account is connected, or sending failed"}},
    summary="Email the refined resume (PDF and DOCX) to your own address",
)
async def email_to_me(refinement_id: uuid.UUID, db: DbSession, user: CurrentUser, services: Services) -> EmailRead:
    """There is no recipient field: it goes to the logged-in account's own address only."""
    row = await _owned(db, user, refinement_id)
    try:
        sent = await email_refinement(db, user, row, services)
    except RefinementError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except EmailUnavailable as exc:
        raise HTTPException(status.HTTP_424_FAILED_DEPENDENCY, detail=str(exc)) from exc
    await db.commit()
    return EmailRead.model_validate(sent)


@router.delete("/{refinement_id}", status_code=status.HTTP_204_NO_CONTENT, responses=_NOT_FOUND, summary="Delete a refinement and its history")
async def delete_refinement(refinement_id: uuid.UUID, db: DbSession, user: CurrentUser) -> Response:
    row = await _owned(db, user, refinement_id)
    await db.delete(row)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
