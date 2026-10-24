"""Generic REST router factory.

Each SQLAlchemy model registered in :mod:`app.api.registry` gets a full set of
standard endpoints (list, get, create, patch, delete, bulk, export) with
validation, RBAC, audit logging and the standard response envelope.
"""
# NOTE: intentionally NO ``from __future__ import annotations`` here so that
# dynamically generated Pydantic schemas can be used as eager annotations.
import csv
import io
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.deps import (
    Pagination,
    client_ip,
    get_current_user,
    get_pagination,
    require_permission,
    write_audit,
)
from app.core.errors import APIError
from app.crud import base as crud
from app.models.auth import User
from app.schemas.common import BulkCreate, BulkIds, BulkUpdate


def _envelope(data: Any, meta: dict | None = None) -> dict:
    return {"data": data, "meta": meta, "errors": None}


def _page_meta(pagination: Pagination, total: int) -> dict:
    total_pages = (total + pagination.page_size - 1) // pagination.page_size if pagination.page_size else 0
    return {
        "page": pagination.page,
        "pageSize": pagination.page_size,
        "total": total,
        "totalPages": total_pages,
    }


def build_resource_router(
    *,
    slug: str,
    model: type,
    tag: str,
    read_schema: type,
    create_schema: type | None = None,
    update_schema: type | None = None,
    search_fields: tuple[str, ...] = (),
    default_sort: tuple[str, ...] = (),
    permission: str | None = None,
    audited: bool = False,
    read_only: bool = False,
) -> APIRouter:
    router = APIRouter(prefix=f"/{slug}", tags=[tag])

    view_dep = require_permission(f"{permission}.view") if permission else get_current_user
    create_dep = require_permission(f"{permission}.create") if permission else get_current_user
    edit_dep = require_permission(f"{permission}.edit") if permission else get_current_user
    delete_dep = require_permission(f"{permission}.delete") if permission else get_current_user

    # ---------------------------------------------------------------- list
    @router.get("", summary=f"List {slug}")
    def list_items(
        pagination: Pagination = Depends(get_pagination),
        db: Session = Depends(get_db),
        user: User = Depends(view_dep),
    ) -> dict:
        stmt = crud.build_query(
            model, pagination, search_fields=search_fields, default_sort=default_sort
        )
        rows, total = crud.paginate(db, stmt, pagination)
        return _envelope(
            [read_schema.model_validate(row) for row in rows],
            _page_meta(pagination, total),
        )

    # -------------------------------------------------------------- create
    if not read_only and create_schema is not None:

        @router.post("", status_code=201, summary=f"Create {slug}")
        def create_item(
            payload: create_schema,
            request: Request,
            db: Session = Depends(get_db),
            user: User = Depends(create_dep),
        ) -> dict:
            data = payload.model_dump(exclude_unset=True)
            column_names = {column.name for column in model.__table__.columns}
            if "created_by" in column_names and not data.get("created_by"):
                data["created_by"] = user.id
            obj = crud.create(db, model, data)
            if audited:
                write_audit(
                    db,
                    user=user,
                    entity_type=slug,
                    entity_id=obj.id,
                    action="create",
                    new_values=crud.to_dict(obj),
                    ip_address=client_ip(request),
                    org_id=getattr(obj, "org_id", None),
                )
                db.commit()
            return _envelope(read_schema.model_validate(obj))

        # ------------------------------------------------------- bulk create
        @router.post("/bulk", status_code=201, summary=f"Bulk create {slug}")
        def bulk_create_items(
            payload: BulkCreate,
            request: Request,
            db: Session = Depends(get_db),
            user: User = Depends(create_dep),
        ) -> dict:
            valid: list[dict] = []
            errors: list[dict] = []
            for index, raw in enumerate(payload.items):
                try:
                    valid.append(create_schema.model_validate(raw).model_dump(exclude_unset=True))
                except Exception as exc:  # noqa: BLE001
                    errors.append({"code": "VALIDATION", "field": f"items[{index}]", "message": str(exc)})
            created = crud.bulk_create(db, model, valid) if valid else []
            if audited:
                for obj in created:
                    write_audit(
                        db,
                        user=user,
                        entity_type=slug,
                        entity_id=obj.id,
                        action="create",
                        new_values=crud.to_dict(obj),
                        ip_address=client_ip(request),
                        org_id=getattr(obj, "org_id", None),
                    )
                db.commit()
            meta = {"created": len(created), "failed": len(errors)}
            return {"data": [read_schema.model_validate(obj) for obj in created], "meta": meta, "errors": errors or None}

    # --------------------------------------------------------- bulk delete
    if not read_only:

        @router.post("/bulk-delete", summary=f"Bulk delete {slug}")
        def bulk_delete_items(
            payload: BulkIds,
            db: Session = Depends(get_db),
            user: User = Depends(delete_dep),
        ) -> dict:
            deleted = 0
            for item_id in payload.ids:
                try:
                    key = uuid.UUID(str(item_id))
                except (ValueError, TypeError):
                    continue
                obj = db.get(model, key)
                if obj is not None:
                    db.delete(obj)
                    deleted += 1
            db.commit()
            return _envelope({"deleted": deleted})

    # --------------------------------------------------------- bulk update
    if not read_only:

        @router.post("/bulk-update", summary=f"Bulk update {slug}")
        def bulk_update_items(
            payload: BulkUpdate,
            db: Session = Depends(get_db),
            user: User = Depends(edit_dep),
        ) -> dict:
            column_names = {column.name for column in model.__table__.columns}
            data = {k: v for k, v in payload.data.items() if k in column_names}
            updated = 0
            for raw in payload.ids:
                try:
                    key = uuid.UUID(str(raw))
                except (ValueError, TypeError):
                    continue
                obj = db.get(model, key)
                if obj is None:
                    continue
                for field, value in data.items():
                    setattr(obj, field, value)
                db.add(obj)
                updated += 1
            db.commit()
            return _envelope({"updated": updated, "fields": sorted(data)})

    # ---------------------------------------------------------------- export
    @router.get("/export", summary=f"Export {slug}")
    def export_items(
        request: Request,
        format: str = Query("csv", pattern="^(csv|json)$"),
        pagination: Pagination = Depends(get_pagination),
        db: Session = Depends(get_db),
        user: User = Depends(view_dep),
    ) -> Response:
        pagination.page = 1
        pagination.page_size = 10000
        stmt = crud.build_query(
            model, pagination, search_fields=search_fields, default_sort=default_sort
        )
        rows = db.execute(stmt).scalars().all()
        if format == "json":
            import json

            body = json.dumps([crud.to_dict(row) for row in rows], default=str)
            return Response(content=body, media_type="application/json")
        columns = [column.name for column in model.__table__.columns]
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: crud.jsonable(getattr(row, column)) for column in columns})
        return Response(
            content=buffer.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'},
        )

    # ------------------------------------------------------------------ get
    @router.get("/{item_id}", summary=f"Get {slug} by id")
    def get_item(
        item_id: uuid.UUID,
        db: Session = Depends(get_db),
        user: User = Depends(view_dep),
    ) -> dict:
        obj = crud.get_or_404(db, model, item_id)
        return _envelope(read_schema.model_validate(obj))

    # --------------------------------------------------------------- update
    if not read_only and update_schema is not None:

        @router.patch("/{item_id}", summary=f"Update {slug}")
        def update_item(
            item_id: uuid.UUID,
            payload: update_schema,
            request: Request,
            db: Session = Depends(get_db),
            user: User = Depends(edit_dep),
        ) -> dict:
            obj = crud.get_or_404(db, model, item_id)
            old_values = crud.to_dict(obj) if audited else None
            data = payload.model_dump(exclude_unset=True)
            obj = crud.update(db, obj, data)
            if audited:
                write_audit(
                    db,
                    user=user,
                    entity_type=slug,
                    entity_id=obj.id,
                    action="update",
                    old_values=old_values,
                    new_values=crud.to_dict(obj),
                    ip_address=client_ip(request),
                    org_id=getattr(obj, "org_id", None),
                )
                db.commit()
            return _envelope(read_schema.model_validate(obj))

    # --------------------------------------------------------------- delete
    if not read_only:

        @router.delete("/{item_id}", status_code=200, summary=f"Delete {slug}")
        def delete_item(
            item_id: uuid.UUID,
            request: Request,
            db: Session = Depends(get_db),
            user: User = Depends(delete_dep),
        ) -> dict:
            obj = crud.get_or_404(db, model, item_id)
            old_values = crud.to_dict(obj) if audited else None
            if audited:
                write_audit(
                    db,
                    user=user,
                    entity_type=slug,
                    entity_id=obj.id,
                    action="delete",
                    old_values=old_values,
                    ip_address=client_ip(request),
                    org_id=getattr(obj, "org_id", None),
                )
            crud.delete(db, obj)
            return _envelope({"deleted": True})

    return router
