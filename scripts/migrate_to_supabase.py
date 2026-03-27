"""
One-time migration script: reads all JSON data files and inserts into Supabase.

This script is designed for the ZAO OS shared-table architecture:
  - users, fractal_sessions, fractal_scores, respect_members already exist
  - Discord-specific tables are prefixed with discord_

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

SUPABASE_URL = os.environ.get("SUPABASE_URL") or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

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


# ── Wallets -> Update existing users table ──────────────────────

def migrate_wallets():
    """
    Migrate wallets.json into the existing ZAO OS `users` table.

    wallets.json maps discord_id -> wallet_address.  For each entry,
    find the user by wallet address and set their discord_id field.

    names_to_wallets.json data is already in respect_members and does
    not need migration.
    """
    print("=== Migrating wallets (updating users.discord_id) ===")

    wallets = load_json("wallets.json") or {}
    if not wallets:
        print("  No wallets.json data found\n")
        return

    updated = 0
    skipped = 0

    for discord_id, wallet_address in wallets.items():
        if not wallet_address:
            skipped += 1
            continue

        # Find user by primary_wallet and set their discord_id
        # Using case-insensitive match since wallet addresses may differ in case
        try:
            result = (
                supabase.table("users")
                .update({"discord_id": discord_id})
                .ilike("primary_wallet", wallet_address)
                .execute()
            )
            if result.data:
                updated += 1
            else:
                # No matching user found for this wallet -- that's OK,
                # the user may not exist in ZAO OS yet
                skipped += 1
        except Exception as e:
            print(f"  Warning: failed to update user for wallet {wallet_address}: {e}")
            skipped += 1

    print(f"  Updated {updated} users with discord_id")
    print(f"  Skipped {skipped} (no matching user or empty wallet)")
    print()


# ── Proposals + Votes ───────────────────────────────────────────

def migrate_proposals():
    """Migrate proposals.json into discord_proposals + discord_proposal_votes tables."""
    print("=== Migrating proposals ===")

    data = load_json("proposals.json")
    if not data:
        print("  No proposals data found\n")
        return

    # Store the _index_message_id in discord_bot_metadata
    index_msg_id = data.get("_index_message_id")
    if index_msg_id:
        supabase.table("discord_bot_metadata").upsert({
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
            "proposal_type": p.get("type", "text"),
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
        supabase.table("discord_proposals").upsert(row, on_conflict="id").execute()

        # Normalize embedded votes dict into discord_proposal_votes rows
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
            supabase.table("discord_proposal_votes").upsert(
                vote_rows, on_conflict="proposal_id,voter_id"
            ).execute()
            vote_count += len(vote_rows)

    print(f"  Upserted {len(proposals)} proposals into discord_proposals")
    print(f"  Upserted {vote_count} votes into discord_proposal_votes")

    # Remind user to reset the sequence
    next_id = data.get("next_id", max((int(k) for k in proposals), default=0) + 1)
    print(f"  NOTE: Run this SQL to reset the discord_proposals sequence:")
    print(f"    SELECT setval('discord_proposals_id_seq', {next_id}, false);")
    print()


# ── Fractal History ─────────────────────────────────────────────

def migrate_history():
    """
    Migrate history.json into the EXISTING ZAO OS tables:
      - fractal_sessions (using new columns: thread_id, facilitator_discord_id,
        group_number, guild_id, completed_at)
      - fractal_scores (using new columns: discord_id, level, respect_points)

    The bot's history.json uses integer IDs. The ZAO OS fractal_sessions uses
    UUID primary keys.  We insert new rows with generated UUIDs and store the
    Discord-specific metadata in the new columns.

    For idempotency, we check for existing sessions by matching on
    thread_id (unique per Discord thread).
    """
    print("=== Migrating fractal history ===")

    data = load_json("history.json")
    if not data:
        print("  No history data found\n")
        return

    fractals = data.get("fractals", [])
    session_count = 0
    score_count = 0

    for f in fractals:
        thread_id = str(f.get("thread_id", ""))
        facilitator_discord_id = str(f["facilitator_id"])
        group_number = f.get("group_number")
        guild_id = str(f["guild_id"])
        completed_at = f["completed_at"]
        fractal_number = f.get("fractal_number", "")
        group_name = f["group_name"]
        facilitator_name = f["facilitator_name"]

        # Check if this session already exists (by thread_id)
        existing = None
        if thread_id:
            result = (
                supabase.table("fractal_sessions")
                .select("id")
                .eq("thread_id", thread_id)
                .execute()
            )
            if result.data:
                existing = result.data[0]

        if existing:
            # Update existing session with Discord-specific fields
            session_id = existing["id"]
            supabase.table("fractal_sessions").update({
                "facilitator_discord_id": facilitator_discord_id,
                "group_number": group_number,
                "guild_id": guild_id,
                "completed_at": completed_at,
            }).eq("id", session_id).execute()
        else:
            # Insert new session row
            session_row = {
                "name": group_name,
                "host_name": facilitator_name,
                "thread_id": thread_id,
                "facilitator_discord_id": facilitator_discord_id,
                "group_number": group_number,
                "guild_id": guild_id,
                "completed_at": completed_at,
                "session_date": completed_at,
                "participant_count": len(f.get("rankings", [])),
                "notes": f"Migrated from Discord bot history (fractal {fractal_number})",
            }
            result = (
                supabase.table("fractal_sessions")
                .insert(session_row)
                .execute()
            )
            session_id = result.data[0]["id"]

        session_count += 1

        # Insert/update rankings into fractal_scores
        rankings = f.get("rankings", [])
        for i, r in enumerate(rankings):
            discord_id = str(r["user_id"])
            display_name = r["display_name"]
            level = r["level"]
            respect = r.get("respect", 0)
            rank_position = i + 1  # 1-indexed

            # Check if score already exists for this session + discord_id
            existing_score = (
                supabase.table("fractal_scores")
                .select("id")
                .eq("session_id", session_id)
                .eq("discord_id", discord_id)
                .execute()
            )

            if existing_score.data:
                # Update existing score
                supabase.table("fractal_scores").update({
                    "level": level,
                    "respect_points": respect,
                    "rank": rank_position,
                    "member_name": display_name,
                }).eq("id", existing_score.data[0]["id"]).execute()
            else:
                # Insert new score
                score_row = {
                    "session_id": session_id,
                    "discord_id": discord_id,
                    "member_name": display_name,
                    "rank": rank_position,
                    "score": respect,
                    "level": level,
                    "respect_points": respect,
                }
                supabase.table("fractal_scores").insert(score_row).execute()

            score_count += 1

    print(f"  Upserted {session_count} fractal sessions")
    print(f"  Upserted {score_count} fractal scores")
    print()


# ── Intros ──────────────────────────────────────────────────────

def migrate_intros():
    """Migrate intros.json into the discord_intros table."""
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
        supabase.table("discord_intros").upsert(
            rows, on_conflict="discord_id"
        ).execute()

    print(f"  Upserted {len(rows)} intros into discord_intros\n")


# ── Events ──────────────────────────────────────────────────────

def migrate_events():
    """Migrate events.json into the discord_events table."""
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
        supabase.table("discord_events").upsert(
            rows, on_conflict="slug"
        ).execute()

    print(f"  Upserted {len(rows)} events into discord_events\n")


# ── Hats Role Mappings ──────────────────────────────────────────

def migrate_hats_roles():
    """Migrate hats_roles.json into discord_hats_role_mappings table."""
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
        supabase.table("discord_hats_role_mappings").upsert(
            rows, on_conflict="hat_id_hex"
        ).execute()

    print(f"  Upserted {len(rows)} hat-role mappings into discord_hats_role_mappings\n")


# ── Bot Metadata ────────────────────────────────────────────────

def migrate_bot_metadata():
    """Migrate any additional bot metadata into discord_bot_metadata."""
    print("=== Migrating bot metadata ===")

    # Store next_id for reference (index_message_id handled in migrate_proposals)
    data = load_json("proposals.json")
    if data and "next_id" in data:
        supabase.table("discord_bot_metadata").upsert({
            "key": "proposals_next_id",
            "value": str(data["next_id"]),
        }, on_conflict="key").execute()
        print(f"  Stored proposals_next_id = {data['next_id']}")

    print()


# ── Main ────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("ZAO Fractal Bot -> Supabase Migration")
    print("(ZAO OS shared-table architecture)")
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
