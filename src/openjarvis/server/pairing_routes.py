"""QR pairing endpoints for scoped mobile/desktop clients."""

from __future__ import annotations

import base64
import io
from typing import Any

import qrcode
from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from openjarvis.security.pairing import PairingService


class PairingCreateRequest(BaseModel):
    server_url: str = Field(default="", max_length=512)
    scopes: list[str] | None = None
    device_name_hint: str = Field(default="", max_length=128)


class PairingConsumeRequest(BaseModel):
    pairing_id: str = Field(min_length=1, max_length=128)
    token: str = Field(min_length=20, max_length=256)
    device_name: str = Field(min_length=1, max_length=128)
    scopes: list[str] | None = None


router = APIRouter(prefix="/api/pairing", tags=["pairing"])


def get_service(request: Request) -> PairingService:
    service = getattr(request.app.state, "pairing_service", None)
    if not isinstance(service, PairingService):
        service = PairingService(server_url=str(getattr(request.app.state, "public_base_url", "")))
        request.app.state.pairing_service = service
    return service


@router.post("/create")
async def create_pairing(body: PairingCreateRequest, request: Request) -> dict[str, Any]:
    return get_service(request).create(
        server_url=body.server_url or None,
        scopes=set(body.scopes or []),
        device_name_hint=body.device_name_hint,
    )


@router.get("/{pairing_id}/qr.png")
async def pairing_qr(pairing_id: str, request: Request) -> Response:
    service = get_service(request)
    qr_data = service.qr_data_for(pairing_id)
    if qr_data is None:
        raise HTTPException(status_code=404, detail="pairing not found")
    image = qrcode.make(qr_data)
    output = io.BytesIO()
    image.save(output, format="PNG")
    return Response(content=output.getvalue(), media_type="image/png", headers={"Cache-Control": "no-store", "Content-Disposition": "inline; filename=jarvis-pairing.png"})


@router.post("/consume")
async def consume_pairing(body: PairingConsumeRequest, request: Request) -> dict[str, Any]:
    try:
        return get_service(request).consume(pairing_id=body.pairing_id, token=body.token, device_name=body.device_name, scopes=set(body.scopes or []))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/self-revoke")
async def self_revoke_device(request: Request) -> dict[str, Any]:
    """Let a paired device revoke only its own token after explicit local action."""
    device_id = str(getattr(request.state, "device_id", ""))
    if not device_id:
        raise HTTPException(status_code=403, detail="self-revoke requires a device token")
    token = request.headers.get("Authorization", "").partition(" ")[2]
    revoked = get_service(request).revoke_authenticated(token)
    if revoked is None or revoked.device_id != device_id:
        raise HTTPException(status_code=401, detail="device token is invalid or already revoked")
    return {"device_id": revoked.device_id, "revoked": True}


@router.get("/devices")
async def list_devices(request: Request) -> dict[str, Any]:
    return {"devices": get_service(request).list_devices()}


@router.delete("/devices/{device_id}")
async def revoke_device(device_id: str, request: Request) -> dict[str, Any]:
    if not get_service(request).revoke(device_id):
        raise HTTPException(status_code=404, detail="device not found")
    return {"device_id": device_id, "revoked": True}


__all__ = ["router"]
