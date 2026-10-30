"""Document categories, employee documents and signed local storage routes (§24.4.5)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.deps import (
    CurrentOrg,
    DbSession,
    IfMatch,
    get_document_service,
    get_local_storage,
    get_onboarding_service,
    get_url_signer,
)
from app.core.context import OrgContext
from app.core.errors import FileTooLarge, ValidationFailed
from app.core.masking import mask_document_number
from app.db.session import unit_of_work
from app.models.document import EmployeeDocument
from app.schemas.common import etag_for
from app.schemas.document import (
    DocumentArchiveRequest,
    DocumentCategoryCreateRequest,
    DocumentCategoryResponse,
    DocumentCategoryUpdateRequest,
    DocumentInitiateRequest,
    DocumentResponse,
    DocumentUploadResponse,
    DocumentVerifyRequest,
    DownloadUrlResponse,
)
from app.services.documents import DocumentService
from app.services.onboarding import OnboardingService
from app.services.privacy import decrypt_export
from app.storage.local import LocalStorage, SignedUrlSigner

router = APIRouter(prefix="/orgs/{org_id}", tags=["Documents"])
storage_router = APIRouter(prefix="/storage", tags=["Document storage"])

MAX_STORAGE_UPLOAD_BYTES = 100 * 1024 * 1024


def _category_response(row: Any, mimes: list[str]) -> DocumentCategoryResponse:
    payload = DocumentCategoryResponse.model_validate(row)
    payload.mime_types = mimes
    return payload


def _document_response(
    service: DocumentService, context: OrgContext, row: EmployeeDocument
) -> DocumentResponse:
    payload = DocumentResponse.model_validate(row)
    payload.masked_document_number = (
        None
        if row.document_number_last_four is None
        else mask_document_number(row.document_number_last_four)
    )
    payload.expiry_state = service.expiry_state(row, service.expiry_warning_days(context))
    return payload


@router.get(
    "/document-categories",
    response_model=list[DocumentCategoryResponse],
    summary="List document categories",
)
def list_categories(
    context: CurrentOrg,
    service: Annotated[DocumentService, Depends(get_document_service)],
    include_inactive: bool = False,
) -> list[DocumentCategoryResponse]:
    return [
        _category_response(row, mimes)
        for row, mimes in service.list_categories(context, include_inactive=include_inactive)
    ]


@router.post(
    "/document-categories",
    response_model=DocumentCategoryResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a document category",
)
def create_category(
    payload: DocumentCategoryCreateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentCategoryResponse:
    with unit_of_work(db):
        row, mimes = service.create_category(context, payload.model_dump())
        return _category_response(row, mimes)


@router.patch(
    "/document-categories/{category_id}",
    response_model=DocumentCategoryResponse,
    summary="Edit a document category",
)
def update_category(
    category_id: uuid.UUID,
    payload: DocumentCategoryUpdateRequest,
    response: Response,
    context: CurrentOrg,
    if_match: IfMatch,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentCategoryResponse:
    with unit_of_work(db):
        row, mimes = service.update_category(
            context,
            category_id,
            payload.model_dump(exclude_unset=True),
            if_match=if_match,
        )
        result = _category_response(row, mimes)
    response.headers["ETag"] = etag_for(row.row_version)
    return result


@router.post(
    "/employees/{employee_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Initiate a document upload",
)
def initiate_document(
    employee_id: uuid.UUID,
    payload: DocumentInitiateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentUploadResponse:
    with unit_of_work(db):
        row, url, expires = service.initiate_upload(context, employee_id, payload.model_dump())
        return DocumentUploadResponse(
            **_document_response(service, context, row).model_dump(),
            upload_url=url,
            upload_expires_at=expires,
        )


@router.post(
    "/employees/{employee_id}/documents/{document_id}/complete",
    response_model=DocumentResponse,
    summary="Complete a document upload",
)
def complete_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    with unit_of_work(db):
        row = service.complete_upload(context, employee_id, document_id)
        return _document_response(service, context, row)


@router.get(
    "/employees/{employee_id}/documents",
    response_model=list[DocumentResponse],
    summary="List employee documents",
)
def list_documents(
    employee_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[DocumentService, Depends(get_document_service)],
    category_id: uuid.UUID | None = None,
    verification_status: str | None = None,
    include_archived: bool = False,
    include_versions: bool = False,
) -> list[DocumentResponse]:
    return [
        _document_response(service, context, row)
        for row in service.list_documents(
            context,
            employee_id,
            category_id=category_id,
            verification_status=verification_status,
            include_archived=include_archived,
            include_versions=include_versions,
        )
    ]


@router.get(
    "/employees/{employee_id}/documents/{document_id}",
    response_model=DocumentResponse,
    summary="Read a document",
)
def read_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    context: CurrentOrg,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    return _document_response(
        service, context, service.get_document(context, employee_id, document_id)
    )


@router.post(
    "/employees/{employee_id}/documents/{document_id}/download-url",
    response_model=DownloadUrlResponse,
    summary="Create a time-limited download URL",
)
def download_url(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DownloadUrlResponse:
    with unit_of_work(db):
        url, expires = service.download_url(context, employee_id, document_id)
    return DownloadUrlResponse(url=url, expires_at=expires)


@router.post(
    "/employees/{employee_id}/documents/{document_id}/versions",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Replace a document with a new version",
)
def replace_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: DocumentInitiateRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentUploadResponse:
    with unit_of_work(db):
        row, url, expires = service.initiate_upload(
            context, employee_id, payload.model_dump(), replaces=document_id
        )
        return DocumentUploadResponse(
            **_document_response(service, context, row).model_dump(),
            upload_url=url,
            upload_expires_at=expires,
        )


@router.post(
    "/employees/{employee_id}/documents/{document_id}/verify",
    response_model=DocumentResponse,
    summary="Verify or reject a document",
)
def verify_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: DocumentVerifyRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
    onboarding: Annotated[OnboardingService, Depends(get_onboarding_service)],
) -> DocumentResponse:
    with unit_of_work(db):
        row = service.verify(
            context,
            employee_id,
            document_id,
            decision=payload.decision,
            rejection_reason=payload.rejection_reason,
        )
        onboarding.on_document_verified(
            context,
            document_id,
            decision=payload.decision,
            rejection_reason=payload.rejection_reason,
        )
        return _document_response(service, context, row)


@router.post(
    "/employees/{employee_id}/documents/{document_id}/archive",
    response_model=DocumentResponse,
    summary="Archive a document",
)
def archive_document(
    employee_id: uuid.UUID,
    document_id: uuid.UUID,
    payload: DocumentArchiveRequest,
    context: CurrentOrg,
    db: DbSession,
    service: Annotated[DocumentService, Depends(get_document_service)],
) -> DocumentResponse:
    with unit_of_work(db):
        row = service.archive(context, employee_id, document_id, payload.reason)
        return _document_response(service, context, row)


@router.get(
    "/documents/expiring",
    response_model=list[DocumentResponse],
    summary="List documents nearing expiry",
)
def expiring_documents(
    context: CurrentOrg,
    service: Annotated[DocumentService, Depends(get_document_service)],
    within_days: int = 30,
) -> list[DocumentResponse]:
    rows = service.expiring(context, within_days=within_days)
    return [_document_response(service, context, row) for row in rows]


@storage_router.put("/upload/{token}", status_code=status.HTTP_204_NO_CONTENT)
async def storage_upload(
    token: str,
    request: Request,
    storage: Annotated[LocalStorage, Depends(get_local_storage)],
    signer: Annotated[SignedUrlSigner, Depends(get_url_signer)],
) -> Response:
    try:
        payload = signer.verify(token, purpose="upload")
    except ValueError as error:
        raise ValidationFailed(str(error)) from error
    body = await request.body()
    if len(body) > MAX_STORAGE_UPLOAD_BYTES:
        raise FileTooLarge()
    storage.put(str(payload["k"]), body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@storage_router.get("/export/{token}")
def storage_export(
    token: str,
    request: Request,
    storage: Annotated[LocalStorage, Depends(get_local_storage)],
    signer: Annotated[SignedUrlSigner, Depends(get_url_signer)],
) -> Response:
    password = request.headers.get("X-Export-OTP")
    if not password:
        raise ValidationFailed("The export one-time password is required.")
    try:
        payload = signer.verify(token, purpose="export")
        data = decrypt_export(storage.get(str(payload["k"])), password)
    except ValueError as error:
        raise ValidationFailed(str(error)) from error
    return Response(content=data, media_type="application/json")


@storage_router.get("/download/{token}")
def storage_download(
    token: str,
    storage: Annotated[LocalStorage, Depends(get_local_storage)],
    signer: Annotated[SignedUrlSigner, Depends(get_url_signer)],
) -> Response:
    try:
        payload = signer.verify(token, purpose="download")
    except ValueError as error:
        raise ValidationFailed(str(error)) from error
    data = storage.get(str(payload["k"]))
    return Response(content=data, media_type="application/octet-stream")


__all__ = ["router", "storage_router"]
