ALTER TABLE grant_applications ADD COLUMN IF NOT EXISTS payment_status TEXT NOT NULL DEFAULT 'none';
