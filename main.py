import asyncio
import os
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client
from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage
from mcp.server.fastmcp import FastMCP

app = FastAPI()
mcp = FastMCP("nem")

NEM_API_KEY = os.environ.get("NEM_API_KEY", "")

@app.middleware("http")
async def mcp_auth(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        auth = request.headers.get("authorization", "")
        if not NEM_API_KEY or auth != f"Bearer {NEM_API_KEY}":
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
NOTION_CLIENT_ID = os.environ["NOTION_CLIENT_ID"]
NOTION_CLIENT_SECRET = os.environ["NOTION_CLIENT_SECRET"]
SLACK_CLIENT_ID = os.environ["SLACK_CLIENT_ID"]
SLACK_CLIENT_SECRET = os.environ["SLACK_CLIENT_SECRET"]

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

    if not any(k in connectors for k in ["notion", "slack"]):
        raise HTTPException(status_code=404, detail="No connectors found. Connect a tool first.")

    mcp_servers = {}
    if "notion" in connectors:
        mcp_servers["notion"] = {
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {
                "OPENAPI_MCP_HEADERS": f'{{"Authorization": "Bearer {connectors["notion"]}", "Notion-Version": "2022-06-28"}}'
            },
        }
    if "slack" in connectors:
        mcp_servers["slack"] = {
            "command": "npx",
            "args": ["-y", "slack-mcp-server"],
            "env": {"SLACK_MCP_XOXP_TOKEN": connectors["slack"]},
        }

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
async def nem_run(task: str) -> str:
    """Run a nem task using your connected tools (Notion, Slack). Returns context-aware output."""
    user_id = "test-user"

    result = supabase.table("connectors").select("tool_name, access_token").eq("user_id", user_id).execute()
    connectors = {row["tool_name"]: row["access_token"] for row in result.data}

    if "anthropic" not in connectors:
        return "Error: No Anthropic key found. Save your key at trynem.vercel.app/testing."
    os.environ["ANTHROPIC_API_KEY"] = connectors["anthropic"]

    if not any(k in connectors for k in ["notion", "slack"]):
        return "Error: No connectors found. Connect a tool first at trynem.vercel.app/testing."

    mcp_servers = {}
    if "notion" in connectors:
        mcp_servers["notion"] = {
            "command": "npx",
            "args": ["-y", "@notionhq/notion-mcp-server"],
            "env": {
                "OPENAPI_MCP_HEADERS": f'{{"Authorization": "Bearer {connectors["notion"]}", "Notion-Version": "2022-06-28"}}'
            },
        }
    if "slack" in connectors:
        mcp_servers["slack"] = {
            "command": "npx",
            "args": ["-y", "slack-mcp-server"],
            "env": {"SLACK_MCP_XOXP_TOKEN": connectors["slack"]},
        }

    options = ClaudeAgentOptions(
        mcp_servers=mcp_servers,
        allowed_tools=[f"mcp__{name}__*" for name in mcp_servers],
        permission_mode="acceptEdits",
    )

    output = ""
    async for msg in query(
        prompt=f"Using the connected tools, find relevant context for this task and answer it: {task}",
        options=options,
    ):
        if isinstance(msg, ResultMessage):
            output = msg.result

    return output


app.mount("/mcp", mcp.sse_app())
