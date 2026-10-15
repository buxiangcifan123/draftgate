"""FastAPI application: REST API + embedded review UI."""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import config, db, webhooks
from ._version import __version__
from .models import Decision, ItemCreate, ItemDetail, ItemSummary, WebhookInfo

STATIC_DIR = Path(__file__).parent / "static"
PREVIEW_LENGTH = 200


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def require_token(
    authorization: Optional[str] = Header(default=None),
    x_draftgate_token: Optional[str] = Header(default=None),
) -> None:
    expected = config.get_token()
    provided = None
    if x_draftgate_token:
        provided = x_draftgate_token.strip()
    elif authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing API token")


# ---------------------------------------------------------------------------
# serializers
# ---------------------------------------------------------------------------

def _summary(item: dict) -> ItemSummary:
    preview = item["content"][:PREVIEW_LENGTH]
    if len(item["content"]) > PREVIEW_LENGTH:
        preview += "..."
    return ItemSummary(
        id=item["id"],
        title=item["title"],
        status=item["status"],
        content_type=item["content_type"],
        tags=item["tags"],
        content_preview=preview,
        created_at=item["created_at"],
        decided_at=item["decided_at"],
    )


def _detail(item: dict) -> ItemDetail:
    delivery = db.get_delivery_by_item(item["id"])
    webhook = None
    if delivery:
        webhook = WebhookInfo(
            status=delivery["status"],
            attempts=delivery["attempts"],
            last_error=delivery["last_error"],
            delivered_at=delivery["delivered_at"],
        )
    return ItemDetail(
        id=item["id"],
        title=item["title"],
        status=item["status"],
        content_type=item["content_type"],
        tags=item["tags"],
        content_preview=item["content"][:PREVIEW_LENGTH],
        created_at=item["created_at"],
        decided_at=item["decided_at"],
        content=item["content"],
        final_content=item["final_content"],
        reason=item["reason"],
        metadata=item["metadata"],
        callback_url=item["callback_url"],
        webhook=webhook,
    )


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------

def create_app(start_worker: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init_db()
        if start_worker:
            webhooks.start_worker()
        try:
            yield
        finally:
            webhooks.stop_worker()

    app = FastAPI(
        title="DraftGate",
        version=__version__,
        description="The human checkpoint for AI content pipelines.",
        lifespan=lifespan,
    )

    # The UI is same-origin; CORS is only for local browser tools.
    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "X-Draftgate-Token"],
    )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.post(
        "/api/items",
        status_code=201,
        response_model=ItemDetail,
        dependencies=[Depends(require_token)],
    )
    def create_item(body: ItemCreate) -> ItemDetail:
        item = db.create_item(
            title=body.title,
            content=body.content,
            content_type=body.content_type,
            tags=body.tags,
            metadata=body.metadata,
            callback_url=body.callback_url,
            callback_secret=body.callback_secret,
        )
        return _detail(item)

    @app.get(
        "/api/items",
        response_model=list[ItemSummary],
        dependencies=[Depends(require_token)],
    )
    def list_items(
        status: Optional[str] = Query(default=None),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[ItemSummary]:
        if status is not None and status not in (
            "pending",
            "approved",
            "rejected",
            "edited",
        ):
            raise HTTPException(status_code=422, detail=f"invalid status filter: {status}")
        items = db.list_items(status=status, limit=limit, offset=offset)
        return [_summary(i) for i in items]

    @app.get(
        "/api/items/{item_id}",
        response_model=ItemDetail,
        dependencies=[Depends(require_token)],
    )
    def get_item(item_id: int) -> ItemDetail:
        item = db.get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"item {item_id} not found")
        return _detail(item)

    @app.post(
        "/api/items/{item_id}/decision",
        response_model=ItemDetail,
        dependencies=[Depends(require_token)],
    )
    def decide(item_id: int, body: Decision) -> ItemDetail:
        final_content = body.content if body.action == "edit" else None
        reason = (body.reason or "").strip() or None
        try:
            item, delivery_id = db.decide_item(
                item_id, body.action, final_content, reason=reason
            )
        except db.NotFoundError:
            raise HTTPException(status_code=404, detail=f"item {item_id} not found")
        except db.ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if delivery_id is not None:
            webhooks.deliver_after_decision(item, delivery_id)
        # re-read so the response reflects webhook delivery state
        refreshed = db.get_item(item_id)
        return _detail(refreshed)  # type: ignore[arg-type]

    @app.post(
        "/api/items/{item_id}/webhook/retry",
        response_model=ItemDetail,
        dependencies=[Depends(require_token)],
    )
    def retry_webhook(item_id: int) -> ItemDetail:
        item = db.get_item(item_id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"item {item_id} not found")
        if not item["callback_url"]:
            raise HTTPException(status_code=409, detail="item has no callback_url")
        delivery = db.get_delivery_by_item(item_id)
        if delivery is None:
            raise HTTPException(status_code=409, detail="no delivery to retry")
        db.reset_delivery(delivery["id"])
        webhooks.deliver_after_decision(item, delivery["id"])
        refreshed = db.get_item(item_id)
        return _detail(refreshed)  # type: ignore[arg-type]

    @app.get(
        "/api/stats",
        response_model=dict,
        dependencies=[Depends(require_token)],
    )
    def stats() -> dict:
        return db.stats()

    return app


app = create_app()
