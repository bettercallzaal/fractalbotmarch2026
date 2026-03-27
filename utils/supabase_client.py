"""Singleton Supabase client used by all cogs."""

import os
from supabase import create_client, Client

_client: Client | None = None


def get_supabase() -> Client:
    """Return (and lazily create) the shared Supabase client.

    Requires SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables.
    The service_role key bypasses Row Level Security so the bot can
    read/write all tables without restriction.
    """
    global _client
    if _client is None:
        url = os.getenv('SUPABASE_URL')
        key = os.getenv('SUPABASE_SERVICE_KEY')
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables are required")
        _client = create_client(url, key)
    return _client
