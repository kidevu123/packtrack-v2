# Runbook

## Health

```bash
ssh root@192.168.1.190 "pct exec 200 -- curl -fsS http://127.0.0.1:8000/healthz"
ssh root@192.168.1.190 "pct exec 200 -- systemctl status packtrack --no-pager"
```

## Logs

```bash
ssh root@192.168.1.190 "pct exec 200 -- journalctl -u packtrack.service --no-pager --lines=100"
ssh root@192.168.1.190 "pct exec 200 -- journalctl -u caddy.service --no-pager --lines=100"
```

## Restart

```bash
ssh root@192.168.1.190 "pct exec 200 -- systemctl restart packtrack.service"
```

## Zoho Sync

Open `/admin/sync` as an owner and click Sync now. If sync fails:

- Confirm `ZOHO_GATEWAY_URL`, `ZOHO_GATEWAY_TOKEN`, and `ZOHO_GATEWAY_BRAND`.
- Check `journalctl -u packtrack.service`.
- Confirm the gateway can reach Zoho.

Failed PO pushes are retried by the scheduler when `ZOHO_CLIENT_ID`, `ZOHO_CLIENT_SECRET`, `ZOHO_REFRESH_TOKEN`, and `ZOHO_ORG_ID` are configured.

## Telegram

Set:

```bash
TELEGRAM_BOT_TOKEN=...
TELEGRAM_WEBHOOK_SECRET=...
```

If `TELEGRAM_WEBHOOK_SECRET` is set, PackTrack rejects webhook calls missing Telegram's `x-telegram-bot-api-secret-token` header.

## Receiving/Luma

Receiving records one `BoxReceipt` per supplier carton and prevents duplicate box numbers per PO. Rows without material codes are blocked from Luma push until the material code is resolved. Use the receiving retry path for failed or not-ready Luma pushes.

## Scheduler

The service is intended to run as one uvicorn worker. Scheduler jobs also take file locks in `LOG_DIR` so a second process does not double-run Zoho sync or push retry.

## Backups

Nightly backup units are installed by deploy:

```bash
ssh root@192.168.1.190 "pct exec 200 -- systemctl list-timers packtrack-backup.timer"
ssh root@192.168.1.190 "pct exec 200 -- ls -lah /var/backups/packtrack"
```

Restore with:

```bash
ssh root@192.168.1.190 "pct exec 200 -- /opt/packtrack/app/deploy/restore.sh <backup-file>"
```

## Emergency Checks

```bash
ssh root@192.168.1.190 "pct exec 200 -- df -h"
ssh root@192.168.1.190 "pct exec 200 -- systemctl is-active postgresql caddy packtrack"
ssh root@192.168.1.190 "pct exec 200 -- tail -n 80 /var/log/caddy/packtrack.log"
```
