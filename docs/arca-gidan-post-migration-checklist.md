# Arca Gidan Post-Migration Checklist

## SQL / RLS Fixes (Must Fix Before Go-Live)

- [x] **Fix: content_records trigger needs SECURITY DEFINER**
  - `ag_submissions` insert/update/delete triggers write to `content_records`, but `content_records` only has a public SELECT policy
  - Trigger functions run as the calling user's role, so normal AG users can't write to `content_records`
  - Fix: make the content-record sync trigger functions `SECURITY DEFINER` or add INSERT/UPDATE/DELETE RLS policies on `content_records` for authenticated users
  - Location: `20260322000000_merge_arca_gidan_into_shared_supabase.sql` lines ~844, ~911, ~920

- [x] **Fix: vote triggers can't update other users' submissions**
  - `ag_update_submission_vote_count()` fires on vote insert/delete and updates `ag_submissions.vote_count`
  - But submission UPDATE RLS restricts updates to the submission owner (while status=submitted) or admins
  - A voter casting a vote triggers an update to someone else's submission row — blocked by RLS
  - Fix: make `ag_update_submission_vote_count()` `SECURITY DEFINER` or add a specific RLS policy allowing vote_count updates via trigger
  - Location: `20260322000000_merge_arca_gidan_into_shared_supabase.sql` lines ~975, ~1006, ~1577

- [x] **Fix: public vote count views use SECURITY INVOKER against restricted vote RLS**
  - Judge-multiplier helper functions are SECURITY INVOKER
  - `ag_submission_details` and `ag_public_vote_counts` views call these functions
  - But `ag_votes` SELECT is limited to `auth.uid() = user_id OR ag_is_admin()`
  - Public/anon users can't see aggregate vote counts
  - Fix: make the weighted-count helper functions `SECURITY DEFINER` or open `ag_votes` SELECT for aggregate reads
  - Location: `20260322000000_merge_arca_gidan_into_shared_supabase.sql` lines ~1155, ~1195, ~1328, ~1372, ~1590

## Admin Scripts (Fix Before Using)

- [x] **Fix: review-votes.mjs references old field names**
  - Prints `vote.voted_for_submission` but new view exposes `submission_title`
  - Location: `arca-gidan/review-votes.mjs` line ~90

- [x] **Fix: generate_competition_report.py references old schema**
  - Still reads `competition["title"]`, `submission_deadline`, `voting_deadline`, `status`, vote `user_id`
  - These fields don't exist in the new `ag_` tables
  - Location: `arca-gidan/scripts/generate_competition_report.py` lines ~110, ~223
  - Also duplicated at: `arca-gidan/scripts/reports/generate_competition_report.py`

## Live Configuration (Manual Steps)

- [ ] **Enable Discord OAuth on shared Supabase**
  - Dashboard -> Authentication -> Providers -> Discord
  - Shared project: `ujlwuvkrxlvoswwkerdf`
  - Redirect URL: `https://ujlwuvkrxlvoswwkerdf.supabase.co/auth/v1/callback`
  - Add callback URL to Discord app configuration

- [x] **Apply the migration SQL**
  - Run `20260322000000_merge_arca_gidan_into_shared_supabase.sql` on shared instance

- [x] **Import historical auth.users with original UUIDs**
  - Must use direct SQL into `auth.users` (not create-user API)
  - Preserves all FK references in ag_submissions, ag_votes, etc.
  - Link each with `ag_link_auth_identity(auth_uuid, discord_member_id, email)`

- [x] **Import historical data into ag_ tables**
  - Disable vote triggers during import
  - Insert in FK order: competitions -> submissions -> votes -> analytics -> reviews -> config -> admins
  - Re-enable triggers

- [x] **Migrate profile-pictures storage bucket**
  - Create bucket on shared instance
  - Copy files preserving folder layout

## Verification (After Migration)

- [x] **Row count comparison**
  ```sql
  select count(*) from ag_competitions;
  select count(*) from ag_submissions;
  select count(*) from ag_votes;
  select count(*) from ag_submission_analytics;
  select count(*) from ag_user_identities;
  select count(*) from ag_profiles;
  select count(*) from content_records where source_app = 'arca-gidan';
  ```

- [ ] **Auth flow end-to-end**
  - Discord OAuth login creates auth.users entry
  - `handle_new_user()` trigger creates/links discord_members + ag_user_identities
  - `ag_profiles` view returns correct data for logged-in user

- [ ] **Submission flow**
  - Can create new submission (triggers content_record creation)
  - Can update own submission
  - Can delete own submission (triggers content_record deletion)

- [ ] **Voting flow**
  - Can cast vote (triggers vote_count update on submission)
  - Can remove vote (triggers vote_count decrement)
  - Cannot vote for own submission
  - Max votes per competition enforced

- [x] **Public views**
  - `ag_public_vote_counts` returns correct weighted counts for anon users
  - `ag_submission_details` returns profile data via ag_profiles
  - Winners page loads correctly

- [ ] **Fraud detection**
  - `ag_calculate_vote_confidence()` uses `discord_created_at` for account age
  - `ag_fraud_detection_summary` view returns data for admins
  - Admin dashboard functional

- [ ] **Profile management**
  - Can update bio, website_url, instagram_url, twitter via `ag_update_profile`
  - Avatar upload goes to `stored_avatar_url` (not `avatar_url`)
  - `ag_profiles` returns COALESCE(stored_avatar_url, avatar_url)
  - Bot sync does NOT overwrite user-uploaded avatars

- [ ] **Cross-project regression check**
  - brain-of-bndc bot still syncs discord_members normally
  - banodoco-website loads without errors
  - ados reads discord_members without issues

- [x] **Old Arca Gidan instance**
  - Kept running as read-only fallback (DO NOT remove)
