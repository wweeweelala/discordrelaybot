import os
import discord
import aiohttp
from aiohttp import web

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
DEST_WEBHOOK_URL = os.getenv("DEST_WEBHOOK_URL")

if not DISCORD_TOKEN or SOURCE_CHANNEL_ID == 0 or not DEST_WEBHOOK_URL:
    raise SystemExit(
        "Missing env vars. Set DISCORD_TOKEN, SOURCE_CHANNEL_ID, DEST_WEBHOOK_URL."
    )

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

@client.event
async def on_message(message: discord.Message):
    # Only relay from your chosen channel
    if message.channel.id != SOURCE_CHANNEL_ID:
        return

    # Prevent loops + ignore other bots/webhooks
    if message.author.bot or message.webhook_id is not None:
        return

    # Build content
    content = message.content or ""

    # If it's a reply, add lightweight context (optional)
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

    payload = {
        "username": message.author.display_name,
        "avatar_url": message.author.display_avatar.url,
        "content": content[:2000],  # Discord message limit
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
    # Start the HTTP server on Render's PORT
    port = int(os.getenv("PORT", "10000"))
    app = await create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"HTTP server running on 0.0.0.0:{port}")

    # Start Discord client (runs forever)
    await client.start(DISCORD_TOKEN)

if __name__ == "__main__":
    import asyncio
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
