"""
One-time migration script: reads all JSON data files and inserts into Supabase.

Usage:
    1. Create the Supabase project and run scripts/create_tables.sql in the SQL Editor.
    2. Set environment variables (or add to .env):
        SUPABASE_URL=https://your-project.supabase.co
        SUPABASE_SERVICE_KEY=eyJ...  (service_role key, bypasses RLS)
    3. Run:
        python scripts/migrate_to_supabase.py

The script is idempotent -- safe to run multiple times.  It uses upserts
so existing rows are updated rather than duplicated.

Prerequisites:
    pip install supabase python-dotenv
"""

import json
import os
import sys

# Allow running from repo root or from scripts/
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
sys.path.insert(0, ROOT_DIR)

from dotenv import load_dotenv

# Load .env from the repo root
load_dotenv(os.path.join(ROOT_DIR, ".env"))

from supabase import create_client, Client

# ── Config ──────────────────────────────────────────────────────

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY environment variables are required.")
    print("Set them in your .env file or export them before running this script.")
    sys.exit(1)

DATA_DIR = os.path.join(ROOT_DIR, "data")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# ── Helpers ─────────────────────────────────────────────────────

def load_json(filename: str):
    """Load a JSON file from the data/ directory.  Returns None if missing."""
    filepath = os.path.join(DATA_DIR, filename)
    if not os.path.exists(filepath):
        return None
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def batch_upsert(table: str, rows: list, on_conflict: str, batch_size: int = 50):
    """Upsert rows in batches to stay within request-size limits."""
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        supabase.table(table).upsert(batch, on_conflict=on_conflict).execute()


# ── Wallets ─────────────────────────────────────────────────────

def migrate_wallets():
    """Migrate wallets.json and names_to_wallets.json into the wallets table."""
    print("=== Migrating wallets ===")

    # 1. Discord-ID-keyed wallets (wallets.json)
    wallets = load_json("wallets.json") or {}
    discord_rows = []
    for discord_id, address in wallets.items():
        discord_rows.append({
            "discord_id": discord_id,
            "wallet_address": address,
            "source": "discord_id",
        })

    if discord_rows:
        batch_upsert("wallets", discord_rows, on_conflict="discord_id")
        print(f"  Upserted {len(discord_rows)} discord_id wallet rows")

    # 2. Name-keyed wallets (names_to_wallets.json)
    names = load_json("names_to_wallets.json") or {}
    name_rows = []
    for name, address in names.items():
        name_rows.append({
            "display_name": name,
            "wallet_address": address or "",
            "source": "name",
        })

    if name_rows:
        # Name rows have no discord_id so we use display_name for dedup.
        # Since display_name is not UNIQUE in the schema, we delete-then-insert
        # for idempotency: first remove all 'name'-source rows, then re-insert.
        supabase.table("wallets").delete().eq("source", "name").execute()
        for i in range(0, len(name_rows), 50):
            batch = name_rows[i : i + 50]
            supabase.table("wallets").insert(batch).execute()
        print(f"  Inserted {len(name_rows)} name wallet rows")

    print()


# ── Proposals + Votes ───────────────────────────────────────────

def migrate_proposals():
    """Migrate proposals.json into proposals + proposal_votes tables."""
    print("=== Migrating proposals ===")

    data = load_json("proposals.json")
    if not data:
        print("  No proposals data found\n")
        return

    # Store the _index_message_id in bot_metadata
    index_msg_id = data.get("_index_message_id")
    if index_msg_id:
        supabase.table("bot_metadata").upsert({
            "key": "proposals_index_message_id",
            "value": str(index_msg_id),
        }, on_conflict="key").execute()
        print(f"  Stored proposals_index_message_id = {index_msg_id}")

    proposals = data.get("proposals", {})
    vote_count = 0

    for pid, p in proposals.items():
        # Build proposal row
        row = {
            "id": int(pid),
            "title": p["title"],
            "description": p.get("description"),
            "proposal_type": p["type"],
            "author_id": p["author_id"],
            "thread_id": p.get("thread_id"),
            "message_id": p.get("message_id"),
            "status": p["status"],
            "options": p.get("options", []),
            "funding_amount": p.get("funding_amount"),
            "image_url": p.get("image_url"),
            "project_url": p.get("project_url"),
            "created_at": p["created_at"],
            "closed_at": p.get("closed_at"),
        }
        supabase.table("proposals").upsert(row, on_conflict="id").execute()

        # Normalize embedded votes dict into proposal_votes rows
        votes = p.get("votes", {})
        vote_rows = []
        for voter_id, vote_data in votes.items():
            if isinstance(vote_data, str):
                # Simple string vote (legacy format)
                value, weight = vote_data, 1.0
            else:
                value = vote_data["value"]
                weight = vote_data.get("weight", 1.0)
            vote_rows.append({
                "proposal_id": int(pid),
                "voter_id": voter_id,
                "vote_value": value,
                "weight": weight,
            })

        if vote_rows:
            supabase.table("proposal_votes").upsert(
                vote_rows, on_conflict="proposal_id,voter_id"
            ).execute()
            vote_count += len(vote_rows)

    print(f"  Upserted {len(proposals)} proposals")
    print(f"  Upserted {vote_count} proposal votes")

    # Remind user to reset the sequence
    next_id = data.get("next_id", max((int(k) for k in proposals), default=0) + 1)
    print(f"  NOTE: Run this SQL to reset the proposals sequence:")
    print(f"    SELECT setval('proposals_id_seq', {next_id}, false);")
    print()


# ── Fractal History ─────────────────────────────────────────────

def migrate_history():
    """Migrate history.json into fractal_sessions + fractal_rankings tables."""
    print("=== Migrating fractal history ===")

    data = load_json("history.json")
    if not data:
        print("  No history data found\n")
        return

    fractals = data.get("fractals", [])
    ranking_count = 0

    for f in fractals:
        # Insert session
        session_row = {
            "id": f["id"],
            "group_name": f["group_name"],
            "facilitator_id": str(f["facilitator_id"]),
            "facilitator_name": f["facilitator_name"],
            "fractal_number": f.get("fractal_number"),
            "group_number": f.get("group_number"),
            "guild_id": str(f["guild_id"]),
            "thread_id": str(f.get("thread_id", "")),
            "completed_at": f["completed_at"],
        }
        supabase.table("fractal_sessions").upsert(
            session_row, on_conflict="id"
        ).execute()

        # Normalize embedded rankings array into fractal_rankings rows
        rankings = f.get("rankings", [])
        ranking_rows = []
        for i, r in enumerate(rankings):
            ranking_rows.append({
                "session_id": f["id"],
                "user_id": str(r["user_id"]),
                "display_name": r["display_name"],
                "level": r["level"],
                "respect": r.get("respect", 0),
                "rank_position": i + 1,
            })

        if ranking_rows:
            supabase.table("fractal_rankings").upsert(
                ranking_rows, on_conflict="session_id,user_id"
            ).execute()
            ranking_count += len(ranking_rows)

    max_id = max((f["id"] for f in fractals), default=0)
    print(f"  Upserted {len(fractals)} fractal sessions")
    print(f"  Upserted {ranking_count} fractal rankings")
    print(f"  NOTE: Run this SQL to reset the fractal_sessions sequence:")
    print(f"    SELECT setval('fractal_sessions_id_seq', {max_id + 1}, false);")
    print()


# ── Intros ──────────────────────────────────────────────────────

def migrate_intros():
    """Migrate intros.json into the intros table."""
    print("=== Migrating intros ===")

    data = load_json("intros.json")
    if not data:
        print("  No intros data found\n")
        return

    rows = []
    for discord_id, entry in data.items():
        rows.append({
            "discord_id": discord_id,
            "intro_text": entry["text"],
            "message_id": str(entry.get("message_id", "")),
            "posted_at": entry.get("timestamp"),
        })

    if rows:
        supabase.table("intros").upsert(
            rows, on_conflict="discord_id"
        ).execute()

    print(f"  Upserted {len(rows)} intros\n")


# ── Events ──────────────────────────────────────────────────────

def migrate_events():
    """Migrate events.json into the events table."""
    print("=== Migrating events ===")

    data = load_json("events.json")
    if not data:
        print("  No events data found\n")
        return

    events = data.get("events", {})
    if not events:
        print("  Events dict is empty (no events to migrate)\n")
        return

    rows = []
    for slug, ev in events.items():
        rows.append({
            "slug": slug,
            "name": ev["name"],
            "day_of_week": ev["day"],
            "event_time": ev["time"],
            "timezone": ev.get("timezone", "UTC"),
            "channel_id": str(ev["channel_id"]),
            "created_by": str(ev.get("created_by", "")),
            "last_reminded_24h": ev.get("last_reminded_24h"),
            "last_reminded_6h": ev.get("last_reminded_6h"),
            "last_reminded_1h": ev.get("last_reminded_1h"),
        })

    if rows:
        supabase.table("events").upsert(
            rows, on_conflict="slug"
        ).execute()

    print(f"  Upserted {len(rows)} events\n")


# ── Hats Role Mappings ──────────────────────────────────────────

def migrate_hats_roles():
    """Migrate hats_roles.json into hats_role_mappings table."""
    print("=== Migrating hats role mappings ===")

    data = load_json("hats_roles.json")
    if not data:
        print("  No hats_roles.json found (file may not exist yet)\n")
        return

    rows = []
    for hat_id_hex, mapping in data.items():
        rows.append({
            "hat_id_hex": hat_id_hex,
            "role_id": str(mapping["role_id"]),
            "hat_name": mapping.get("hat_name"),
        })

    if rows:
        supabase.table("hats_role_mappings").upsert(
            rows, on_conflict="hat_id_hex"
        ).execute()

    print(f"  Upserted {len(rows)} hat-role mappings\n")


# ── Bot Metadata ────────────────────────────────────────────────

def migrate_bot_metadata():
    """Migrate any additional bot metadata (e.g. _index_message_id)."""
    print("=== Migrating bot metadata ===")

    # The _index_message_id is already handled in migrate_proposals().
    # This function exists for future metadata entries.

    # Also store next_id for reference
    data = load_json("proposals.json")
    if data and "next_id" in data:
        supabase.table("bot_metadata").upsert({
            "key": "proposals_next_id",
            "value": str(data["next_id"]),
        }, on_conflict="key").execute()
        print(f"  Stored proposals_next_id = {data['next_id']}")

    print()


# ── Main ────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("ZAO Fractal Bot -> Supabase Migration")
    print("=" * 60)
    print(f"Supabase URL: {SUPABASE_URL}")
    print(f"Data dir:     {DATA_DIR}")
    print()

    migrate_wallets()
    migrate_proposals()
    migrate_history()
    migrate_intros()
    migrate_events()
    migrate_hats_roles()
    migrate_bot_metadata()

    print("=" * 60)
    print("Migration complete!")
    print("=" * 60)
    print()
    print("Post-migration checklist:")
    print("  1. Verify row counts match your JSON data")
    print("  2. Run the sequence reset SQL statements printed above")
    print("  3. Test the bot with SUPABASE_URL and SUPABASE_SERVICE_KEY set")
    print()


if __name__ == "__main__":
    main()
