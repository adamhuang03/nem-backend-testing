import asyncio
import os
import re
import json
import httpx
import anthropic as _anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
from mcp.server.fastmcp import FastMCP
from pipeline import run_pipeline, run_answer_pipeline

RAILWAY_PUBLIC_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost")
mcp = FastMCP("nem", host=RAILWAY_PUBLIC_DOMAIN)
app = FastAPI()

NEM_API_KEY = os.environ.get("NEM_API_KEY", "nem-test-token")
RAILWAY_URL = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "nem-backend-testing-production.up.railway.app")
BASE_URL = f"https://{RAILWAY_URL}"


@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        auth = request.headers.get("authorization", "")
        if auth != f"Bearer {NEM_API_KEY}":
            print(f"[mcp] 401 unauthorized path={request.url.path}", flush=True)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        print(f"[mcp] {request.method} {request.url.path} query={dict(request.query_params)}", flush=True)
    response = await call_next(request)
    if request.url.path.startswith("/mcp"):
        print(f"[mcp] response status={response.status_code} path={request.url.path}", flush=True)
    return response


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
    """Issue token — accepts client_credentials without verification for testing."""
    return {
        "access_token": NEM_API_KEY,
        "token_type": "Bearer",
        "expires_in": 86400,
    }


@app.get("/oauth/authorize")
async def oauth_authorize(request: Request):
    """Redirect back with auth code immediately (open for testing)."""
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    code = "nem-auth-code"
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"{redirect_uri}?code={code}&state={state}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
PROFILE_ID = "f9893af7-ea34-441c-98e5-9672be957423"
NOTION_CLIENT_ID = os.environ["NOTION_CLIENT_ID"]
NOTION_CLIENT_SECRET = os.environ["NOTION_CLIENT_SECRET"]
SLACK_CLIENT_ID = os.environ["SLACK_CLIENT_ID"]
SLACK_CLIENT_SECRET = os.environ["SLACK_CLIENT_SECRET"]
GOOGLE_CLIENT_ID = os.environ["GOOGLE_APP_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_APP_CLIENT_SECRET"]

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class ExchangeRequest(BaseModel):
    code: str
    redirect_uri: str


class StoreKeyRequest(BaseModel):
    anthropic_key: str
    user_id: str = "test-user"


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
    """Exchange Notion OAuth code for access token, save to Supabase."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.notion.com/v1/oauth/token",
            auth=(NOTION_CLIENT_ID, NOTION_CLIENT_SECRET),
            json={
                "grant_type": "authorization_code",
                "code": req.code,
                "redirect_uri": req.redirect_uri,
            },
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=400,
            detail=f"Notion token exchange failed: {resp.text}"
        )

    access_token = resp.json()["access_token"]

    supabase.table("connectors").delete().eq("user_id", "test-user").eq("tool_name", "notion").execute()
    supabase.table("connectors").insert({
        "user_id": "test-user",
        "tool_name": "notion",
        "access_token": access_token,
    }).execute()

    return {"success": True}


@app.post("/exchange-slack")
async def exchange_slack(req: ExchangeRequest):
    """Exchange Slack OAuth code for user token, save to Supabase."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://slack.com/api/oauth.v2.access",
            data={
                "client_id": SLACK_CLIENT_ID,
                "client_secret": SLACK_CLIENT_SECRET,
                "code": req.code,
                "redirect_uri": req.redirect_uri,
            },
        )

    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(status_code=400, detail=f"Slack token exchange failed: {data.get('error')}")

    access_token = data["authed_user"]["access_token"]

    supabase.table("connectors").delete().eq("user_id", "test-user").eq("tool_name", "slack").execute()
    supabase.table("connectors").insert({
        "user_id": "test-user",
        "tool_name": "slack",
        "access_token": access_token,
    }).execute()

    return {"success": True}


@app.post("/exchange-google")
async def exchange_google(req: ExchangeRequest):
    """Exchange Google OAuth code for refresh token, save to Supabase."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "code": req.code,
                "redirect_uri": req.redirect_uri,
                "grant_type": "authorization_code",
            },
        )

    data = resp.json()
    if "refresh_token" not in data:
        raise HTTPException(status_code=400, detail=f"Google token exchange failed: {data}")

    supabase.table("connectors").delete().eq("user_id", "test-user").eq("tool_name", "google").execute()
    supabase.table("connectors").insert({
        "user_id": "test-user",
        "tool_name": "google",
        "access_token": data["refresh_token"],
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
    user_id = PROFILE_ID  # Batch 1: hardcoded; Batch 2: resolved from mcp_token
    connectors = _get_connectors("test-user")  # connectors table still keyed by "test-user"
    mcp_servers = _build_mcp_servers(connectors)
    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    mcp_options = ClaudeAgentOptions(mcp_servers=mcp_servers,
        allowed_tools=[f"mcp__{n}__*" for n in mcp_servers], permission_mode="bypassPermissions")
    base_options = ClaudeAgentOptions(allowed_tools=[], permission_mode="bypassPermissions")

    row = supabase.table("runs").insert({"user_id": user_id, "task": task}).execute()
    session_id = row.data[0]["id"]

    result = await run_pipeline(task, mcp_options, base_options)

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
    connectors = _get_connectors("test-user")  # connectors table still keyed by "test-user"
    mcp_servers = _build_mcp_servers(connectors)
    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    mcp_options = ClaudeAgentOptions(mcp_servers=mcp_servers,
        allowed_tools=[f"mcp__{n}__*" for n in mcp_servers], permission_mode="bypassPermissions")
    base_options = ClaudeAgentOptions(allowed_tools=[], permission_mode="bypassPermissions")

    result = await run_answer_pipeline(session_data, answers, mcp_options, base_options)

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
