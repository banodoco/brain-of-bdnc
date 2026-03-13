-- Store the questions/late-entries thread ID so it survives bot restarts
ALTER TABLE competitions ADD COLUMN IF NOT EXISTS questions_thread_id BIGINT;
