#!/bin/bash
#
# Complete Archive Script
# Runs all necessary archive commands to ensure 100% data completeness.
# Can be left running for several days.
#
# Usage: ./scripts/complete_archive.sh
#

set -e  # Exit on error

cd /Users/peteromalley/Documents/bndc

LOG_FILE="complete_archive.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=========================================="
log "Starting Complete Archive Process"
log "=========================================="

# Get initial count
INITIAL_COUNT=$(python3 -c "
import os, sys
sys.path.append('.')
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client
supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))
result = supabase.table('discord_messages').select('message_id', count='exact').limit(1).execute()
print(result.count)
")
log "Initial message count: $INITIAL_COUNT"

# Command 1: Sept 2023 - Feb 2025 (18 months)
log ""
log "=========================================="
log "PHASE 1/3: Sept 2023 - Feb 2025 (18 months)"
log "Using --fast-fill for optimized gap filling"
log "=========================================="
python scripts/archive_discord.py --start-date 2023-09-01 --end-date 2025-03-01 --fast-fill

PHASE1_COUNT=$(python3 -c "
import os, sys
sys.path.append('.')
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client
supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))
result = supabase.table('discord_messages').select('message_id', count='exact').limit(1).execute()
print(result.count)
")
log "After Phase 1: $PHASE1_COUNT messages (added $((PHASE1_COUNT - INITIAL_COUNT)))"

# Command 2: April - July 2025 (4 months)
log ""
log "=========================================="
log "PHASE 2/3: April - July 2025 (4 months)"
log "Using --fast-fill for optimized gap filling"
log "=========================================="
python scripts/archive_discord.py --start-date 2025-04-01 --end-date 2025-08-01 --fast-fill

PHASE2_COUNT=$(python3 -c "
import os, sys
sys.path.append('.')
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client
supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))
result = supabase.table('discord_messages').select('message_id', count='exact').limit(1).execute()
print(result.count)
")
log "After Phase 2: $PHASE2_COUNT messages (added $((PHASE2_COUNT - PHASE1_COUNT)))"

# Command 3: Oct 2025 - Jan 2026 (4 months)
log ""
log "=========================================="
log "PHASE 3/3: Oct 2025 - Jan 2026 (4 months)"
log "Using --fast-fill for optimized gap filling"
log "=========================================="
python scripts/archive_discord.py --start-date 2025-10-01 --end-date 2026-02-01 --fast-fill

FINAL_COUNT=$(python3 -c "
import os, sys
sys.path.append('.')
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client
supabase = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))
result = supabase.table('discord_messages').select('message_id', count='exact').limit(1).execute()
print(result.count)
")
log "After Phase 3: $FINAL_COUNT messages (added $((FINAL_COUNT - PHASE2_COUNT)))"

# Summary
log ""
log "=========================================="
log "COMPLETE ARCHIVE FINISHED"
log "=========================================="
log "Initial count:  $INITIAL_COUNT"
log "Final count:    $FINAL_COUNT"
log "Total added:    $((FINAL_COUNT - INITIAL_COUNT))"
log ""
log "Run 'python scripts/find_gaps.py --paranoid' to verify completeness"
