#!/usr/bin/env python3
import os
import json
import base64
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("google")

CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["GOOGLE_REFRESH_TOKEN"]


async def get_access_token() -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post("https://oauth2.googleapis.com/token", data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": REFRESH_TOKEN,
            "grant_type": "refresh_token",
        })
    return resp.json()["access_token"]


@mcp.tool()
async def gmail_search(query: str, max_results: int = 10) -> str:
    """Search Gmail messages. Supports Gmail search syntax (from:, subject:, after:, is:unread, etc)."""
    token = await get_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": query, "maxResults": min(max_results, 20)},
        )
    data = resp.json()
    if "messages" not in data:
        return json.dumps({"error": data, "query": query})
    messages = []
    async with httpx.AsyncClient() as client:
        for msg in data["messages"][:10]:
            r = await client.get(
                f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg['id']}",
                headers={"Authorization": f"Bearer {token}"},
                params={"format": "metadata", "metadataHeaders": ["Subject", "From", "Date"]},
            )
            d = r.json()
            hdrs = {h["name"]: h["value"] for h in d.get("payload", {}).get("headers", [])}
            messages.append({
                "id": msg["id"],
                "subject": hdrs.get("Subject", ""),
                "from": hdrs.get("From", ""),
                "date": hdrs.get("Date", ""),
                "snippet": d.get("snippet", ""),
            })
    return json.dumps(messages, indent=2)


@mcp.tool()
async def gmail_get_message(message_id: str) -> str:
    """Get the full body of a Gmail message by ID."""
    token = await get_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{message_id}",
            headers={"Authorization": f"Bearer {token}"},
            params={"format": "full"},
        )
    data = resp.json()

    def extract_body(payload):
        if payload.get("mimeType") == "text/plain":
            body = payload.get("body", {}).get("data", "")
            return base64.urlsafe_b64decode(body + "==").decode("utf-8", errors="replace") if body else ""
        for part in payload.get("parts", []):
            result = extract_body(part)
            if result:
                return result
        return ""

    hdrs = {h["name"]: h["value"] for h in data.get("payload", {}).get("headers", [])}
    return json.dumps({
        "subject": hdrs.get("Subject", ""),
        "from": hdrs.get("From", ""),
        "date": hdrs.get("Date", ""),
        "body": extract_body(data.get("payload", {})),
    }, indent=2)


@mcp.tool()
async def sheets_list_files(query: str = "") -> str:
    """List Google Sheets files in Drive. Optionally filter by name with query string."""
    token = await get_access_token()
    q = "mimeType='application/vnd.google-apps.spreadsheet'"
    if query:
        q += f" and name contains '{query}'"
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.googleapis.com/drive/v3/files",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": q, "fields": "files(id,name,modifiedTime)", "pageSize": 20},
        )
    data = resp.json()
    if "files" not in data:
        return json.dumps({"error": data})
    return json.dumps(data["files"], indent=2)


@mcp.tool()
async def sheets_get_values(spreadsheet_id: str, range: str = "Sheet1!A1:Z200") -> str:
    """Read values from a Google Sheet. Use sheets_list_files first to find the spreadsheet_id. Range example: 'Sheet1!A1:Z200'"""
    token = await get_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet_id}/values/{range}",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    if "error" in data:
        return json.dumps({"error": data["error"]})
    return json.dumps({"range": data.get("range"), "values": data.get("values", [])}, indent=2)


if __name__ == "__main__":
    mcp.run()
