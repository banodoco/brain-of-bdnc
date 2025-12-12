"""Common constants used throughout the application."""
import os

# Storage backend constant (Supabase only)
STORAGE_SUPABASE = 'supabase'

def get_storage_backend(backend_arg: str = None) -> str:
    """
    Get the storage backend to use.
    Always returns 'supabase' - SQLite has been removed.
    Kept for backwards compatibility with existing code.
    """
    return STORAGE_SUPABASE
