"""Safe failure projection and durable audit for standard OpenAPI calls."""
import uuid

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from loguru import logger

from app.database import async_session
from app.services.openapi_applications import audit
from app.services.openapi_oauth import OAuthFailure


class OpenAPIRoute(APIRoute):
    def get_route_handler(self):
        original = super().get_route_handler()

        async def handler(request):
            request_id = str(uuid.uuid4())
            request.state.openapi_request_id = request_id
            code = "success"
            status = 200
            try:
                response = await original(request)
                response.headers["X-Request-ID"] = request_id
                response.headers["Cache-Control"] = "no-store"
                status = response.status_code
                return response
            except OAuthFailure as exc:
                code, status = exc.error, exc.status
                response = exc.response()
                response.headers["X-Request-ID"] = request_id
                return response
            except RequestValidationError as exc:
                code, status = "invalid_request", 422
                if self.path.endswith("/{employee_id}/access") and any(
                    len(error["loc"]) > 1
                    and error["loc"][1] in {"instance_ref", "interaction", "embed_origin", "scene_key"}
                    for error in exc.errors()
                ):
                    code, status = "invalid_interaction", 400
                raise HTTPException(status, detail={"code": code, "message": code},
                                    headers={"Cache-Control": "no-store", "X-Request-ID": request_id})
            except HTTPException as exc:
                status = exc.status_code
                code = exc.detail.get("code", "request_denied") if isinstance(exc.detail, dict) else "request_denied"
                exc.headers = {**(exc.headers or {}), "Cache-Control": "no-store", "X-Request-ID": request_id}
                raise
            except Exception:
                code, status = "service_unavailable", 503
                # Do not include exception strings: provider/DB exceptions may
                # contain credential-bearing request values.
                logger.error("OpenAPI request failed request_id={}", request_id)
                raise HTTPException(status, detail={"code": code, "message": code},
                                    headers={"Cache-Control": "no-store", "X-Request-ID": request_id})
            finally:
                async with async_session() as db:
                    await audit(db, f"request.{request.method.lower()}",
                                application_id=getattr(request.state, "openapi_application_id", None),
                                user_id=getattr(request.state, "openapi_user_id", None),
                                request_id=request_id, outcome=f"{status}:{code}", operation=self.path)
                    await db.commit()

        return handler
