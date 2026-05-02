# Discord Bot Deployment

## Single-replica constraint

brain-of-bndc must run as a single process.

Multiple replicas would break the in-memory `_pending_messages` cache, the MP2 approval poll loop concurrency assumptions, and Discord event delivery semantics. A second bot process would receive the same Discord events and could duplicate role grants, message handling, and approval recovery work.

The current deployment matches this constraint:

- `Procfile` runs one process: `web: python main.py`
- `railway.json` uses one start command: `python main.py`
- The codebase does not use `AutoShardedBot`, `num_shards`, or cluster coordination.

The MP2 `/get-approved` approval poller relies on those properties. It uses an in-process `asyncio.Lock`, an in-memory `_pending_messages` cache, and a startup-only Discord reconciliation pass. Do not run multiple bot replicas with this design.

Because the deployment is single-process, MP2 deliberately does not use a durable database-side posting lease. The poller's pre-send `pending_intros` lookup and the startup reconciliation pass cover the single-replica crash windows without introducing cross-process lease behavior.

To scale beyond one process, implement Discord sharding with `AutoShardedBot` and migrate `_pending_messages` to a shared cache or database-backed coordination layer before increasing replica count.
