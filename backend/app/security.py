from fastapi import Header, HTTPException, Request

from . import config


async def require_extension_origin(request: Request, x_extension_secret: str | None = Header(default=None)) -> None:
    """Reject requests that don't come from the trusted extension.

    Binding to 127.0.0.1 alone does not stop a malicious webpage's JS from
    calling a localhost port (see PLAN.md 3.2) — so every mutating endpoint
    must go through this check.
    """
    if config.SHARED_SECRET:
        if x_extension_secret != config.SHARED_SECRET:
            raise HTTPException(status_code=403, detail="missing or invalid X-Extension-Secret header")
        return

    if config.ALLOWED_ORIGIN:
        origin = request.headers.get("origin", "")
        if origin != config.ALLOWED_ORIGIN:
            raise HTTPException(status_code=403, detail=f"origin '{origin}' not allowed")
        return

    # Neither configured: refuse to run wide open. Local dev must set one.
    raise HTTPException(
        status_code=500,
        detail="server misconfigured: set ALLOWED_ORIGIN or SHARED_SECRET in backend/.env",
    )
