## OpenMuse Workflow Uploader – Specification

### 1. Overview
This feature extends the existing *reacting* subsystem (see `src/features/reacting/`) by automating the upload of a user's **workflow** to OpenMuse when the bot owner reacts to a Discord message.

The process begins with a reaction event *triggered by a curator-level user*, continues through interactive DMs with the **author** (the person who owns the reacted message), aggregates content from surrounding messages, stores files in Supabase, generates metadata with Anthropic Claude, and finally notifies both the **triggering curator** and the **author**.

### 2. Trigger & Eligibility
1. **Reaction Watch-List** – The emoji ↔︎ action pair is **already configured** in `WATCHLIST_JSON`; no extra registration required.
2. **Author Opt-Out Check** – The feature executes *only if* `members.permission_to_curate ≠ 0` for the message author.
   • This nullable flag lives in the SQLite `members` table – see `src/common/db_handler.py` (`DatabaseHandler.update_member_permission_status`).
   • Setting it to `0` (False) opts the user out of future curation DMs.

### 3. Initial DM (Call-to-Action)
When eligible, the bot sends the author a DM (via `safe_send_message`) that contains:
• A short thank-you blurb and the original message jump-URL.
• Two Discord buttons:
  1. **"Upload workflow to OpenMuse"** (✅ confirm)
  2. **"I'm not interested"** (🚫 decline)

#### Interaction Handling
• **Decline** → set `members.permission_to_curate = 0`; ACK DM to the user; DM the curator (reacting user).
• **Confirm** → continue with the workflow pipeline (sections 4-9).
• Any failure to DM (Forbidden, timeout, etc.) is logged and surfaced to the curator via DM where possible.
• After a choice is made (or the view times out), **delete** the interactive DM to avoid clutter.

### 4. Collect Source Material
1. **Primary JSON Attachment** – The reacted-to message *must* contain a `.json` attachment representing the workflow. This file is uploaded to Supabase **workflows** bucket.
2. **Surrounding Context Messages** – Query Discord for all messages by the same author in the channel ±2 h 30 m around `message.created_at`.
   • Record their text content.
   • Collect *all* attachments (images/videos; skip other file types) → saved later.
• Assumes the curator reacted to the *correct* message (no fallback/lookup needed).
• Stop pulling once the ± 2 h 30 m window has been traversed – do **not** keep paginating indefinitely.

### 5. Generate Workflow Name
Feed the concatenated text content (capped so the total prompt ≤ **3 000 Claude tokens**) to **Claude** via `ClaudeClient.generate_chat_completion` with instructions:
```
Given the user's messages, propose an accurate, technical workflow name ≤ 36 characters. Prefer wording drawn directly from the user's text.
Return **ONLY** the name.
```
The returned string becomes `asset.name`.

### 6. Determine Model & Variant
Provide Claude with:
• The same message text.
• A JSON array containing all rows from the `models` table (id, name, variant).
Request a JSON response:
```json
{"model": "FooXL", "variant": "v2.1"}
```
Parse and write:
* `asset.lora_base_model = model`
* `asset.model_variant   = variant`
Retrieval of the `models` data happens via Supabase:
```python
models = await asyncio.to_thread(
    openmuse_interactor.supabase.table('models').select('*').execute
)
```
Handle paging if `.data` length == 1000 (Supabase default limit).

### 6.5 Ensure Author Profile Exists
Before the asset insert occurs, call `OpenMuseInteractor.find_or_create_profile(author)` (already used inside the attachment uploader) so that:
• If a Supabase **profile** row for this Discord user does not exist, it is created.
• The returned `profile_id_uuid` is stored for later use as `asset.user_id` and for any media uploads.

### 7. Persist Asset Record
Insert into `assets` table:
• `id`            – uuid (generated)
• `type`          – "workflow"
• `name`          – generated in step 5
• `creator`/`user_id` – author id
• `description`   – first 160 chars of primary message or blank
• `download_link` – public URL of workflow uploaded in step 4.1
• `admin_status`  – "Listed"
• `user_status`   – "Listed"
• Columns such as `lora_type`, `lora_link`, `description` **may be NULL** – the schema allows this.
• Remaining columns default/null as appropriate.

### 8. Upload Media & Create Relationships
For each attachment collected in step 4.2:
1. **Upload** to Supabase **videos** bucket using `OpenMuseInteractor.upload_discord_attachment` (reuse logic from `openmuse_uploader.py`).
2. **Insert** into `media` table (same helper already provides this).
3. **Link** rows in `asset_media`:
   • `is_primary = True` for the *first* media item; else False.
   • `status = "Listed"`.
• The workflow JSON itself is already referenced via `asset.download_link`; the `asset_media` links cover *additional* videos/images.

### 9. Notifications
• **Author DM** –
  "Your workflow has been uploaded! You can edit it here: <workflow_url>"
• **Admin DM**  – identical summary plus author & asset id.
The URL sent to both parties must be in the form:
`https://openmuse.ai/assets/loras/{asset_uuid}`

### 10. Error Handling & Edge Cases
• All Discord API operations wrapped with `safe_send_message` or `RateLimiter`.
• Missing `.json` → DM author & curator, abort.
• Supabase failures → DM author & admin, keep logs.
• Claude errors → fallback name = "Untitled Workflow", skip model fields.
• Time-window search returns >200 messages → truncate to most recent 200.

### 11. Rate Limiting & Retries
• External API calls (Supabase, Claude) follow exponential-backoff (see `ClaudeClient`).
• One in-flight workflow per user to avoid duplication (track with in-memory `asyncio.Lock` keyed by user-id).

### 12. Security & Privacy
• Uploaded URLs use signed time-limited tokens unless asset is published.
• Opt-out honoured via `members.permission_to_curate`.

### 13. Testing Checklist
- ✅ Reaction triggers DM only when permission_to_curate≠0.
- ✅ "I'm not interested" sets opt-out and notifies curator.
- ✅ Workflow JSON uploaded & asset row created.
- ✅ Attachments uploaded and linked.
- ✅ DMs sent to author & admin with valid URLs.

---
_Last updated: {{DATE}}_ 