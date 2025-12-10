-- SQL script to create system_logs table in Supabase
-- This table stores application logs for debugging and monitoring

-- System Logs Table
CREATE TABLE IF NOT EXISTS system_logs (
    id BIGSERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    level TEXT NOT NULL,  -- DEBUG, INFO, WARNING, ERROR, CRITICAL
    logger_name TEXT NOT NULL,
    message TEXT NOT NULL,
    module TEXT,
    function_name TEXT,
    line_number INTEGER,
    exception TEXT,  -- Stack trace if error
    extra JSONB DEFAULT '{}'::jsonb,  -- Additional context
    hostname TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_system_logs_timestamp ON system_logs(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs(level);
CREATE INDEX IF NOT EXISTS idx_system_logs_logger_name ON system_logs(logger_name);
CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at DESC);

-- Composite index for common query patterns
CREATE INDEX IF NOT EXISTS idx_system_logs_level_timestamp ON system_logs(level, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_system_logs_logger_timestamp ON system_logs(logger_name, timestamp DESC);

-- Full-text search for log messages
CREATE INDEX IF NOT EXISTS idx_system_logs_message_fts ON system_logs USING gin(to_tsvector('english', message));

-- Enable RLS
ALTER TABLE system_logs ENABLE ROW LEVEL SECURITY;

-- Create policy for service role access
DROP POLICY IF EXISTS "Service role can access all system_logs" ON system_logs;
CREATE POLICY "Service role can access all system_logs" ON system_logs
    FOR ALL USING (true);

-- Auto-cleanup: Create a function to delete old logs (keep last 7 days by default)
CREATE OR REPLACE FUNCTION cleanup_old_logs(days_to_keep INTEGER DEFAULT 7)
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM system_logs
    WHERE timestamp < NOW() - (days_to_keep || ' days')::INTERVAL;
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Create a view for recent errors
CREATE OR REPLACE VIEW recent_errors AS
SELECT 
    id,
    timestamp,
    level,
    logger_name,
    message,
    module,
    function_name,
    exception
FROM system_logs
WHERE level IN ('ERROR', 'CRITICAL')
AND timestamp >= NOW() - INTERVAL '24 hours'
ORDER BY timestamp DESC;

-- Create a view for log summary statistics
CREATE OR REPLACE VIEW log_stats AS
SELECT 
    DATE_TRUNC('hour', timestamp) as hour,
    level,
    COUNT(*) as count
FROM system_logs
WHERE timestamp >= NOW() - INTERVAL '24 hours'
GROUP BY DATE_TRUNC('hour', timestamp), level
ORDER BY hour DESC, level;
