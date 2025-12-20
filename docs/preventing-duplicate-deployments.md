# Preventing Duplicate Deployments

## The Problem

On December 19, 2025, Railway deployed commit `7ceafe4` **twice** within 38 seconds, causing two bot instances to run simultaneously and triggering Discord's rate limiter.

## Root Causes

Duplicate deployments can happen due to:

### 1. **GitHub Webhook Firing Twice**
- GitHub sometimes sends duplicate webhook events
- Network retries can cause duplicate triggers
- Multiple webhooks configured in GitHub repo settings

### 2. **Railway Auto-Retry**
- If Railway thinks the first deployment failed during build/startup
- Before health checks existed, Railway couldn't verify deployment success
- May trigger a retry while first deployment is still starting

### 3. **Manual Triggers**
- Accidentally clicking "Redeploy" while a deployment is in progress
- Multiple team members triggering deploys simultaneously

### 4. **Race Conditions**
- Push triggers deployment
- Another push happens before first completes
- Both deployments proceed

## Solutions Implemented ✅

### 1. Health Check System
**Status:** ✅ Implemented (Dec 20, 2025)

Railway now uses the `/ready` endpoint to verify deployment success:

```json
// railway.json
{
  "deploy": {
    "healthcheckPath": "/ready",
    "healthcheckTimeout": 300
  }
}
```

**How it helps:**
- Railway waits for `/ready` to return 200 before marking deployment as successful
- Won't start a new deployment until previous one is confirmed ready
- Prevents overlapping deployments

**Current behavior:**
- Bot starts → health server starts on port 8080
- Bot connects to Discord
- Bot marks ready → `/ready` returns 200
- Railway considers deployment successful

### 2. Deployment ID Logging
**Status:** ✅ Implemented (Dec 20, 2025)

Every deployment logs its unique ID:
```
🚀 Starting deployment 0cdddc18-b06... (service: e9af91ca-730...)
```

**How it helps:**
- Can identify duplicate instances in logs
- Easy to spot when two deployments are running simultaneously
- Use `grep "Starting deployment"` to find overlaps

### 3. Diagnostic Tool
**Status:** ✅ Implemented (Dec 20, 2025)

```bash
python scripts/debug.py deployments
```

Automatically detects duplicate deployments:
```
⚠️  DUPLICATE: Commit 7ceafe4 deployed twice 38s apart
   First:  2025-12-19T12:23:55.924Z
   Second: 2025-12-19T12:24:33.515Z
```

## Additional Prevention Measures (Recommended)

### 1. Add Deployment Mutex (Railway-Level)
**Status:** ⚠️ Needs investigation

Railway might support deployment queuing or mutex settings. Check:
- Railway project settings for "deployment strategy"
- Look for "serial deployments" or "queue deployments" option

### 2. GitHub Webhook Idempotency
**Status:** 🔍 Needs checking

Verify GitHub webhook configuration:

```bash
# Check GitHub webhooks
gh api repos/banodoco/brain-of-bdnc/hooks
```

Look for:
- Only ONE Railway webhook configured
- No duplicate webhook URLs
- Webhook has proper secret configured

**To check manually:**
1. Go to GitHub repo → Settings → Webhooks
2. Verify only ONE Railway webhook exists
3. Check "Recent Deliveries" for duplicate events

### 3. Reduce Restart Retries
**Status:** ⚠️ Consider adjusting

Current config:
```json
{
  "deploy": {
    "restartPolicyMaxRetries": 10
  }
}
```

**Recommendation:** Reduce to 3
```json
{
  "deploy": {
    "restartPolicyMaxRetries": 3
  }
}
```

**Why:**
- 10 retries can cause extended crash loops
- With health checks, fewer retries needed
- Faster to detect real deployment failures

### 4. Add Deployment Lock (Application-Level)
**Status:** 💡 Optional advanced solution

Could implement a distributed lock using Supabase:

```python
# On bot startup
deployment_id = os.getenv('RAILWAY_DEPLOYMENT_ID')
lock_acquired = await try_acquire_deployment_lock(deployment_id)

if not lock_acquired:
    logger.error("Another instance is already running, exiting")
    sys.exit(1)
```

**Pros:**
- Guarantees only one instance runs
- Works even if Railway triggers duplicate deploys

**Cons:**
- Adds complexity
- Requires careful lock cleanup on crashes
- May not be necessary with health checks

## Monitoring for Duplicates

### Daily Check
```bash
python scripts/debug.py deployments | grep "DUPLICATE"
```

If you see duplicates, investigate:
1. Check GitHub webhook deliveries
2. Check Railway project logs for retry triggers
3. Verify health checks are working

### After Each Deployment
```bash
# Check if only one bot is running
railway logs | grep "Bot is ready"
```

Should see ONE "Bot is ready" message per deployment.

### Real-Time Monitoring
```bash
# Watch for deployment starts
railway logs --follow | grep "Starting deployment"
```

If you see multiple deployment IDs in quick succession, investigate.

## What to Do If Duplicates Occur

1. **Immediate:** Cancel extra deployments in Railway dashboard
2. **Wait:** Give 1-2 hours for Discord rate limit to expire
3. **Investigate:** Check GitHub webhooks and Railway logs
4. **Verify:** Ensure health checks are configured
5. **Redeploy:** Once clear, redeploy from Railway dashboard (not GitHub)

## Verification

Current status can be verified with:

```bash
# Check Railway config has health checks
cat railway.json | grep healthcheck

# Should show:
#   "healthcheckPath": "/ready",
#   "healthcheckTimeout": 300

# Check health endpoint is working
curl https://brain-of-bdnc-production.up.railway.app/ready

# Should return:
#   {"status": "ready", "deployment_id": "...", ...}
```

## Future Improvements

1. **Implement Railway deployment hooks**
   - Add a pre-deploy script that checks for running instances
   - Gracefully shut down old instance before starting new

2. **Add deployment notification**
   - Post to Discord when deployment starts/completes
   - Team visibility into deployment status

3. **Webhook validation**
   - Verify GitHub webhook signatures
   - Reject duplicate webhook deliveries within 1 minute

4. **Deployment dashboard**
   - Web UI showing current deployment status
   - Alert if multiple instances detected

## References

- Health check implementation: `src/common/health_server.py`
- Railway config: `railway.json`
- Diagnostic tool: `scripts/debug.py`
- Incident analysis: `docs/deployment-troubleshooting.md`
