"""Templates: ready-made pipelines, copied into your workflows with "Use template"."""

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser, DbSession
from app.models.workflow import Workflow
from app.schemas.workflow import WorkflowRead
from app.services.templates import TemplateNotFound, TemplateRead, list_templates, use_template

router = APIRouter(prefix="/templates", tags=["templates"])


class UseTemplateRequest(BaseModel):
    timezone: str | None = Field(
        default=None, max_length=64, description="IANA time zone for the template's schedule (the browser's), e.g. Asia/Kolkata."
    )


@router.get(
    "",
    response_model=list[TemplateRead],
    summary="The template catalog, with the credentials each needs",
    description="`requirements[].satisfied` says whether you (or the server) have the credential; `ready` is all of them.",
)
async def get_templates(db: DbSession, user: CurrentUser) -> list[TemplateRead]:
    return await list_templates(db, user)


@router.post(
    "/{slug}/use",
    response_model=WorkflowRead,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow from a template",
    description=(
        "Copies the template into a new workflow you own and can edit. LLM steps use the free providers you have "
        "keys for (with the others as fallbacks); the template's triggers are created switched off; turn them on "
        "in the editor's Triggers panel."
    ),
    responses={404: {"description": "No such template"}},
)
async def create_from_template(slug: str, db: DbSession, user: CurrentUser, body: UseTemplateRequest | None = None) -> Workflow:
    try:
        return await use_template(db, user, slug, timezone=(body or UseTemplateRequest()).timezone)
    except TemplateNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"No template '{slug}'") from None
