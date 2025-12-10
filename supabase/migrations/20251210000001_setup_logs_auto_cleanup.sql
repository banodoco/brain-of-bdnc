-- Auto-cleanup for system_logs: Delete logs older than 48 hours
-- Run this in Supabase SQL Editor to enable automatic cleanup

-- First, enable the pg_cron extension (if not already enabled)
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Grant usage to postgres user
GRANT USAGE ON SCHEMA cron TO postgres;

-- Create the cleanup function (48 hours retention)
CREATE OR REPLACE FUNCTION cleanup_old_logs_48h()
RETURNS INTEGER AS $$
DECLARE
    deleted_count INTEGER;
BEGIN
    DELETE FROM system_logs
    WHERE timestamp < NOW() - INTERVAL '48 hours';
    
    GET DIAGNOSTICS deleted_count = ROW_COUNT;
    
    -- Log the cleanup (optional - creates a meta log entry)
    IF deleted_count > 0 THEN
        INSERT INTO system_logs (level, logger_name, message, module, hostname)
        VALUES ('INFO', 'AutoCleanup', 'Deleted ' || deleted_count || ' old log entries', 'pg_cron', 'supabase');
    END IF;
    
    RETURN deleted_count;
END;
$$ LANGUAGE plpgsql;

-- Schedule the cleanup to run every hour
-- This ensures logs are cleaned up regularly without building up
SELECT cron.schedule(
    'cleanup-system-logs',           -- job name
    '0 * * * *',                     -- every hour at minute 0
    'SELECT cleanup_old_logs_48h()' -- the function to run
);

-- To verify the job was created:
-- SELECT * FROM cron.job;

-- To manually run cleanup now:
-- SELECT cleanup_old_logs_48h();

-- To remove the scheduled job (if needed):
-- SELECT cron.unschedule('cleanup-system-logs');
