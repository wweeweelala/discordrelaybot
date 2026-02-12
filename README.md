# Discord Relay Bot (Render Free)

Relays all messages from one Discord channel to a destination webhook.

## Env Vars (set these in Render)
- DISCORD_TOKEN
- SOURCE_CHANNEL_ID
- DEST_WEBHOOK_URL

## Keep-alive endpoint
- GET /health returns `ok`

Use UptimeRobot to ping:
https://YOUR-RENDER-URL.onrender.com/health
every 5 minutes.

## Notes
- Enable "Message Content Intent" in the Discord Developer Portal for your bot.
- Free tiers can still be unreliable; if the service is down, messages sent during downtime are missed.
