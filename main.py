import os
import discord
import aiohttp
from aiohttp import web
from urllib.parse import urlparse

# ====== ENV VARS ======
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
SOURCE_CHANNEL_ID = int(os.getenv("SOURCE_CHANNEL_ID", "0"))
DEST_WEBHOOK_URL = os.getenv("DEST_WEBHOOK_URL")

# ONLY this webhook (by URL) is allowed to be relayed FROM the source channel.
# Leave empty to ignore ALL webhook messages in the source channel.
ALLOWED_SOURCE_WEBHOOK_URL = os.getenv("ALLOWED_SOURCE_WEBHOOK_URL", "")

# What name/avatar to show in the destination channel (NOT the original user)
RELAY_USERNAME = os.getenv("RELAY_USERNAME", "Relay")
RELAY_AVATAR_URL = os.getenv("RELAY_AVATAR_URL", "")  # optional; leave blank for none

# If True: if a message was never relayed (e.g., bot restarted) and then it gets edited,
# the bot will create a relayed message at edit-time.
CREATE_ON_EDIT_IF_MISSING = os.getenv("CREATE_ON_EDIT_IF_MISSING", "true").lower() in ("1", "true", "yes")

if not DISCORD_TOKEN or SOURCE_CHANNEL_ID == 0 or not DEST_WEBHOOK_URL:
    raise SystemExit("Missing env vars. Set DISCORD_TOKEN, SOURCE_CHANNEL_ID, DEST_WEBHOOK_URL.")


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
        idx = parts.index("webhooks")
        return int(parts[idx + 1])
    except Exception:
        return None


DEST_WEBHOOK_ID = extract_webhook_id(DEST_WEBHOOK_URL)
ALLOWED_SOURCE_WEBHOOK_ID = extract_webhook_id(ALLOWED_SOURCE_WEBHOOK_URL)

# ====== DISCORD CLIENT ======
intents = discord.Intents.default()
intents.message_content = True  # MUST also be enabled in Discord Developer Portal
client = discord.Client(intents=intents)

http_session: aiohttp.ClientSession | None = None
dest_webhook: discord.Webhook | None = None

# Map: source message id -> destination (relayed) message id
relay_map: dict[int, int] = {}


def build_relay_content(msg: discord.Message) -> str:
    content = (msg.content or "").strip()

    # Append attachment URLs
    if msg.attachments:
        urls = [a.url for a in msg.attachments]
        content = (content + "\n\n" if content else "") + "\n".join(urls)

    return content[:2000].strip()


async def ensure_clients():
    global http_session, dest_webhook
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
    if dest_webhook is None:
        dest_webhook = discord.Webhook.from_url(DEST_WEBHOOK_URL, session=http_session)


def should_relay_message(msg: discord.Message) -> bool:
    # Only relay from the specific source channel
    if msg.channel.id != SOURCE_CHANNEL_ID:
        return False

    # If it's a webhook message, only allow the one webhook you specified
    if msg.webhook_id is not None:
        if ALLOWED_SOURCE_WEBHOOK_ID is None:
            return False  # no allowed webhook configured -> ignore all webhook messages
        if int(msg.webhook_id) != int(ALLOWED_SOURCE_WEBHOOK_ID):
            return False  # not the allowed webhook -> ignore

    # Prevent looping: never re-relay messages created by our destination webhook
    if DEST_WEBHOOK_ID is not None and msg.webhook_id is not None:
        if int(msg.webhook_id) == int(DEST_WEBHOOK_ID):
            return False

    return True


async def relay_send_from_message(msg: discord.Message) -> int | None:
    """Send relayed message to destination webhook. Returns relayed message ID."""
    content = build_relay_content(msg)
    if not content:
        return None

    await ensure_clients()
    assert dest_webhook is not None

    sent = await dest_webhook.send(
        content=content,
        username=RELAY_USERNAME,
        avatar_url=RELAY_AVATAR_URL if RELAY_AVATAR_URL else None,
        wait=True,  # returns created message
        allowed_mentions=discord.AllowedMentions.none(),
    )
    return sent.id


async def relay_edit(relayed_id: int, new_content: str):
    await ensure_clients()
    assert dest_webhook is not None
    await dest_webhook.edit_message(
        message_id=relayed_id,
        content=new_content,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@client.event
async def on_ready():
    print(f"Logged in as {client.user} (id={client.user.id})")
    print("RELAY_USERNAME =", RELAY_USERNAME)
    print("CREATE_ON_EDIT_IF_MISSING =", CREATE_ON_EDIT_IF_MISSING)
    if ALLOWED_SOURCE_WEBHOOK_URL and not ALLOWED_SOURCE_WEBHOOK_ID:
        print("WARNING: ALLOWED_SOURCE_WEBHOOK_URL is set but couldn't parse its webhook id.")
    if not DEST_WEBHOOK_ID:
        print("WARNING: Couldn't parse DEST_WEBHOOK_URL webhook id (loop protection reduced).")


@client.event
async def on_message(message: discord.Message):
    if not should_relay_message(message):
        return

    relayed_id = await relay_send_from_message(message)
    if relayed_id is None:
        return

    relay_map[message.id] = relayed_id
    print(f"RELayed message: source={message.id} dest={relayed_id}")


@client.event
async def on_message_edit(before: discord.Message, after: discord.Message):
    print("EDIT EVENT FIRED:", after.id, "channel=", after.channel.id, "webhook_id=", after.webhook_id)

    if after.channel.id != SOURCE_CHANNEL_ID:
        print("EDIT IGNORED (wrong channel)")
        return

    if not should_relay_message(after):
        print("EDIT IGNORED (should_relay_message false)")
        return

    # Fetch latest to ensure updated content/attachments
    try:
        fresh = await after.channel.fetch_message(after.id)
        print("FETCHED FRESH MESSAGE OK")
    except Exception as e:
        fresh = after
        print("FETCH FAILED, using 'after':", repr(e))

    new_content = build_relay_content(fresh)
    print("NEW CONTENT LEN:", len(new_content))

    if not new_content:
        print("EDIT IGNORED (empty content)")
        return

    relayed_id = relay_map.get(after.id)
    print("MAPPED RELAY ID:", relayed_id)

    if not relayed_id:
        if not CREATE_ON_EDIT_IF_MISSING:
            print("NO MAPPING and CREATE_ON_EDIT_IF_MISSING is false -> cannot update")
            return
        relayed_id = await relay_send_from_message(fresh)
        if relayed_id is None:
            print("CREATE ON EDIT FAILED (no relayed id)")
            return
        relay_map[after.id] = relayed_id
        print(f"CREATED RELAY ON EDIT: source={after.id} dest={relayed_id}")
        return

    try:
        await relay_edit(relayed_id, new_content)
        print(f"UPDATED RELAY ON EDIT: source={after.id} dest={relayed_id}")
    except discord.NotFound:
        relay_map.pop(after.id, None)
        print("EDIT FAILED: destination message not found (maybe deleted)")
    except discord.Forbidden:
        print("EDIT FAILED: forbidden (webhook can't edit that message)")
    except Exception as e:
        print("EDIT FAILED:", repr(e))


# ====== WEB SERVER FOR RENDER KEEP-ALIVE ======
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
