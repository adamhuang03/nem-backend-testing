import asyncio
import logging
import os
import re
import json
import httpx
import anthropic as _anthropic

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
from mcp.server.fastmcp import FastMCP
from pipeline import run_pipeline, run_answer_pipeline
from contextvars import ContextVar

RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost")
mcp = FastMCP("nem", host=RAILWAY_PUBLIC_DOMAIN)
app = FastAPI()

NEM_API_KEY = os.environ.get("NEM_API_KEY", "nem-test-token")
RAILWAY_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "nem-backend-testing-production.up.railway.app")
BASE_URL = f"https://{RAILWAY_URL}"


class MCPAuthMiddleware:
    """Raw ASGI middleware — avoids BaseHTTPMiddleware buffering incompatibility with SSE."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path", "").startswith("/mcp"):
            headers = {k: v for k, v in scope.get("headers", [])}
            token = headers.get(b"authorization", b"").decode().removeprefix("Bearer ").strip()
            profile = supabase.table("profiles").select("id").eq("mcp_token", token).execute()
            if not profile.data:
                print(f"[mcp] 401 unauthorized path={scope['path']}", flush=True)
                body = b'{"error":"Unauthorized"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
            user_id = profile.data[0]["id"]
            _current_user_id.set(user_id)
            is_sse = scope["path"].endswith("/sse")
            method = scope.get("method", "")
            qs = scope.get("query_string", b"").decode()
            session_id = next((p.split("=")[1] for p in qs.split("&") if p.startswith("session_id=")), None)
            sid_str = f" session={session_id}" if session_id else ""
            print(f"[mcp] {'SSE connect' if is_sse else method} path={scope['path']} user={user_id}{sid_str}", flush=True)
            await self.app(scope, receive, send)
            if is_sse:
                print(f"[mcp] SSE disconnect path={scope['path']} user={user_id}", flush=True)
            return
        await self.app(scope, receive, send)


@app.on_event("startup")
async def startup():
    print(f"[startup] host={RAILWAY_PUBLIC_DOMAIN} base_url={BASE_URL} nem_api_key_set={'yes' if NEM_API_KEY != 'nem-test-token' else 'default'}", flush=True)


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def oauth_protected_resource():
    return {
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
    }


@app.get("/.well-known/oauth-authorization-server")
def oauth_metadata():
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "registration_endpoint": f"{BASE_URL}/oauth/register",
    }


@app.post("/oauth/register")
async def oauth_register(request: Request):
    """Dynamic client registration — accepts any client for testing."""
    body = await request.json()
    return {
        "client_id": "nem-client",
        "client_secret": "nem-client-secret",
        "client_id_issued_at": 1711000000,
        "client_secret_expires_at": 0,
        **body,
    }


@app.post("/oauth/token")
async def oauth_token(request: Request):
    body = await request.form()
    code = body.get("code", "")
    if code and code.startswith("nem_"):
        return {"access_token": code, "token_type": "Bearer", "expires_in": 31536000}
    return {"access_token": NEM_API_KEY, "token_type": "Bearer", "expires_in": 31536000}


@app.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    import base64, json as _json
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    packed = base64.urlsafe_b64encode(
        _json.dumps({"redirect_uri": redirect_uri, "state": state}).encode()
    ).decode()
    google_auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={BASE_URL}/oauth/google-callback"
        f"&response_type=code"
        f"&scope=openid%20email"
        f"&state={packed}"
    )
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>nem</title>
  <link href="https://fonts.googleapis.com/css2?family=Oxanium:wght@400;500;600&display=swap" rel="stylesheet">
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Oxanium', sans-serif;
      background: #FAFAFA;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      height: 100vh;
      gap: 12px;
    }}
    .wordmark {{ font-size: 38px; font-weight: 600; color: #00C892; letter-spacing: 0.02em; }}
    .tagline {{ font-size: 13px; color: rgba(0,0,0,0.4); letter-spacing: 0.03em; margin-bottom: 24px; }}
    .google-btn {{
      display: flex; align-items: center; gap: 10px;
      padding: 11px 22px; background: white;
      border: 1px solid rgba(0,0,0,0.12); border-radius: 8px;
      font-family: 'Oxanium', sans-serif; font-size: 14px; font-weight: 500;
      color: rgba(0,0,0,0.72); cursor: pointer; text-decoration: none;
      letter-spacing: 0.02em; transition: border-color 0.15s, box-shadow 0.15s;
    }}
    .google-btn:hover {{ border-color: rgba(0,185,135,0.45); box-shadow: 0 0 0 3px rgba(0,185,135,0.08); }}
    .google-logo {{ width: 18px; height: 18px; }}
  </style>
</head>
<body>
  <div class="wordmark">nem</div>
  <div class="tagline">connect your account to continue</div>
  <a href="{google_auth_url}" class="google-btn">
    <svg class="google-logo" viewBox="0 0 24 24">
      <path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z"/>
      <path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/>
      <path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l3.66-2.84z"/>
      <path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/>
    </svg>
    Continue with Google
  </a>
</body>
</html>"""
    from fastapi.responses import HTMLResponse
    return HTMLResponse(html)


@app.get("/oauth/google-callback")
async def oauth_google_callback(request: Request):
    import base64, json as _json
    code = request.query_params.get("code", "")
    packed = request.query_params.get("state", "")

    try:
        data = _json.loads(base64.urlsafe_b64decode(packed).decode())
        redirect_uri = data["redirect_uri"]
        original_state = data["state"]
    except Exception:
        return JSONResponse({"error": "Invalid state"}, status_code=400)

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": code,
                "redirect_uri": f"{BASE_URL}/oauth/google-callback",
                "grant_type": "authorization_code",
            },
        )
        token_data = token_resp.json()
        if "access_token" not in token_data:
            return JSONResponse({"error": "Google token exchange failed", "detail": token_data}, status_code=400)

        userinfo_resp = await client.get(
            "https://www.googleapis.com/oauth2/v2/userinfo",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
        )
        userinfo = userinfo_resp.json()
        email = userinfo.get("email")

    profile = supabase.table("profiles").select("id, mcp_token").eq("email", email).execute()
    if not profile.data:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h3 style='font-family:sans-serif;padding:40px'>No nem account found for this Google account. Please sign up at trynem.vercel.app.</h3>")

    mcp_token = profile.data[0]["mcp_token"]
    if not mcp_token:
        from fastapi.responses import HTMLResponse
        return HTMLResponse("<h3 style='font-family:sans-serif;padding:40px'>No MCP token found. Please visit trynem.vercel.app to set up your account.</h3>")

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"{redirect_uri}?code={mcp_token}&state={original_state}")

app.add_middleware(MCPAuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PROFILE_ID = "f9893af7-ea34-441c-98e5-9672be957423"
_current_user_id: ContextVar[str] = ContextVar("current_user_id", default=PROFILE_ID)
NOTION_CLIENT_ID = os.environ["NOTION_CLIENT_ID"]
NOTION_CLIENT_SECRET = os.environ["NOTION_CLIENT_SECRET"]
SLACK_CLIENT_ID = os.environ["SLACK_CLIENT_ID"]
SLACK_CLIENT_SECRET = os.environ["SLACK_CLIENT_SECRET"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_APP_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_APP_CLIENT_SECRET"]

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class ExchangeRequest(BaseModel):
    provider: str  # "notion" | "slack" | "google"
    code: str
    redirect_uri: str
    user_id: str = PROFILE_ID


class StoreKeyRequest(BaseModel):
    anthropic_key: str
    user_id: str = PROFILE_ID


class RunRequest(BaseModel):
    task: str
    user_id: str = "test-user"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/test-notion")
async def test_notion(user_id: str = "test-user"):
    """Test Notion token validity via users/me."""
    result = supabase.table("connectors").select("access_token").eq("user_id", user_id).eq("tool_name", "notion").execute()
    if not result.data:
        return {"error": "No Notion token found"}
    token = result.data[0]["access_token"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://api.notion.com/v1/users/me",
            headers={"Authorization": f"Bearer {token}", "Notion-Version": "2022-06-28"},
        )
    return {"status": resp.status_code, "body": resp.json()}


@app.get("/test-slack")
async def test_slack(user_id: str = "test-user"):
    """Test Slack token validity via auth.test."""
    result = supabase.table("connectors").select("access_token").eq("user_id", user_id).eq("tool_name", "slack").execute()
    if not result.data:
        return {"error": "No Slack token found"}
    token = result.data[0]["access_token"]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {token}"},
        )
    return {"status": resp.status_code, "body": resp.json()}


@app.post("/store-key")
async def store_key(req: StoreKeyRequest):
    """Store user's Anthropic API key in Supabase."""
    supabase.table("connectors").delete().eq("user_id", req.user_id).eq("tool_name", "anthropic").execute()
    supabase.table("connectors").insert({
        "user_id": req.user_id,
        "tool_name": "anthropic",
        "access_token": req.anthropic_key,
    }).execute()
    return {"success": True}


@app.get("/connectors")
async def connectors(user_id: str = "test-user"):
    result = supabase.table("connectors").select("tool_name").eq("user_id", user_id).execute()
    return {"connected": [row["tool_name"] for row in result.data]}


@app.post("/exchange")
async def exchange(req: ExchangeRequest):
    """Exchange OAuth code for access token and save to Supabase. provider: notion | slack | google"""
    async with httpx.AsyncClient() as client:
        if req.provider == "notion":
            resp = await client.post(
                "https://api.notion.com/v1/oauth/token",
                auth=(NOTION_CLIENT_ID, NOTION_CLIENT_SECRET),
                json={"grant_type": "authorization_code", "code": req.code, "redirect_uri": req.redirect_uri},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=400, detail=f"Notion token exchange failed: {resp.text}")
            access_token = resp.json()["access_token"]

        elif req.provider == "slack":
            resp = await client.post(
                "https://slack.com/api/oauth.v2.access",
                data={"client_id": SLACK_CLIENT_ID, "client_secret": SLACK_CLIENT_SECRET,
                      "code": req.code, "redirect_uri": req.redirect_uri},
            )
            data = resp.json()
            if not data.get("ok"):
                raise HTTPException(status_code=400, detail=f"Slack token exchange failed: {data.get('error')}")
            access_token = data["authed_user"]["access_token"]

        elif req.provider == "google":
            resp = await client.post(
                "https://oauth2.googleapis.com/token",
                data={"client_id": GOOGLE_CLIENT_ID, "client_secret": GOOGLE_CLIENT_SECRET,
                      "code": req.code, "redirect_uri": req.redirect_uri, "grant_type": "authorization_code"},
            )
            data = resp.json()
            if "refresh_token" not in data:
                raise HTTPException(status_code=400, detail=f"Google token exchange failed: {data}")
            access_token = data["refresh_token"]

        else:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {req.provider}")

    supabase.table("connectors").delete().eq("user_id", req.user_id).eq("tool_name", req.provider).execute()
    supabase.table("connectors").insert({
        "user_id": req.user_id,
        "tool_name": req.provider,
        "access_token": access_token,
    }).execute()

    return {"success": True}


@app.get("/test-google")
async def test_google(user_id: str = "test-user"):
    """Test Google token validity by exchanging refresh token for access token."""
    result = supabase.table("connectors").select("access_token").eq("user_id", user_id).eq("tool_name", "google").execute()
    if not result.data:
        return {"error": "No Google token found"}
    refresh_token = result.data[0]["access_token"]
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    return {"status": resp.status_code, "body": resp.json()}


@app.get("/role")
async def get_role():
    profile = supabase.table("profiles").select("role").eq("id", PROFILE_ID).execute()
    return {"role": profile.data[0]["role"] if profile.data else None}


@app.post("/set-role")
async def set_role(role: str):
    supabase.table("profiles").update({"role": role}).eq("id", PROFILE_ID).execute()
    return {"role": role}


def _build_mcp_servers(connectors: dict) -> dict:
    mcp_servers = {}
    if "notion" in connectors:
        mcp_servers["notion"] = {
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {"OPENAPI_MCP_HEADERS": f'{{"Authorization": "Bearer {connectors["notion"]}", "Notion-Version": "2022-06-28"}}'},
        }
    if "slack" in connectors:
        mcp_servers["slack"] = {
            "command": "npx",
            "args": ["-y", "slack-mcp-server"],
            "env": {"SLACK_MCP_XOXP_TOKEN": connectors["slack"]},
        }
    if "google" in connectors:
        mcp_servers["google"] = {
            "command": "python",
            "args": ["/app/google_mcp.py"],
            "env": {
                "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
                "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
                "GOOGLE_REFRESH_TOKEN": connectors["google"],
            },
        }
    return mcp_servers


def _get_connectors(user_id: str) -> dict:
    result = supabase.table("connectors").select("tool_name, access_token").eq("user_id", user_id).execute()
    return {row["tool_name"]: row["access_token"] for row in result.data}


@app.post("/onboard")
async def onboard():
    user_id = "test-user"
    result = supabase.table("connectors").select("tool_name, access_token").eq("user_id", user_id).execute()
    connectors = {row["tool_name"]: row["access_token"] for row in result.data}

    if "anthropic" not in connectors:
        raise HTTPException(status_code=400, detail="No Anthropic key found.")
    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    mcp_servers = _build_mcp_servers(connectors)
    options = ClaudeAgentOptions(
        mcp_servers=mcp_servers,
        allowed_tools=[f"mcp__{name}__*" for name in mcp_servers],
        permission_mode="acceptEdits",
    )

    role = "professional"
    async for msg in query(
        prompt=(
            "Do a quick, light scan of the connected tools — just glance at surface-level metadata: "
            "a few Notion page titles, Slack channel names, Gmail subject lines, or Sheets file names. "
            "Do NOT read full content of anything. One tool call per source is enough. "
            "Based on what you see, infer what professional role this user likely has. "
            'Return ONLY a JSON object, no explanation: {"role": "Chief of Staff at a startup"}'
        ),
        options=options,
    ):
        if isinstance(msg, ResultMessage):
            match = re.search(r'\{.*?"role".*?\}', msg.result, re.DOTALL)
            if match:
                try:
                    role = json.loads(match.group())["role"]
                except Exception:
                    pass

    supabase.table("profiles").update({"role": role}).eq("id", PROFILE_ID).execute()
    return {"role": role}


@app.post("/suggest-tasks")
async def suggest_tasks(role: str):
    user_id = "test-user"
    result = supabase.table("connectors").select("tool_name, access_token").eq("user_id", user_id).execute()
    connectors = {r["tool_name"]: r["access_token"] for r in result.data}
    tool_names = [k for k in connectors if k != "anthropic"]

    if "anthropic" not in connectors:
        raise HTTPException(status_code=400, detail="No Anthropic key found.")
    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    client = _anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": (
            f"The user is a {role} with these tools connected: {', '.join(tool_names)}. "
            "Suggest 3 tasks that showcase what an AI agent could do FOR them — multi-step workflows that pull context from their connected tools and take real action. "
            "Good tasks: synthesize info across sources, then produce something useful (drafts, summaries, status updates). "
            "Bad tasks: single-step lookups or vague overhauls. "
            "Each task should feel like something a smart assistant would just go do. "
            "Example for a salesperson: 'Look through my open deals in Notion and draft a personalized follow-up email for each one that hasn't heard from me in 2+ weeks.' "
            'Return ONLY a JSON array of 3 strings: ["task 1", "task 2", "task 3"]'
        )}],
    )
    match = re.search(r'\[.*?\]', msg.content[0].text, re.DOTALL)
    tasks = json.loads(match.group()) if match else ["What did I miss this week?", "Summarize my open tasks", "What emails need my attention?"]
    return {"tasks": tasks}


@app.post("/run")
async def run(req: RunRequest):
    """Fetch user connectors from Supabase, build MCP config, run Agent SDK."""
    print(f"[run] user_id={req.user_id}", flush=True)

    result = supabase.table("connectors").select("tool_name, access_token").eq("user_id", req.user_id).execute()
    connectors = {row["tool_name"]: row["access_token"] for row in result.data}
    print(f"[run] connectors found: {list(connectors.keys())}", flush=True)

    if "anthropic" not in connectors:
        raise HTTPException(status_code=400, detail="No Anthropic key found. Save your API key first.")

    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    if not any(k in connectors for k in ["notion", "slack", "google"]):
        raise HTTPException(status_code=404, detail="No connectors found. Connect a tool first.")

    mcp_servers = _build_mcp_servers(connectors)
    print(f"[run] mcp_servers configured: {list(mcp_servers.keys())}", flush=True)
    print(f"[run] allowed_tools: {[f'mcp__{name}__*' for name in mcp_servers]}", flush=True)

    options = ClaudeAgentOptions(
        mcp_servers=mcp_servers,
        allowed_tools=[f"mcp__{name}__*" for name in mcp_servers],
        permission_mode="acceptEdits",
    )

    output = ""
    msg_count = 0
    async for msg in query(
        prompt=f"Using the connected tools, find relevant context for this task and answer it: {req.task}",
        options=options,
    ):
        msg_count += 1
        print(f"[run] msg #{msg_count} type={type(msg).__name__}: {str(msg)[:1000]}", flush=True)
        if isinstance(msg, ResultMessage):
            output = msg.result

    print(f"[run] done. total messages={msg_count}", flush=True)
    return {"output": output, "connected": list(connectors.keys())}


# --- MCP server ---

@mcp.tool()
async def nem_start(task: str) -> dict:
    """Run the nem pipeline for a given task. Returns the full plan.

    If status is 'complete': show the thought_log. Then scan the plan for live actions
    — steps that write to external systems (sending a Slack message, publishing or
    updating a Notion doc, creating or updating a Linear ticket, writing to Salesforce
    or any CRM, or any action that modifies real data outside this session). If the plan
    contains live actions: show the full plan and ask only "Are we good to proceed?" —
    no other questions, no surfacing of concerns or gaps. Once confirmed, execute all
    steps without stopping for any reason. If the plan contains no live actions: execute
    immediately without showing the plan or asking anything. Show progress inline as
    each step completes. As you execute each step: if the step references a specific
    resource — a Notion doc, Linear ticket, Figma board, Slack thread, Mode dashboard,
    or any named artifact — call the relevant MCP tool to fetch it before doing anything
    else in that step. Do not substitute from memory or treat the fetch as optional
    orientation.
    If status is 'missing_connector': surface the message to the user, then tell them
    to connect the missing tool and run nem again. Nothing else.
    If status is 'questions': show the questions to the user, then show the note verbatim
    on its own line before asking them to respond.
    If status is 'error': surface the message.
    """
    user_id = _current_user_id.get()
    connectors = _get_connectors(user_id)
    mcp_servers = _build_mcp_servers(connectors)
    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    mcp_options = ClaudeAgentOptions(mcp_servers=mcp_servers,
        allowed_tools=[f"mcp__{n}__*" for n in mcp_servers], permission_mode="bypassPermissions")
    base_options = ClaudeAgentOptions(allowed_tools=[], permission_mode="bypassPermissions")

    row = supabase.table("runs").insert({"user_id": user_id, "task": task}).execute()
    session_id = row.data[0]["id"]
    print(f"[nem_start] session={session_id} task={task[:100]}", flush=True)

    async def flush_logs(logs):
        try:
            supabase.table("runs").update({"logs": logs}).eq("id", session_id).execute()
            print(f"[nem_start] flushed {len(logs)} log entries session={session_id}", flush=True)
        except Exception as e:
            print(f"[nem_start] log flush failed: {e}", flush=True)

    result = await run_pipeline(task, mcp_options, base_options, on_log=flush_logs)

    supabase.table("runs").update({
        "status": result["status"],
        "logs": result["logs"],
        "session_data": result.get("session_data"),
        "plan": result.get("plan"),
    }).eq("id", session_id).execute()

    status = result["status"]
    if status == "complete":
        return {
            "status": "complete",
            "session_id": session_id,
            "thought_log": result.get("thought_log", ""),
            "plan": result.get("plan", ""),
        }
    elif status == "questions":
        return {
            "status": "questions",
            "session_id": session_id,
            "questions": result["session_data"]["questions"],
            "note": "nem will trust whatever answers you give and continue. Try to be as honest as you can. In the future, nem will ask more questions if your answers don't match how you really think about your workflows.",
        }
    elif status == "missing_connector":
        return {"status": "missing_connector", "message": result.get("plan", result.get("output", ""))}
    else:
        return {"status": "error", "message": result.get("plan", result.get("output", ""))}


@mcp.tool()
async def nem_answer(session_id: str, answers: str) -> dict:
    """Provide answers to nem's prerequisite questions. Runs shaper + jal_i and returns the full plan.

    When complete: show the thought_log. Then scan the plan for live actions — steps
    that write to external systems (sending a Slack message, publishing or updating a
    Notion doc, creating or updating a Linear ticket, writing to Salesforce or any CRM,
    or any action that modifies real data outside this session). If the plan contains
    live actions: show the full plan and ask only "Are we good to proceed?" — no other
    questions, no surfacing of concerns or gaps. Once confirmed, execute all steps
    without stopping for any reason. If the plan contains no live actions: execute
    immediately without showing the plan or asking anything. Show progress inline as
    each step completes. As you execute each step: if the step references a specific
    resource — a Notion doc, Linear ticket, Figma board, Slack thread, Mode dashboard,
    or any named artifact — call the relevant MCP tool to fetch it before doing anything
    else in that step. Do not substitute from memory or treat the fetch as optional
    orientation.
    """
    row = supabase.table("runs").select("session_data, user_id").eq("id", session_id).execute()
    if not row.data:
        return {"status": "error", "message": f"Session {session_id} not found."}

    session_data = row.data[0]["session_data"]
    user_id = row.data[0]["user_id"]
    connectors = _get_connectors(user_id)
    mcp_servers = _build_mcp_servers(connectors)
    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    mcp_options = ClaudeAgentOptions(mcp_servers=mcp_servers,
        allowed_tools=[f"mcp__{n}__*" for n in mcp_servers], permission_mode="bypassPermissions")
    base_options = ClaudeAgentOptions(allowed_tools=[], permission_mode="bypassPermissions")

    async def flush_logs(logs):
        try:
            supabase.table("runs").update({"logs": logs}).eq("id", session_id).execute()
            print(f"[nem_answer] flushed {len(logs)} log entries session={session_id}", flush=True)
        except Exception as e:
            print(f"[nem_answer] log flush failed: {e}", flush=True)

    result = await run_answer_pipeline(session_data, answers, mcp_options, base_options, on_log=flush_logs)

    supabase.table("runs").update({
        "status": result["status"],
        "logs": result.get("logs", []),
        "plan": result.get("plan"),
    }).eq("id", session_id).execute()

    return {
        "status": result["status"],
        "session_id": session_id,
        "thought_log": result.get("thought_log", ""),
        "plan": result.get("plan", ""),
    }


@mcp.tool()
async def nem_review(session_id: str, step_number: int, executed_result: str) -> dict:
    """Placeholder — not yet implemented."""
    return {"status": "not_implemented", "message": "Step review coming soon."}


app.mount("/mcp", mcp.sse_app())

# ---------------------------------------------------------------------------
# nem_test — streamable HTTP transport for connection durability testing
# ---------------------------------------------------------------------------

mcp_test = FastMCP("nem_test")

@mcp_test.tool()
async def nem_test(minutes: float) -> str:
    """Wait for the given number of minutes then return — tests whether streamable-http holds long-running connections."""
    await asyncio.sleep(minutes * 60)
    return f"Connection held for {minutes} minute(s). Streamable HTTP is working."

app.mount("/mcp_test", mcp_test.streamable_http_app())
