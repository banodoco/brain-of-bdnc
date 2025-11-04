"""Common constants used throughout the application."""
import os

# Database
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'data')
PROD_DATABASE_PATH = os.path.join(DATA_DIR, 'production.db')
DEV_DATABASE_PATH = os.path.join(DATA_DIR, 'dev.db')

# Storage backend constants
STORAGE_SQLITE = 'sqlite'
STORAGE_SUPABASE = 'supabase'
STORAGE_BOTH = 'both'

def get_database_path(dev_mode: bool = False) -> str:
    """Get the appropriate database path based on mode."""
    # Ensure data directory exists
    os.makedirs(DATA_DIR, exist_ok=True)
    return DEV_DATABASE_PATH if dev_mode else PROD_DATABASE_PATH 

def get_storage_backend(backend_arg: str = None) -> str:
    """
    Get the storage backend to use.
    Priority: CLI arg > env var > default (sqlite)
    """
    if backend_arg:
        return backend_arg.lower()
    
    env_backend = os.getenv('STORAGE_BACKEND', 'sqlite').lower()
    if env_backend in [STORAGE_SQLITE, STORAGE_SUPABASE, STORAGE_BOTH]:
        return env_backend
    
    return STORAGE_SQLITE 