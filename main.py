import os
import discord
import aiohttp
from aiohttp import web
from urllib.parse import urlparse

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
DEST_WEBHOOK_URL = os.getenv("DEST_WEBHOOK_URL")

# This is the ONLY webhook allowed to be relayed from the source channel.
# Set it to the webhook URL you want to allow (could be the same as DEST_WEBHOOK_URL or different).
ALLOWED_SOURCE_WEBHOOK_URL = os.getenv("ALLOWED_SOURCE_WEBHOOK_URL", "")

if not DISCORD_TOKEN or SOURCE_CHANNEL_ID == 0 or not DEST_WEBHOOK_URL:
    raise SystemExit(
        "Missing env vars. Set DISCORD_TOKEN, SOURCE_CHANNEL_ID, DEST_WEBHOOK_URL."
    )

def extract_webhook_id(webhook_url: str) -> int | None:
    """
    Extract webhook ID from a Discord webhook URL like:
    https://discord.com/api/webhooks/{webhook_id}/{token}
    Returns None if parsing fails.
    """
    if not webhook_url:
        return None
    try:
        path = urlparse(webhook_url).path.strip("/")
        parts = path.split("/")
        # ... /api/webhooks/{id}/{token}
        idx = parts.index("webhooks")
        wid = int(parts[idx + 1])
        return wid
    except Exception:
        return None

DEST_WEBHOOK_ID = extract_webhook_id(DEST_WEBHOOK_URL)
ALLOWED_SOURCE_WEBHOOK_ID = extract_webhook_id(ALLOWED_SOURCE_WEBHOOK_URL)

# Discord intents
intents = discord.Intents.default()
intents.message_content = True  # must be enabled in Discord Developer Portal too
client = discord.Client(intents=intents)

http_session: aiohttp.ClientSession | None = None

async def ensure_session() -> aiohttp.ClientSession:
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    return http_session

async def post_to_webhook(payload: dict) -> None:
    session = await ensure_session()
    async with session.post(DEST_WEBHOOK_URL, json=payload) as resp:
        if resp.status >= 300:
            text = await resp.text()
            print(f"Webhook post failed: {resp.status} {text}")

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")
    if ALLOWED_SOURCE_WEBHOOK_URL and not ALLOWED_SOURCE_WEBHOOK_ID:
        print("WARNING: ALLOWED_SOURCE_WEBHOOK_URL is set but couldn't parse webhook id.")
    if not DEST_WEBHOOK_ID:
        print("WARNING: Couldn't parse DEST_WEBHOOK_URL webhook id (loop protection still works via webhook_id check).")

@client.event
async def on_message(message: discord.Message):
    # Only relay from the specific source channel
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    # If it's a webhook message, only allow the one webhook you specified
    if message.webhook_id is not None:
        if ALLOWED_SOURCE_WEBHOOK_ID is None:
            # No allowed webhook configured -> ignore ALL webhook messages
            return
        if int(message.webhook_id) != int(ALLOWED_SOURCE_WEBHOOK_ID):
            # Not the allowed webhook -> ignore
            return

    # Prevent looping: never re-relay messages created by our destination webhook
    # (This is extra safety in case SOURCE_CHANNEL_ID == destination channel)
    if DEST_WEBHOOK_ID is not None and message.webhook_id is not None:
        if int(message.webhook_id) == int(DEST_WEBHOOK_ID):
            return

    # Allow bot messages (and humans)
    content = message.content or ""

    # If it's a reply, include lightweight context (optional)
    if message.reference and isinstance(message.reference.resolved, discord.Message):
        ref = message.reference.resolved
        ref_text = (ref.content or "").strip()
        if ref_text:
            content = f"> Replying to {ref.author.display_name}: {ref_text}\n{content}"

    # Append attachment URLs
    if message.attachments:
        urls = [a.url for a in message.attachments]
        content = (content + "\n\n" if content else "") + "\n".join(urls)

    # If there's nothing to send (e.g., embed-only), skip
    if not content.strip():
        return

    # For webhook messages, author info may be None-ish; fall back safely
    username = getattr(message.author, "display_name", None) or "Webhook"
    avatar_url = None
    try:
        if message.author and hasattr(message.author, "display_avatar"):
            avatar_url = message.author.display_avatar.url
    except Exception:
        avatar_url = None

    payload = {
        "username": username,
        "avatar_url": avatar_url,
        "content": content[:2000],
        "allowed_mentions": {"parse": []},  # prevent ping relays
    }

    await post_to_webhook(payload)

# --- Web server for Render (keeps service "wakeable") ---

async def create_app() -> web.Application:
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="ok")

    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    async def on_cleanup(_app: web.Application):
        global http_session
        if http_session and not http_session.closed:
            await http_session.close()

    app.on_cleanup.append(on_cleanup)
    return app

async def main():
    port = int(os.getenv("PORT", "10000"))
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP server running on 0.0.0.0:{port}")

    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
