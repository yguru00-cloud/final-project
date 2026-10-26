"""Cart Commute: the New Haven grocery trip planner.

Run it with:  python app.py   then open http://localhost:8000
FastAPI serves both the API (/api/...) and the web page (static/), so there is only one thing to start.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from backend import audit, data_sources as ds
from pydantic import BaseModel, Field

from backend.agents import Planner, PlanError, chat, set_quantity, set_version, swap_product
from backend.config import STATIC_DIR
from backend.models import ChatRequest, PlanRequest
from backend.safety import BudgetExceeded, RateLimiter

app = FastAPI(title="Cart Commute")
limiter = RateLimiter()


@app.middleware("http")
async def no_stale_assets(request: Request, call_next):
    """Always serve the current page files, so an update never shows a half-old page."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "has_key": bool(os.environ.get("PORTKEY_API_KEY")), "stores": len(ds.STORES),
            "well_compared": ds.coverage()}


@app.get("/api/stores")
def stores() -> dict:
    return {"stores": ds.STORES}


@app.post("/api/plan")
async def plan(req: PlanRequest, request: Request) -> StreamingResponse:
    """Stream the run as newline-delimited JSON events, ending with a 'result' or 'error' event."""
    if not os.environ.get("PORTKEY_API_KEY"):
        raise HTTPException(503, "PORTKEY_API_KEY is not set. Put it in .env in this folder or the folder above.")
    if not limiter.allow(_client(request)):
        raise HTTPException(429, "Too many plans in the last hour. Please wait a bit and try again.")

    queue: asyncio.Queue = asyncio.Queue()

    async def emit(event: dict) -> None:
        await queue.put(event)

    async def worker() -> None:
        planner = Planner(req, emit)
        try:
            result = await planner.run()
            await queue.put({"type": "result", "plan": result})
        except PlanError as exc:
            await queue.put({"type": "error", "message": str(exc)})
        except BudgetExceeded as exc:
            audit.log(planner.run_id, "guard", check="budget", error=str(exc))
            await queue.put({"type": "error", "message": str(exc)})
        except Exception as exc:  # keep the page alive and say what happened
            audit.log(planner.run_id, "error", error=f"{type(exc).__name__}: {exc}")
            await queue.put({"type": "error", "message": f"Something went wrong ({type(exc).__name__}). Please try again."})
        finally:
            await queue.put(None)

    async def stream():
        task = asyncio.create_task(worker())
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        finally:
            task.cancel()

    return StreamingResponse(stream(), media_type="application/x-ndjson")


class SwapRequest(BaseModel):
    item_key: str = Field(max_length=60)
    store_brand: str = Field(max_length=60)
    offer_id: str = Field(max_length=80)


@app.post("/api/plan/{plan_id}/swap")
def swap(plan_id: str, req: SwapRequest) -> JSONResponse:
    """Use another matching product for one item; every option is re-ranked by code (no model call)."""
    try:
        return JSONResponse(json.loads(json.dumps(swap_product(plan_id, req.item_key, req.store_brand, req.offer_id), default=str)))
    except PlanError as exc:
        raise HTTPException(404, str(exc))


class QuantityRequest(BaseModel):
    item_key: str = Field(max_length=60)
    qty: float = Field(gt=0, le=500)


@app.post("/api/plan/{plan_id}/quantity")
def quantity(plan_id: str, req: QuantityRequest) -> JSONResponse:
    """Change one item's amount; every store is re-priced for it by code (no model call)."""
    try:
        return JSONResponse(json.loads(json.dumps(set_quantity(plan_id, req.item_key, req.qty), default=str)))
    except PlanError as exc:
        raise HTTPException(404, str(exc))


class VersionRequest(BaseModel):
    item_key: str = Field(max_length=60)
    offer_id: str | None = Field(default=None, max_length=80)  # None = back to the regular product


@app.post("/api/plan/{plan_id}/version")
def version(plan_id: str, req: VersionRequest) -> JSONResponse:
    """Buy one specific version of an item (e.g. lactose-free milk); re-ranked by code (no model call)."""
    try:
        return JSONResponse(json.loads(json.dumps(set_version(plan_id, req.item_key, req.offer_id), default=str)))
    except PlanError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/chat")
async def chat_route(req: ChatRequest, request: Request) -> JSONResponse:
    if not limiter.allow(_client(request)):
        raise HTTPException(429, "Too many messages in the last hour. Please wait a bit.")
    try:
        return JSONResponse(await chat(req.plan_id, req.message, req.history))
    except PlanError as exc:
        raise HTTPException(404, str(exc))
    except BudgetExceeded as exc:
        raise HTTPException(402, str(exc))
    except Exception as exc:  # never send a raw stack trace to the page
        audit.log(req.plan_id, "error", where="chat", error=f"{type(exc).__name__}: {exc}"[:300])
        raise HTTPException(500, "The helper hit a problem. Please try again.")


@app.get("/api/audit")
def audit_trail(run_id: str | None = None, limit: int = 100) -> dict:
    return {"events": audit.recent(min(limit, 500), run_id)}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  Cart Commute is running: open http://localhost:{port}\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
