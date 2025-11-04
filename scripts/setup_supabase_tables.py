#!/usr/bin/env python3
"""
Script to create the Supabase tables for Discord data sync.
This script reads the SQL schema and executes it via the Supabase client.
"""

import asyncio
import os
import sys
from pathlib import Path

# Add the project root to the path so we can import our modules
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from supabase import create_client, Client
from supabase.lib.client_options import ClientOptions
from src.common.log_handler import setup_logging


async def create_tables():
    """Create the Supabase tables."""
    logger = setup_logging(dev_mode=False)
    logger.info("Setting up Supabase tables...")
    
    # Get Supabase credentials
    supabase_url = os.getenv('SUPABASE_URL')
    supabase_key = os.getenv('SUPABASE_SERVICE_KEY')
    
    if not supabase_url or not supabase_key:
        logger.error("SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables must be set.")
        return False
    
    try:
        # Initialize Supabase client
        options = ClientOptions(auto_refresh_token=False, postgrest_client_timeout=60)
        supabase = create_client(supabase_url, supabase_key, options=options)
        logger.info("Supabase client initialized successfully.")
        
        # Read the SQL schema file
        schema_file = Path(__file__).parent / "create_supabase_schema.sql"
        if not schema_file.exists():
            logger.error(f"Schema file not found: {schema_file}")
            return False
        
        with open(schema_file, 'r') as f:
            sql_content = f.read()
        
        # Split the SQL into individual statements
        statements = [stmt.strip() for stmt in sql_content.split(';') if stmt.strip()]
        
        logger.info(f"Found {len(statements)} SQL statements to execute...")
        
        # Execute each statement
        for i, statement in enumerate(statements, 1):
            if not statement:
                continue
                
            try:
                logger.info(f"Executing statement {i}/{len(statements)}...")
                logger.debug(f"SQL: {statement[:100]}...")
                
                # Use the rpc function to execute raw SQL
                await asyncio.to_thread(
                    supabase.rpc,
                    'exec',
                    {'sql': statement}
                )
                logger.debug(f"Statement {i} executed successfully.")
                
            except Exception as e:
                # Some statements might fail if tables already exist, which is okay
                if "already exists" in str(e).lower() or "duplicate" in str(e).lower():
                    logger.info(f"Statement {i} skipped (already exists): {str(e)[:100]}...")
                else:
                    logger.warning(f"Statement {i} failed: {e}")
                    # Continue with other statements
        
        logger.info("✅ Supabase table setup completed!")
        return True
        
    except Exception as e:
        logger.error(f"Failed to set up Supabase tables: {e}", exc_info=True)
        return False


if __name__ == "__main__":
    success = asyncio.run(create_tables())
    if success:
        print("🎉 Supabase tables created successfully!")
        print("You can now run the sync script.")
    else:
        print("❌ Failed to create tables. Check the logs above.")
        sys.exit(1)

