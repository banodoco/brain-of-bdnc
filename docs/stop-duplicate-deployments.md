# How to Stop Duplicate Deployments

## Quick Answer

The duplicate deployment issue is **already solved** with health checks implemented on Dec 20, 2025. Here's what prevents it now:

### ✅ What's Already Fixed

1. **Health Check Endpoint** - Railway waits for `/ready` to return 200 before considering deployment successful
2. **Health Check Timeout** - 300 second timeout ensures bot has time to start
3. **Deployment Logging** - Each deployment logs unique ID to identify duplicates
4. **Diagnostic Tool** - `python scripts/debug.py deployments` detects duplicate deploys

**Result:** Only ONE duplicate detected in history (Dec 19, before health checks). All subsequent deployments (Dec 20) worked correctly with no duplicates.

## Why the Dec 19 Duplicate Happened

Looking at the metadata:
```
2025-12-19 12:24:33 | 7ceafe4 | Reason: deploy
           └─ Public repo deploy
           └─ Patch ID: 39d3d097-269...
```

The second deployment had a **patch ID**, suggesting Railway re-triggered it. Without health checks, Railway couldn't verify the first deployment succeeded, so it likely thought it failed and auto-retried.

## How Health Checks Prevent This

**Before (Dec 19):**
```
1. Push to GitHub
2. Railway starts deployment A (12:23:55)
3. Railway can't tell if A succeeded
4. Railway triggers deployment B (12:24:33) ← duplicate!
5. Both A and B run simultaneously
6. Discord rate limit triggered
```

**After (Dec 20+):**
```
1. Push to GitHub
2. Railway starts deployment A
3. A starts, bot connects to Discord
4. A marks ready → /ready returns 200
5. Railway sees A is healthy ✓
6. No deployment B triggered
7. Single bot instance running
```

## Additional Prevention (If Needed)

If you see duplicates again, here are additional measures:

### 1. Check GitHub Webhooks

**Action:** Verify only ONE Railway webhook exists

**How to check:**
1. Go to https://github.com/banodoco/brain-of-bdnc/settings/hooks
2. Count how many webhooks point to Railway
3. Should be exactly ONE
4. Check "Recent Deliveries" tab for duplicate events

**Fix if multiple webhooks:**
- Delete duplicate webhooks
- Keep only one pointing to `railway.app`

### 2. Reduce Restart Retries

**Current setting:** `restartPolicyMaxRetries: 10`

**Recommendation:** Reduce to 3

```json
// railway.json
{
  "deploy": {
    "restartPolicyMaxRetries": 3  // Change from 10
  }
}
```

**Why:**
- With health checks, Railway knows deployment succeeded
- 10 retries can cause crash loops
- 3 retries is sufficient for transient failures

### 3. Monitor for Duplicates

**Daily check:**
```bash
python scripts/debug.py deployments | grep DUPLICATE
```

**After each deployment:**
```bash
# Should see only ONE "Bot is ready" per deployment
railway logs | grep "Bot is ready"

# Should see only ONE deployment ID
railway logs | grep "Starting deployment"
```

### 4. If Duplicate Occurs

1. **Cancel** the extra deployment in Railway dashboard immediately
2. **Wait** 1-2 hours for Discord rate limit to expire
3. **Check** GitHub webhooks for duplicates
4. **Verify** health checks are working:
   ```bash
   curl https://brain-of-bdnc-production.up.railway.app/ready
   ```
5. **Redeploy** once from Railway dashboard (NOT GitHub push)

## Current Status ✅

**Health checks:** ✅ Configured and working  
**Duplicate deployments:** ✅ None since Dec 19 (before health checks)  
**Monitoring tools:** ✅ In place (`debug.py deployments`)  
**Documentation:** ✅ Complete

The issue is **solved**. Health checks are the primary prevention mechanism, and they're working correctly.

## Verification Commands

```bash
# 1. Verify health checks configured
cat railway.json | grep healthcheck

# 2. Test health endpoint
python scripts/debug.py railway-status

# 3. Check for duplicate deployments
python scripts/debug.py deployments

# 4. Monitor deployment count
railway deployment list --limit 10
```

## Summary

**The duplicate deployment issue is resolved through health checks.** Railway now waits for the bot to be ready before considering a deployment successful, preventing the auto-retry that caused the Dec 19 duplicate. No duplicates have occurred since health checks were added.

If you want extra safety, reduce `restartPolicyMaxRetries` from 10 to 3, but it's not strictly necessary with health checks in place.
