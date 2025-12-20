# Deployment Troubleshooting Guide

This guide helps diagnose and prevent deployment issues, particularly the December 19, 2025 rate-limiting incident.

## The Dec 19, 2025 Incident: What Happened

### Timeline
1. **12:23:55** - Deployment `7ceafe4` starts
2. **12:24:33** - **Same commit deployed again** (38 seconds later)
3. **12:24:46** - First bot instance logs into Discord
4. **12:25:27** - Second bot instance logs into Discord
5. **Multiple logins from same IP** → Discord rate limit (HTTP 429)
6. **12:29:05** - Third deployment starts
7. **12:29:56** - Container stopped (replaced by new deployment)
8. **Crash loop begins** - Railway's `restartPolicyType: ON_FAILURE` with `maxRetries: 10` caused rapid restart attempts, each hitting the rate limit immediately

### Root Cause
**Railway deployed the same commit twice**, creating two simultaneous bot instances that both logged into Discord from the same IP, triggering rate limiting. Once rate-limited, the aggressive restart policy put the bot in a crash loop.

### Why It Happened
Possible triggers:
- GitHub webhook fired twice
- Railway auto-retry thinking first deploy failed
- Manual redeploy accidentally triggered
- Railway's health check system couldn't verify the first deploy succeeded (no health endpoint existed)

## Prevention Measures Implemented

### 1. Health Check Endpoint ✅
**File:** `src/common/health_server.py`

The bot now runs an HTTP server on port 8080 with three endpoints:
- `/health` - Basic liveness probe (always returns 200 if server running)
- `/ready` - Readiness probe (returns 200 only when Discord bot is ready)
- `/status` - Detailed status with metrics

**Railway Config:** `railway.json`
```json
{
  "deploy": {
    "healthcheckPath": "/ready",
    "healthcheckTimeout": 300
  }
}
```

**Benefits:**
- Railway can verify deployment succeeded before starting a new one
- Prevents duplicate deployments from overlapping
- Provides deployment lifecycle visibility

### 2. Deployment Lifecycle Logging ✅
**File:** `main.py`

Logs deployment metadata at startup:
```python
deployment_id = os.getenv('RAILWAY_DEPLOYMENT_ID', 'local')
service_id = os.getenv('RAILWAY_SERVICE_ID', 'local')
replica_id = os.getenv('RAILWAY_REPLICA_ID', 'local')
logger.info(f"🚀 Starting deployment {deployment_id[:8]}...")
```

Logs when bot is ready:
```python
logger.info(f"✅ Bot is ready! (Deployment: {deployment_id[:8]}...)")
```

**Benefits:**
- Distinguish between multiple bot instances in logs
- Identify when duplicate deployments are running
- Track deployment lifecycle in Supabase logs

### 3. Deployment Diagnostics Tool ✅
**File:** `scripts/debug.py`

New `deployments` command analyzes Railway deployment history:
```bash
python scripts/debug.py deployments
```

**Features:**
- Detects duplicate deployments (same commit within 5 minutes)
- Shows deployment timeline with statuses
- Identifies patterns in crashes/failures
- Summarizes deployment health

**Output example:**
```
🔍 Checking for duplicate deployments...

⚠️  DUPLICATE: Commit 7ceafe4 deployed twice 38s apart
   First:  2025-12-19T12:23:55.924Z
   Second: 2025-12-19T12:24:33.515Z
```

## Diagnostic Workflow

When the bot stops working:

### 1. Check Current Deployment Status
```bash
# See if bot is healthy
curl https://your-railway-url.up.railway.app/status

# Or check Railway dashboard
railway status
```

### 2. Analyze Recent Deployments
```bash
python scripts/debug.py deployments
```

Look for:
- ⚠️ Duplicate deployments (same commit, close timestamps)
- 💥 Crashed deployments
- 🗑️ Multiple REMOVED deployments in quick succession

### 3. Check Application Logs
```bash
# From Supabase (last hour, errors only)
python scripts/debug.py logs --hours 1 | grep -i error

# From Railway (last 100 lines)
python scripts/debug.py railway-logs -n 100
```

Look for:
- Multiple "Bot is ready" messages (indicates duplicate instances)
- HTTP 429 errors (rate limiting)
- Multiple deployment IDs in logs (duplicate deployments)

### 4. Check for Rate Limiting
Search logs for:
```
HTTPException: 429 Too Many Requests
discord.errors.HTTPException: 429
```

If found, wait 1-2 hours before redeploying (Discord rate limits typically expire).

## Common Issues & Solutions

### Issue: Duplicate Deployments
**Symptoms:**
- Multiple "Bot is ready" messages within minutes
- `debug.py deployments` shows duplicate commits
- Two deployment IDs in logs

**Solution:**
1. Cancel extra deployments in Railway dashboard
2. Ensure health checks are configured (`railway.json`)
3. Check GitHub webhook settings (should only trigger once per push)

### Issue: Crash Loop
**Symptoms:**
- Deployment status flips between DEPLOYING → CRASHED → DEPLOYING
- Logs show rapid restarts
- "429 Too Many Requests" in logs

**Solution:**
1. **Stop the bleeding:** Pause the service in Railway dashboard
2. Wait 1-2 hours for Discord rate limit to expire
3. Check application logs for root cause
4. Fix the issue, then redeploy

**Prevention:**
- Health checks prevent Railway from restarting unhealthy deployments
- Consider reducing `restartPolicyMaxRetries` to 3 (currently 10)

### Issue: "No linked project" in debug.py
**Symptoms:**
```
❌ No linked Railway project found.
```

**Solution:**
```bash
cd /path/to/bndc
railway link
```

## Best Practices

### Development Workflow
1. **Test locally first**
   ```bash
   python main.py --dev --summary-now
   ```

2. **Check for errors before pushing**
   ```bash
   python -m py_compile main.py src/**/*.py
   ```

3. **Push to GitHub**
   ```bash
   git add . && git commit -m "..." && git push
   ```

4. **Monitor deployment**
   - Watch Railway dashboard for deployment status
   - Check health endpoint once deployed
   - Verify bot comes online in Discord

### Emergency Response
If the bot goes down:

1. **Triage** (< 2 minutes)
   ```bash
   python scripts/debug.py deployments  # Check for issues
   curl https://your-url.up.railway.app/status  # Check health
   ```

2. **Diagnose** (< 5 minutes)
   ```bash
   python scripts/debug.py logs --hours 1  # Recent errors
   railway logs --lines 200  # Deployment logs
   ```

3. **Fix** (depends on issue)
   - If rate-limited: Wait 1-2 hours, don't redeploy
   - If duplicate deploys: Cancel extra, redeploy once
   - If code error: Fix, test locally, then deploy

### Monitoring
Regular checks to catch issues early:

**Daily:**
```bash
# Check if bot is healthy
python scripts/debug.py deployments | head -20
```

**After each deployment:**
```bash
# Verify health endpoint
curl https://your-railway-url.up.railway.app/ready

# Check logs for duplicate instances
railway logs | grep "Bot is ready"
```

## Architecture Improvements (Future)

Consider implementing:

1. **Connection pooling for archive subprocesses**
   - Current: Each archive subprocess creates new Discord connection
   - Better: Reuse main bot connection or implement connection pool

2. **Exponential backoff on login failures**
   - Current: Immediate retry on 429 error
   - Better: Wait 60s, 120s, 240s, etc. before retry

3. **Railway webhook validation**
   - Investigate why webhooks sometimes fire twice
   - Add idempotency key to prevent duplicate deploys

4. **Deployment lifecycle hooks**
   - Add graceful shutdown to old instance before new starts
   - Prevents overlapping Discord connections

5. **Reduce restart retries**
   - Current: `restartPolicyMaxRetries: 10`
   - Consider: `3` to prevent extended crash loops

## Environment Variables

The health server uses these Railway environment variables:
- `RAILWAY_DEPLOYMENT_ID` - Unique ID for this deployment
- `RAILWAY_SERVICE_ID` - Service identifier
- `RAILWAY_REPLICA_ID` - Replica identifier (for multi-region)

These are automatically set by Railway and logged at startup for diagnostics.

## References

- Health check implementation: `src/common/health_server.py`
- Railway config: `railway.json`
- Diagnostics tool: `scripts/debug.py`
- Main entry point: `main.py` (lines 76-84, 229-238)
