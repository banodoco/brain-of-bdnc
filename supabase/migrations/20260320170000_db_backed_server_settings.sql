-- Make server_config the source of truth for runtime writes.

ALTER TABLE server_config
    ADD COLUMN IF NOT EXISTS write_enabled BOOLEAN DEFAULT FALSE;

UPDATE server_config
SET write_enabled = TRUE
WHERE enabled = TRUE
  AND COALESCE(write_enabled, FALSE) = FALSE;

ALTER TABLE server_config
    ALTER COLUMN write_enabled SET DEFAULT FALSE;

UPDATE server_config
SET write_enabled = FALSE
WHERE write_enabled IS NULL;

ALTER TABLE server_config
    ALTER COLUMN write_enabled SET NOT NULL;

CREATE OR REPLACE FUNCTION enforce_writable_server_guild()
RETURNS TRIGGER AS $$
DECLARE
    guild_is_writable BOOLEAN;
BEGIN
    IF NEW.guild_id IS NULL THEN
        RAISE EXCEPTION 'guild_id is required for writes to %', TG_TABLE_NAME;
    END IF;

    SELECT (enabled AND write_enabled)
    INTO guild_is_writable
    FROM server_config
    WHERE guild_id = NEW.guild_id;

    IF guild_is_writable IS DISTINCT FROM TRUE THEN
        RAISE EXCEPTION 'Writes are disabled for guild % on table %', NEW.guild_id, TG_TABLE_NAME;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE
    target RECORD;
BEGIN
    FOR target IN
        SELECT c.table_name
        FROM information_schema.columns c
        JOIN information_schema.tables t
          ON t.table_schema = c.table_schema
         AND t.table_name = c.table_name
        WHERE c.table_schema = 'public'
          AND c.column_name = 'guild_id'
          AND c.table_name <> 'server_config'
          AND t.table_type = 'BASE TABLE'
    LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS %I ON %I',
            'trg_enforce_writable_server_' || target.table_name,
            target.table_name
        );
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE INSERT OR UPDATE ON %I FOR EACH ROW EXECUTE FUNCTION enforce_writable_server_guild()',
            'trg_enforce_writable_server_' || target.table_name,
            target.table_name
        );
    END LOOP;
END $$;
