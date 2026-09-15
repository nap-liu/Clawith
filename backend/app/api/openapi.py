"""OAuth-protected standard employee discovery and temporary login services."""
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.openapi_boundary import OpenAPIRoute
from app.core.events import get_redis
from app.core.permissions import build_visible_agents_query, check_agent_access
from app.core.security import create_access_token, set_access_token_cookie
from app.database import get_db
from app.services.agent_login import exchange_agent_login
from app.services.openapi_login import AGENT_AUDIENCE
from app.models.agent import Agent
from app.models.openapi_application import OpenAPICredential
from app.schemas.openapi_application import LoginLinkInput, LoginExchangeInput, EmployeeSearchInput, EmployeeAccessInput
from app.schemas.schemas import UserOut
from app.schemas.openapi_application import EmployeeAccessOut, EmployeePageOut, OAuthTokenOut, LoginLinkOut, CapabilitiesOut
from app.schemas.openapi_application import EmployeeScenesOut, EmployeeUserInput, LoginExchangeOut
from app.services.openapi_applications import (
    audit, authenticate_client, credential, delegated_user, digest, login_user, fail, issue_system_token, now,
)
from app.services.openapi_login import issue_login_code, redirect_target, verify_login_code
from app.services.openapi_interactions import activate_launcher, cleanup_pending_interactions, prepare_interaction
from app.services.openapi_scenes import available_scenes, require_available_scene
from app.services.openapi_host_context import host_context_bootstrap
from app.services.turn_inbox import schedule_durable_turn_resume
from app.services.platform_service import platform_service
from app.services.openapi_oauth import (
    OAuthFailure, SystemContext, client_credentials_request, client_request, client_basic, oauth2, require_scope,
)

router = APIRouter(prefix="/openapi/v1", tags=["OpenAPI v1"], route_class=OpenAPIRoute)


def employee_projection(agent, public_base_url=""):
    return {"id": str(agent.id), "name": agent.name, "avatar_url": agent.avatar_url,
            "description": agent.role_description or "",
            "access_url": f"{public_base_url.rstrip('/')}/h5/agents/{agent.id}/chat"}


async def rate_limit(app):
    redis = await get_redis()
    key = f"openapi:rate:{app.id}:{int(now().timestamp()) // 60}"
    count = await redis.incr(key)
    if count == 1:
        await redis.expire(key, 120)
    if count > app.rate_limit_per_minute:
        fail("rate_limited", 429)


async def system_context(request: Request, authorization=Depends(oauth2), db: AsyncSession = Depends(get_db)):
    if not authorization:
        raise OAuthFailure(None, 401)
    if not authorization.lower().startswith("bearer "):
        raise OAuthFailure("invalid_token", 401)
    try:
        app, value = await credential(db, authorization[7:], "system")
    except HTTPException:
        raise OAuthFailure("invalid_token", 401)
    request.state.openapi_application_id = app.id
    await rate_limit(app)
    return SystemContext(app, set(value.scopes) & set(app.scopes))


async def resolve_user(request, db, context, claim):
    user = await delegated_user(db, context.application, claim)
    request.state.openapi_user_id = user.id
    return user


@router.post("/auth/token", response_model=OAuthTokenOut, openapi_extra={"requestBody": {"required": True, "content": {
    "application/x-www-form-urlencoded": {"schema": {"type": "object", "required": ["grant_type"],
        "properties": {"grant_type": {"type": "string", "enum": ["client_credentials"]},
                       "scope": {"type": "string"}}}}}},
    "security": [{"ClientSecretBasic": []}]})
async def token(request: Request, response: Response, basic=Depends(client_basic), db: AsyncSession = Depends(get_db)):
    client_id, secret, requested_scope = await client_credentials_request(request)
    try:
        client_id = uuid.UUID(client_id)
        app = await authenticate_client(db, client_id, secret)
    except (ValueError, HTTPException):
        raise OAuthFailure("invalid_client", 401, basic=True)
    request.state.openapi_application_id = app.id
    await rate_limit(app)
    scopes = set(app.scopes) if requested_scope is None else requested_scope
    if not scopes <= set(app.scopes):
        raise OAuthFailure("invalid_scope")
    await cleanup_pending_interactions(db)
    access_token = await issue_system_token(db, app, sorted(scopes))
    await db.commit()
    response.headers["Pragma"] = "no-cache"
    return {"access_token": access_token, "token_type": "Bearer", "expires_in": 300,
            "scope": " ".join(sorted(scopes))}


@router.post("/auth/revoke", status_code=200, openapi_extra={"requestBody": {"required": True, "content": {
    "application/x-www-form-urlencoded": {"schema": {"type": "object", "required": ["token"],
        "properties": {"token": {"type": "string"}, "token_type_hint": {"type": "string"}}}}}}})
async def revoke_token(request: Request, basic=Depends(client_basic), db: AsyncSession = Depends(get_db)):
    client_id, secret, form = await client_request(request, revoke=True)
    try:
        app = await authenticate_client(db, uuid.UUID(client_id), secret)
    except (ValueError, HTTPException):
        raise OAuthFailure("invalid_client", 401, basic=True)
    request.state.openapi_application_id = app.id
    value = await db.get(OpenAPICredential, digest(form["token"][0]))
    if value and value.application_id == app.id and value.kind == "system":
        value.consumed_at = now()
    await db.commit()
    return Response(status_code=200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/capabilities", response_model=CapabilitiesOut)
async def capabilities(context=Depends(system_context)):
    return {"protocol_version": 1, "scopes": sorted(context.scopes),
            "trust_user_identity": context.application.trust_user_identity}


@router.post("/digital-employees/search", response_model=EmployeePageOut)
async def employees(body: EmployeeSearchInput, request: Request,
                    context=Security(system_context, scopes=["employees:read"]), db: AsyncSession = Depends(get_db)):
    require_scope(context, "employees:read")
    user = await resolve_user(request, db, context, body.user)
    stmt = build_visible_agents_query(user, tenant_id=context.application.tenant_id)
    if body.search:
        stmt = stmt.where(Agent.name.ilike(f"%{body.search}%"))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await db.execute(stmt.order_by(Agent.name, Agent.id)
                             .offset((body.page - 1) * body.page_size).limit(body.page_size))).scalars()
    base = await platform_service.get_configured_public_base_url(db)
    result = {"items": [employee_projection(agent, base) for agent in rows], "total": total,
              "page": body.page, "page_size": body.page_size, "has_more": body.page * body.page_size < total}
    await db.commit()
    return result


@router.post("/digital-employees/{employee_id}/scenes/search", response_model=EmployeeScenesOut,
             description="List this employee's enabled published scenes using the public H5 manifest.")
async def employee_scenes(employee_id: uuid.UUID, body: EmployeeUserInput, request: Request,
                          context=Security(system_context, scopes=["employees:read"]),
                          db: AsyncSession = Depends(get_db)):
    require_scope(context, "employees:read")
    user = await resolve_user(request, db, context, body.user)
    agent, _ = await check_agent_access(db, user, employee_id)
    if agent.tenant_id != context.application.tenant_id or agent.scope != "standard" or agent.is_deleted:
        fail("employee_unavailable", 404)
    items = await available_scenes(db, employee_id)
    await db.commit()
    return {"items": items, "total": len(items)}


@router.post("/digital-employees/{employee_id}/access", response_model=EmployeeAccessOut,
             response_model_exclude_unset=True,
             description=(
                 "With only user, returns the existing employee information and access URL. "
                 "Adding instance_ref, interaction, embed_origin or scene_key also requires auth:login and "
                 "returns a temporary login URL. An interaction prepares a first question and "
                 "optional JSON context without executing it. Opening the login URL activates "
                 "one new conversation and one first question; identical request_id retries "
                 "reuse that interaction. instance_ref alone resumes its current conversation. "
                 "scene_key selects an enabled published scene when the login URL is opened. "
                 "host_context.enabled adds per-message host snapshots; instance_ref and embed_origin "
                 "are required. Enabling it does not create an automatic question."
             ),
             responses={
                 400: {"description": "invalid_interaction or invalid_host_context: invalid optional launch fields."},
                 404: {"description": "scene_unavailable: the selected scene is not available for activation."},
                 409: {"description": "interaction_conflict: request_id was already used with different business content."},
                 410: {"description": "interaction_expired or interaction_unavailable: the interaction can no longer be activated."},
             })
async def employee(employee_id: uuid.UUID, body: EmployeeAccessInput, request: Request,
                   context=Security(system_context, scopes=["employees:read"]), db: AsyncSession = Depends(get_db)):
    require_scope(context, "employees:read")
    user = await resolve_user(request, db, context, body.user)
    agent, _ = await check_agent_access(db, user, employee_id)
    if agent.tenant_id != context.application.tenant_id or agent.scope != "standard" or agent.is_deleted:
        fail("employee_unavailable", 404)
    base = await platform_service.get_configured_public_base_url(db)
    result = employee_projection(agent, base)
    host_context = host_context_bootstrap(body, base)
    if any(value is not None for value in (body.instance_ref, body.interaction, body.embed_origin, body.scene_key)):
        require_scope(context, "auth:login")
        app = context.application
        if body.embed_origin and app.embed_origins and body.embed_origin not in app.embed_origins:
            fail("embed_origin_denied")
        if body.scene_key is not None:
            manifest = await require_available_scene(db, employee_id, body.scene_key)
            result.update(scene_key=manifest["scene_key"], scene_revision=manifest["revision"])
        record = None
        if body.interaction is not None:
            record = await prepare_interaction(
                db, app, user, agent.id, body.instance_ref, body.interaction.model_dump(), body.scene_key,
            )
        launcher = {"employee_id": str(agent.id), "instance_ref": body.instance_ref,
                    "interaction_id": str(record.id) if record else None, "scene_key": body.scene_key}
        if host_context is not None:
            launcher["host_context"] = host_context
            result["host_context"] = {"version": 1, "frame_origin": host_context["frame_origin"]}
        code = await issue_login_code(
            db, app, user, f"/h5/agents/{agent.id}/chat", body.embed_origin, launcher=launcher,
        )
        result.update(login_url=f"{base}/openapi/login?{urlencode({'code': code})}", expires_in=60)
        if record:
            result["request_id"] = record.request_id
    await db.commit()
    return result


@router.post("/auth/links", response_model=LoginLinkOut)
async def login_link(body: LoginLinkInput, request: Request,
                     context=Security(system_context, scopes=["auth:login"]), db: AsyncSession = Depends(get_db)):
    require_scope(context, "auth:login")
    app = context.application
    user = await resolve_user(request, db, context, body.user)
    if body.embed_origin and app.embed_origins and body.embed_origin not in app.embed_origins:
        fail("embed_origin_denied")
    base = await platform_service.get_configured_public_base_url(db)
    redirect_uri = redirect_target(app, body.redirect_uri, base)
    code = await issue_login_code(db, app, user, redirect_uri, body.embed_origin)
    login_url = f"{base}/openapi/login?{urlencode({'code': code})}"
    await db.commit()
    return {"login_url": login_url, "expires_in": 60}


@router.post("/auth/link-exchange",
             description=(
                 "Exchange a single-use login code for the delegated user's ordinary login. "
                 "Codes from extended employee access recheck employee permission and activate "
                 "the prepared conversation before returning its redirect URI. First-question "
                 "execution starts only after the activation commits; repeated activation "
                 "through newly issued codes does not submit another question. Generic auth/links "
                 "codes retain their existing login and redirect behavior."
             ),
             responses={
                 200: {"model": LoginExchangeOut},
                 400: {"description": "invalid_interaction: the launch reference is invalid."},
                 404: {"description": "scene_unavailable: the selected scene is no longer available."},
                 410: {"description": "interaction_expired or interaction_unavailable: the prepared interaction is no longer available."},
             })
async def exchange_link(body: LoginExchangeInput, request: Request, response: Response,
                        db: AsyncSession = Depends(get_db)):
    payload = verify_login_code(body.code, allow_agent=True, allow_expired_agent=True)
    if payload.get("aud") == AGENT_AUDIENCE:
        return await exchange_agent_login(db, body.code, payload, request, response)
    app, value = await credential(db, body.code, "login", lock=True)
    request.state.openapi_application_id = app.id
    request.state.openapi_user_id = value.user_id
    if (payload.get("app_id") != str(app.id) or payload.get("generation") != app.generation
            or payload.get("redirect_uri") != value.redirect_uri):
        fail("invalid_login_code", 401)
    user = await login_user(db, app, value)
    base = await platform_service.get_configured_public_base_url(db)
    redirect_uri = redirect_target(app, value.redirect_uri, base)
    anchor = None
    if value.launcher is not None:
        redirect_uri, anchor = await activate_launcher(db, app, user, value.launcher)
    value.consumed_at = now()
    jwt_token = create_access_token(str(user.id), user.role)
    await audit(db, "login.complete", application_id=app.id, user_id=user.id)
    await db.commit()
    if anchor is not None:
        await schedule_durable_turn_resume(anchor)
    set_access_token_cookie(response, request, jwt_token)
    result = {"access_token": jwt_token, "token_type": "bearer", "user": UserOut.model_validate(user),
              "redirect_uri": redirect_uri}
    if value.launcher and value.launcher.get("host_context"):
        result["host_context"] = value.launcher["host_context"]
    return result
