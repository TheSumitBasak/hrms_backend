"""Document upload/download (local object-store placeholder for S3)."""
from __future__ import annotations

import shutil
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse
from jose import jwt
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_permission, write_audit
from app.core.errors import APIError
from app.core.security import decode_token
from app.crud.base import get_or_404
from app.models.auth import User
from app.models.employee import Employee, EmployeeDocument

router = APIRouter(tags=["Documents"])

UPLOAD_DIR = Path("uploads")
MAX_BYTES = 10 * 1024 * 1024


@router.post("/documents/upload", status_code=201, summary="Upload an employee document")
def upload_document(
    employee_id: uuid.UUID = Form(...),
    document_type_id: uuid.UUID = Form(...),
    expiry_date: date | None = Form(None),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_permission("employee_document.create")),
) -> dict:
    employee = get_or_404(db, Employee, employee_id, "Employee")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix
    stored_name = f"{uuid.uuid4()}{suffix}"
    destination = UPLOAD_DIR / stored_name
    size = 0
    with destination.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
        size = destination.stat().st_size
    if size > MAX_BYTES:
        destination.unlink(missing_ok=True)
        raise APIError(400, "File exceeds the 10MB limit", "VALIDATION")
    document = EmployeeDocument(
        employee_id=employee.id,
        document_type_id=document_type_id,
        file_name=file.filename or stored_name,
        file_path=str(destination),
        expiry_date=expiry_date,
        status="pending",
        uploaded_by=user.id,
        uploaded_at=datetime.now(timezone.utc),
    )
    db.add(document)
    write_audit(
        db,
        user=user,
        entity_type="employee_documents",
        entity_id=employee.id,
        action="create",
        new_values={"file_name": document.file_name},
        org_id=employee.org_id,
    )
    db.commit()
    db.refresh(document)
    return {
        "data": {"id": str(document.id), "file_name": document.file_name, "size": size},
        "meta": None,
        "errors": None,
    }


@router.get("/documents/{document_id}/download", summary="Download an employee document")
def download_document(
    document_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("employee_document.view")),
):
    document = get_or_404(db, EmployeeDocument, document_id, "Document")
    path = Path(document.file_path)
    if not path.exists():
        raise APIError(404, "Stored file not found", "NOT_FOUND")
    return FileResponse(path, filename=document.file_name)


def _sign_document(document_id: uuid.UUID, ttl_minutes: int = 5) -> tuple[str, datetime]:
    expires = datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
    token = jwt.encode(
        {"sub": str(document_id), "type": "doc", "exp": expires},
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    return token, expires


@router.post("/documents/{document_id}/signed-url", summary="Create a short-lived signed download URL")
def signed_url(
    document_id: uuid.UUID,
    ttl_minutes: int = Query(5, ge=1, le=60),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> dict:
    get_or_404(db, EmployeeDocument, document_id, "Document")
    token, expires = _sign_document(document_id, ttl_minutes)
    return {
        "data": {
            "url": f"/api/documents/download-signed?token={token}",
            "expires_at": expires.isoformat(),
            "ttl_minutes": ttl_minutes,
        },
        "meta": None,
        "errors": None,
    }


@router.get("/documents/download-signed", summary="Download a document via a signed token")
def download_signed(
    token: str = Query(...),
    db: Session = Depends(get_db),
):
    payload = decode_token(token)
    if payload is None or payload.get("type") != "doc":
        raise APIError(401, "Invalid or expired signed URL", "UNAUTHENTICATED")
    document = db.get(EmployeeDocument, uuid.UUID(payload["sub"]))
    if document is None:
        raise APIError(404, "Document not found", "NOT_FOUND")
    path = Path(document.file_path)
    if not path.exists():
        raise APIError(404, "Stored file not found", "NOT_FOUND")
    return FileResponse(path, filename=document.file_name)
