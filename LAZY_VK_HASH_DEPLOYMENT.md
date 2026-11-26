# WDTT VPN - Lazy VK Hash Initialization Deployment Guide

## Overview

This implementation adds **lazy VK hash initialization** - VK calls are only created when a user first connects via VLESS link, not when the user is created in the panel.

### How it works:

1. **User created in panel** → No VK call created, `vk_hash_initialized = 0`
2. **User connects via VLESS** → Plugin detects no VK hash
3. **Plugin calls VK Hash Service** (`POST /vk-hash/ensure`) with user's `wdtt_password`
4. **VK Hash Service** creates VK call via Panel API (`POST /panel/api/vk/call/create`)
5. **Panel returns real vk_hash** → Service marks user as initialized (`vk_hash_initialized = 1`)
6. **Plugin caches vk_hash** → Subsequent connections use cached hash
7. **VK Relay created** → Connection established via WebRTC + DTLS

---

## Architecture

```
┌─────────────┐     VLESS Connection      ┌──────────────────┐
│   Client    │ ───────────────────────▶  │   Xray + Plugin  │
└─────────────┘                           └────────┬─────────┘
                                                    │
                                                    │ Check vk_hash cache
                                                    ▼
                                          ┌──────────────────┐
                                          │  VKHashManager   │
                                          │   (in-memory)    │
                                          └────────┬─────────┘
                                                   │
                                          Not found │
                                                   ▼
                                          ┌──────────────────┐
                                          │ HTTP POST        │
                                          │ /vk-hash/ensure  │
                                          │ wdtt_password: X │
                                          └────────┬─────────┘
                                                   │
                                                   ▼
                                          ┌──────────────────┐
                                          │ VK Hash Service  │
                                          │ (Python aiohttp) │
                                          └────────┬─────────┘
                                                   │
                                                   ▼
                                          ┌──────────────────┐
                                          │ WDTT Panel API   │
                                          │ POST /vk/call/   │
                                          │ create {pwd,      │
                                          │ apply: true}     │
                                          └────────┬─────────┘
                                                   │
                                          Returns vk_hash │
                                                   ▼
                                          ┌──────────────────┐
                                          │ Update DB        │
                                          │ vk_hash_initialized│
                                          │ = 1, created_at  │
                                          └────────┬─────────┘
                                                   │
                                          Return vk_hash │
                                                   ▼
                                          ┌──────────────────┐
                                          │ Cache in         │
                                          │ VKHashManager    │
                                          │ Create VK Relay  │
                                          └──────────────────┘
```

---

## Files Added/Modified

### New Files:
1. **`migrate_vk_hash.py`** - Database migration script
2. **`vk_hash_service.py`** - HTTP service for lazy VK hash creation
3. **`plugin/wdtt/integrator.go`** - Updated with lazy initialization logic
4. **`plugin/main.go`** - Updated with VKHashServiceConfig

### Modified Files:
1. **`db.py`** - Added `vk_hash_initialized` and `vk_hash_created_at` columns + helper functions
2. **`docker-compose.yml`** - Added `vk-hash-service` and `redis` services
3. **`plugin-config.json`** - Added `vk_hash_service_url` and `vk_hash_service_timeout`

---

## Deployment Steps

### 1. Run Database Migration

```bash
# On server with existing database
cd /opt/wdtt-bot
python migrate_vk_hash.py
```

This adds two columns to `customers` table:
- `vk_hash_initialized` (INTEGER DEFAULT 0)
- `vk_hash_created_at` (INTEGER DEFAULT 0)

### 2. Update Configuration

Edit `.env`:
```bash
# Add to .env
VK_HASH_SERVICE_PORT=8083
```

Edit `plugin-config.json`:
```json
{
  "vk_hash_service_url": "http://localhost:8083",
  "vk_hash_service_timeout": 30
}
```

### 3. Deploy with Docker Compose

```bash
cd /opt/wdtt-deploy
docker-compose up -d --build
```

This starts:
- `wdtt-bot` (main bot)
- `vk-hash-service` (lazy VK hash HTTP service on port 8083)
- `redis` (for caching/sessions)

### 4. Verify Services

```bash
# Check VK hash service health
curl http://localhost:8083/health
# {"status": "ok", "service": "vk-hash-lazy-init"}

# Check VK hash status for a user
curl "http://localhost:8083/vk-hash/status?wdtt_password=USER_PASSWORD"
```

---

## How It Preserves Existing Data

| Data | Preserved? | How |
|------|------------|-----|
| **User subscriptions** | ✅ | No changes to `customers` table except new columns |
| **Payment history** | ✅ | `payments` table unchanged |
| **Referrals** | ✅ | `referrals` table unchanged |
| **Server configs** | ✅ | `servers` table unchanged |
| **Plans** | ✅ | `plans` table unchanged |
| **Site users/sessions** | ✅ | `site_users`, `site_sessions` unchanged |
| **VK accounts** | ✅ | `vk_accounts`, `vk_issued` unchanged |

**New columns added to `customers`:**
```sql
vk_hash_initialized INTEGER NOT NULL DEFAULT 0,
vk_hash_created_at INTEGER NOT NULL DEFAULT 0
```

---

## Configuration Options

### Plugin Config (`plugin-config.json`)

```json
{
  "vk_hash_service_url": "http://localhost:8083",
  "vk_hash_service_timeout": 30
}
```

| Parameter | Description | Default |
|-----------|-------------|---------|
| `vk_hash_service_url` | HTTP endpoint of VK hash service | Required for lazy init |
| `vk_hash_service_timeout` | Request timeout in seconds | 30 |

### VK Hash Service Config (Environment Variables)

| Variable | Description | Default |
|----------|-------------|---------|
| `VK_HASH_SERVICE_PORT` | HTTP port | 8083 |
| `VK_HASH_SERVICE_HOST` | Bind address | 0.0.0.0 |
| `DB_PATH` | SQLite database path | /app/data/bot.db |

---

## Testing

### Test VK Hash Creation

```bash
# 1. Get a user's wdtt_password from panel
# 2. Call ensure endpoint
curl -X POST http://localhost:8083/vk-hash/ensure \
  -H "Content-Type: application/json" \
  -d '{"wdtt_password": "USER_WDTT_PASSWORD"}'

# Expected response:
# {"success": true, "vk_hash": "abc123...", "initialized": true, "error": ""}
```

### Test VLESS Connection

1. Generate VLESS link for user
2. Connect with client (v2rayNG, Shadowrocket, etc.)
3. Check logs: `docker-compose logs -f vk-hash-service`
4. Should see: `Created VK hash for user 123456: abc123...`

### Verify Database

```bash
sqlite3 /opt/wdtt-bot/data/bot.db \
  "SELECT telegram_id, wdtt_password, vk_hash_initialized, vk_hash_created_at FROM customers WHERE vk_hash_initialized = 1;"
```

---

## Rollback Plan

If issues arise:

1. **Disable lazy init** - Remove `vk_hash_service_url` from plugin-config.json
2. **Restart Xray** - Plugin will use existing VKHashManager polling
3. **Database** - New columns are harmless, can be left or dropped:
   ```sql
   ALTER TABLE customers DROP COLUMN vk_hash_initialized;
   ALTER TABLE customers DROP COLUMN vk_hash_created_at;
   ```

---

## Monitoring

### Metrics to Watch

| Metric | Source | Alert If |
|--------|--------|----------|
| `vk_hash_service_requests_total` | VK Hash Service | > 100 errors/min |
| `vk_hash_creation_duration_seconds` | VK Hash Service | > 30s p99 |
| `vk_hash_initialized_users` | DB query | Stuck at 0 for active users |
| `panel_vk_call_create_errors` | Panel logs | Any errors |

### Logs

```bash
# VK Hash Service logs
docker-compose logs -f vk-hash-service

# Xray Plugin logs (shows lazy init)
docker-compose logs -f xray | grep -i "vk_hash\|ensure"
```

---

## Troubleshooting

### Issue: "no vk_hash available for user"
**Cause**: VK hash service unreachable or failed
**Fix**: 
1. Check service health: `curl http://localhost:8083/health`
2. Check panel API: `curl -k https://panel:2860/panel/api/vk/status`
3. Check logs: `docker-compose logs vk-hash-service`

### Issue: VK hash service returns 500
**Cause**: Panel API error (cookies expired, rate limit)
**Fix**:
1. Check panel cookies in `.env`: `WDTT_USERNAME`, `WDTT_PASSWORD`
2. Verify panel accessible: `curl -k $WDTT_BASE_URL/panel/api/users`
3. Restart panel if needed

### Issue: Connection timeout on first VLESS connect
**Cause**: VK call creation takes time (WebRTC signaling)
**Fix**:
- Increase `vk_hash_service_timeout` in plugin-config.json
- Check network between plugin and VK hash service

---

## Security Notes

- VK Hash Service runs on localhost (port 8083) - not exposed externally
- Uses same database as bot - protected by file permissions
- Panel credentials only in bot container, not in plugin
- All communication over Docker host network (network_mode: host)