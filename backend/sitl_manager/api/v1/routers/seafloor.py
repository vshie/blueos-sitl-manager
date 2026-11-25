from typing import List

from fastapi import APIRouter, HTTPException, Response, status
from fastapi_versioning import versioned_api_route

from sitl_manager import autopilot, seafloor
from sitl_manager.api.common import require_sitl, to_http_exception
from sitl_manager.models import (
    OperationResult,
    RenamePresetRequest,
    Seafloor,
    SeafloorUpload,
    SonarSettings,
    SonarState,
)

seafloor_router = APIRouter(
    prefix="/seafloor",
    tags=["seafloor"],
    route_class=versioned_api_route(1, 0),
    responses={status.HTTP_404_NOT_FOUND: {"description": "Not found"}},
)


@seafloor_router.get("/library", response_model=List[Seafloor], summary="List the stored seafloors.")
@to_http_exception
async def list_seafloors() -> List[Seafloor]:
    return seafloor.library()


@seafloor_router.post(
    "/library",
    response_model=Seafloor,
    summary="Store a seafloor built from a survey, replacing one of the same name.",
)
@to_http_exception
async def save_seafloor(upload: SeafloorUpload) -> Seafloor:
    try:
        return seafloor.save(upload)
    except ValueError as error:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(error)) from error


@seafloor_router.get(
    "/library/{name}/grid",
    summary="A seafloor's depth grid: little-endian uint16 centimetres, row by row from the south.",
    response_class=Response,
)
@to_http_exception
async def get_grid(name: str) -> Response:
    return Response(content=seafloor.grid_bytes(name), media_type="application/octet-stream")


@seafloor_router.post("/library/{name}/rename", response_model=Seafloor, summary="Rename a seafloor.")
@to_http_exception
async def rename_seafloor(name: str, request: RenamePresetRequest) -> Seafloor:
    return seafloor.rename(name, request.name)


@seafloor_router.delete("/library/{name}", response_model=OperationResult, summary="Delete a seafloor.")
@to_http_exception
async def delete_seafloor(name: str) -> OperationResult:
    return OperationResult(success=True, detail=seafloor.remove(name))


@seafloor_router.get("/sonar", response_model=SonarState, summary="The simulated sonar and what it reads now.")
@to_http_exception
async def get_sonar() -> SonarState:
    return seafloor.state()


@seafloor_router.post(
    "/sonar",
    response_model=OperationResult,
    summary="Start the simulated sonar over a seafloor, or switch it off. The first start restarts the autopilot.",
)
@to_http_exception
async def set_sonar(settings: SonarSettings) -> OperationResult:
    # Switching it off is allowed anywhere, since that is how a vehicle gets its rangefinder
    # back; sounding a seafloor only means anything in the simulator, under a boat.
    if settings.enabled:
        await require_sitl("a simulated sonar only exists in the simulator")
        aboard = await autopilot.get_vehicle_type()
        if aboard is not None and aboard != seafloor.BOAT:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"The vehicle is a {aboard}; the simulated sonar sounds a boat's survey from a boat.",
            )
    return OperationResult(success=True, detail=await seafloor.apply(settings))
