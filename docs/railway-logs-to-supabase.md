# Sending Railway Logs to Supabase

Railway platform logs (deployments, restarts, health checks) are currently separate from application logs. Here's how to consolidate them:

## Option 1: Railway Log Drain (Recommended)

Railway supports log drains that can send logs to external services.

1. **Create a Supabase Edge Function** to receive Railway logs:
   ```sql
   -- Create table for Railway platform logs
   CREATE TABLE railway_logs (
       id BIGSERIAL PRIMARY KEY,
       timestamp TIMESTAMPTZ NOT NULL,
       deployment_id TEXT,
       service_id TEXT,
       environment TEXT,
       log_type TEXT, -- 'deploy', 'build', 'runtime', 'restart'
       message TEXT,
       metadata JSONB,
       created_at TIMESTAMPTZ DEFAULT NOW()
   );
   
   CREATE INDEX idx_railway_logs_timestamp ON railway_logs(timestamp DESC);
   CREATE INDEX idx_railway_logs_type ON railway_logs(log_type);
   ```

2. **Set up Railway Webhook** (via Railway dashboard):
   - Go to Project Settings → Webhooks
   - Add webhook URL pointing to your Supabase edge function
   - Select events: `deployment.started`, `deployment.completed`, `deployment.failed`

3. **Create Edge Function** to parse and store:
   ```typescript
   // supabase/functions/railway-webhook/index.ts
   import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
   import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'

   serve(async (req) => {
     const payload = await req.json()
     
     const supabase = createClient(
       Deno.env.get('SUPABASE_URL')!,
       Deno.env.get('SUPABASE_SERVICE_KEY')!
     )
     
     await supabase.from('railway_logs').insert({
       timestamp: payload.timestamp,
       deployment_id: payload.deploymentId,
       log_type: payload.type,
       message: payload.message,
       metadata: payload
     })
     
     return new Response('OK', { status: 200 })
   })
   ```

## Option 2: Periodic Sync Script

Create a cron job that periodically fetches Railway logs and stores them:

```python
# scripts/sync_railway_logs.py
import subprocess
import json
from supabase import create_client
import os

def fetch_and_store_railway_logs():
    # Fetch logs via Railway CLI
    result = subprocess.run(
        ['railway', 'logs', '--lines', '1000', '--json'],
        capture_output=True, text=True
    )
    
    logs = result.stdout.strip().split('\n')
    
    supabase = create_client(
        os.getenv('SUPABASE_URL'),
        os.getenv('SUPABASE_SERVICE_KEY')
    )
    
    for log_line in logs:
        log = json.loads(log_line)
        supabase.table('railway_logs').insert({
            'timestamp': log['timestamp'],
            'message': log['message'],
            'metadata': log
        }).execute()

if __name__ == '__main__':
    fetch_and_store_railway_logs()
```

Run via cron: `0 * * * * python /app/scripts/sync_railway_logs.py`

## Option 3: Real-time Streaming (Advanced)

Use Railway's GraphQL API to stream logs in real-time:

```python
# Add to main.py or separate service
import asyncio
import websockets
import json

async def stream_railway_logs():
    railway_token = os.getenv('RAILWAY_TOKEN')
    
    async with websockets.connect(
        'wss://backboard.railway.app/graphql',
        extra_headers={'Authorization': f'Bearer {railway_token}'}
    ) as ws:
        # Subscribe to deployment logs
        await ws.send(json.dumps({
            'type': 'start',
            'payload': {
                'query': '''
                    subscription {
                        deploymentLogs(deploymentId: "...") {
                            message
                            timestamp
                        }
                    }
                '''
            }
        }))
        
        async for message in ws:
            data = json.loads(message)
            # Store to Supabase
```

## Recommended Approach

For your use case, **Option 1 (Railway Webhooks)** is best because:
- Real-time delivery
- No additional services needed
- Railway handles retry logic
- Captures deployment events that trigger restarts

This would have helped us immediately see what caused the restart at 12:25:24 today.
