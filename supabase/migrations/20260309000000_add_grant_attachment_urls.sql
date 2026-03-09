ALTER TABLE grant_applications ADD COLUMN IF NOT EXISTS attachment_urls JSONB DEFAULT '[]'::JSONB;
