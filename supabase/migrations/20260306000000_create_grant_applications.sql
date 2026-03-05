CREATE TABLE IF NOT EXISTS grant_applications (
    id SERIAL PRIMARY KEY,
    thread_id BIGINT NOT NULL UNIQUE,
    applicant_id BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'reviewing',
    gpu_type TEXT,
    recommended_hours NUMERIC,
    gpu_rate_usd NUMERIC,
    total_cost_usd NUMERIC,
    sol_amount NUMERIC,
    sol_price_usd NUMERIC,
    wallet_address TEXT,
    tx_signature TEXT,
    llm_assessment TEXT,
    thread_content TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    approved_at TIMESTAMPTZ,
    paid_at TIMESTAMPTZ,
    rejected_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_grant_applications_thread_id ON grant_applications(thread_id);
CREATE INDEX IF NOT EXISTS idx_grant_applications_applicant_id ON grant_applications(applicant_id);
CREATE INDEX IF NOT EXISTS idx_grant_applications_status ON grant_applications(status);

ALTER TABLE grant_applications ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow service role full access on grant_applications"
    ON grant_applications
    FOR ALL
    USING (true)
    WITH CHECK (true);
