import os
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from supabase import create_client
import anthropic

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tighten for production
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
NOTION_CLIENT_ID = os.environ["NOTION_CLIENT_ID"]
NOTION_CLIENT_SECRET = os.environ["NOTION_CLIENT_SECRET"]

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


class ExchangeRequest(BaseModel):
    code: str
    redirect_uri: str


class RunRequest(BaseModel):
    task: str
    anthropic_key: str
    user_id: str = "test-user"


@app.get("/health")
def health():
    return {"status": "ok"}


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

    # Delete any existing connector for this user+tool, then insert fresh
    supabase.table("connectors").delete().eq("user_id", "test-user").eq("tool_name", "notion").execute()
    supabase.table("connectors").insert({
        "user_id": "test-user",
        "tool_name": "notion",
        "access_token": access_token,
    }).execute()

    return {"success": True}


@app.post("/run")
async def run(req: RunRequest):
    """Read Notion token from Supabase, fetch context, run Claude, return output."""
    # Get token from Supabase
    result = (
        supabase.table("connectors")
        .select("access_token")
        .eq("user_id", req.user_id)
        .eq("tool_name", "notion")
        .execute()
    )

    if not result.data:
        raise HTTPException(
            status_code=404,
            detail="No Notion token found. Connect Notion first."
        )

    notion_token = result.data[0]["access_token"]

    # Fetch context from Notion
    notion_context = await get_notion_context(notion_token, req.task)

    # Run Claude with context + user's Anthropic key
    claude = anthropic.Anthropic(api_key=req.anthropic_key)
    message = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        messages=[{
            "role": "user",
            "content": f"Here is context from Notion:\n\n{notion_context}\n\nTask: {req.task}"
        }]
    )

    return {"output": message.content[0].text, "notion_context": notion_context}


async def get_notion_context(token: str, task: str) -> str:
    """Search Notion via REST API and return a summary of relevant pages."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://api.notion.com/v1/search",
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2022-06-28",
                "Content-Type": "application/json",
            },
            json={"query": task, "page_size": 5},
        )

    if resp.status_code != 200:
        return f"[Notion API error: {resp.status_code} — {resp.text[:300]}]"

    results = resp.json().get("results", [])
    if not results:
        return "No relevant Notion pages found."

    lines = []
    for page in results:
        # Extract title
        title = "Untitled"
        props = page.get("properties", {})
        for prop in props.values():
            if prop.get("type") == "title":
                arr = prop.get("title", [])
                if arr:
                    title = arr[0].get("plain_text", "Untitled")
                    break
        # Databases have title at top level
        if title == "Untitled" and page.get("object") == "database":
            db_title = page.get("title", [])
            if db_title:
                title = db_title[0].get("plain_text", "Untitled")

        url = page.get("url", "")
        lines.append(f"- {title}: {url}")

    return "Relevant Notion pages:\n" + "\n".join(lines)
