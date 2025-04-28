import argparse
import sys
import os
from datetime import datetime, timedelta
import logging
import calendar
from typing import List, Dict, Any, Optional
import json
from collections import defaultdict

# Adjust the path to import from the 'src' directory
script_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(script_dir)
sys.path.insert(0, project_root)

from src.common.db_handler import DatabaseHandler
from src.common.constants import get_database_path

# --- Configuration ---
BATCH_SIZE = 10000
DEFAULT_DEV_MODE = False # Set to True if you want to use the dev database by default
LOG_PREVIEW_MESSAGE_COUNT = 3 # How many messages to preview per batch
# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Placeholder LLM Functions ---
# Replace these with your actual LLM interaction logic

def process_batch_with_llm_1(batch: List[Any]) -> Any:
    """
    Placeholder function to process a batch of messages (either dictionaries or pre-formatted strings).
    If dictionaries are provided and contain `message_id`, include the first 10 IDs in the dummy output;
    otherwise include the first 10 items as a plain preview.
    Replace this with your actual API call and result processing.
    """
    logger.info(f"Processing batch of {len(batch)} messages with LLM 1...")

    ids_preview: List[Any]
    if batch and isinstance(batch[0], dict) and "message_id" in batch[0]:
        ids_preview = [m["message_id"] for m in batch[:10]]
    else:
        ids_preview = batch[:10]  # a simple preview of the first few items

    processed_data = {
        "summary": f"Processed {len(batch)} messages.",
        "preview": ids_preview,
    }
    logger.info("LLM 1 processing complete for batch.")
    return processed_data

def process_combined_results_with_llm_2(combined_results: List[Any]) -> Any:
    """
    Placeholder function to process the combined results from LLM 1 with a second LLM.
    Replace this with your actual API call and result processing.
    """
    logger.info(f"Processing {len(combined_results)} combined results with LLM 2...")
    # Example: Simulate final LLM processing
    final_output = {
        "overall_summary": f"Final processing of {len(combined_results)} batches complete.",
        "details": combined_results
    }
    logger.info(f"LLM 2 processing complete.")
    # time.sleep(2) # Simulate processing time if needed
    return final_output

# --- Helper Functions ---

def get_month_input(month_arg: str | None) -> str:
    """
    Gets and validates the month input from args or user prompt.
    Defaults to the previous full calendar month if no input is given.
    """
    if month_arg:
        try:
            datetime.strptime(month_arg, '%Y-%m')
            logger.info(f"Using specified month: {month_arg}")
            return month_arg
        except ValueError:
            logger.error("Invalid date format provided via command line. Please use YYYY-MM.")
            sys.exit(1)
    else:
        try:
            month_str = input("Enter the month to process (YYYY-MM) [Default: last full month]: ")
            if not month_str:
                # Calculate previous month
                today = datetime.today()
                first_day_current_month = today.replace(day=1)
                last_day_previous_month = first_day_current_month - timedelta(days=1)
                previous_month_str = last_day_previous_month.strftime('%Y-%m')
                logger.info(f"No month entered, defaulting to last full month: {previous_month_str}")
                return previous_month_str
            else:
                # Validate entered month
                datetime.strptime(month_str, '%Y-%m')
                logger.info(f"Using entered month: {month_str}")
                return month_str
        except ValueError:
            logger.error("Invalid format entered. Please use YYYY-MM format or leave blank for default.")
            sys.exit(1)
        except EOFError:
            logger.error("\nNo input received. Exiting.")
            sys.exit(1)


def get_month_date_range(year_month: str) -> tuple[datetime, datetime]:
    """Calculates the start and end datetime objects for a given YYYY-MM string."""
    year, month = map(int, year_month.split('-'))
    _, last_day = calendar.monthrange(year, month)
    start_date = datetime(year, month, 1, 0, 0, 0)
    # Go to the *end* of the last day of the month
    end_date = datetime(year, month, last_day, 23, 59, 59, 999999)
    return start_date, end_date

# --- Formatting Helper ---

def format_messages_hierarchical(messages: List[Dict], db_handler: DatabaseHandler) -> List[Dict]:
    """Format messages into an indented, thread-aware structure similar to the SQL example.

    The resulting list contains dictionaries with keys:
        - message_id
        - ancestor_id      (ID of the true root message for ordering)
        - created_at       (original timestamp string)
        - message          (formatted/indented content)
        - author           (username if found, else author_id)
        - reactions        (reaction_count)
        - attachments      (comma-separated list of attachment URLs or None)
    Messages are filtered to roughly match the SQL HAVING clause:
        * replies (reference_id/thread_id not NULL)
        * messages that have replies
        * messages with ≥2 reactions
        * messages whose content contains an http link
    They are ordered by ancestor_id then created_at (ASC).
    """

    if not messages:
        return []

    # Build lookup tables ----------------------------------------------------
    msgs_by_id: Dict[int, Dict] = {m["message_id"]: m for m in messages}
    children_map: defaultdict[int, list[int]] = defaultdict(list)
    for m in messages:
        parent_id: Optional[int] = m.get("reference_id") or m.get("thread_id")
        if parent_id:
            children_map[parent_id].append(m["message_id"])

    # True ancestor calculation (to group threads correctly) ------------------
    ancestor_map: Dict[int, int] = {}

    def _get_true_ancestor_id(mid: int, visited_path: set[int]) -> int:
        """ Find the ultimate root ancestor ID for a message, handling cycles. """
        if mid in ancestor_map:
            return ancestor_map[mid]
        
        # Cycle detection
        if mid in visited_path:
            logger.warning(f"Detected reference cycle during ancestor lookup for message ID: {mid}. Using self as ancestor.")
            ancestor_map[mid] = mid # Use self as ancestor to break cycle
            return mid

        msg = msgs_by_id.get(mid)
        if not msg:
            ancestor_map[mid] = mid # Should not happen if mid comes from messages list
            return mid

        parent = msg.get("reference_id") or msg.get("thread_id")
        if parent and parent in msgs_by_id:
            # Add current message to path before recursing
            visited_path.add(mid)
            true_ancestor = _get_true_ancestor_id(parent, visited_path)
            # Remove current message from path after returning
            visited_path.remove(mid)
            ancestor_map[mid] = true_ancestor
            return true_ancestor
        else:
            # No parent found in our set, this message is the root
            ancestor_map[mid] = mid
            return mid

    # Compute ancestor for all messages
    for mid in msgs_by_id:
        if mid not in ancestor_map:
            _get_true_ancestor_id(mid, set())

    # Depth calculation ------------------------------------------------------
    depth_map: Dict[int, int] = {}

    def _compute_depth(mid: int, visited_path: set[int]) -> int:
        """ Recursively compute message depth, handling cycles. """
        if mid in depth_map:
            return depth_map[mid]
        
        # Cycle detection
        if mid in visited_path:
            logger.warning(f"Detected reference cycle involving message ID: {mid}. Assigning depth 0.")
            depth_map[mid] = 0 # Break the cycle
            return 0

        msg = msgs_by_id.get(mid)
        if not msg:
            depth_map[mid] = 0
            return 0

        parent = msg.get("reference_id") or msg.get("thread_id")
        if parent and parent in msgs_by_id:
            # Add current message to path before recursing
            visited_path.add(mid)
            parent_depth = _compute_depth(parent, visited_path)
            # Remove current message from path after returning
            visited_path.remove(mid)
            depth_map[mid] = parent_depth + 1
        else:
            depth_map[mid] = 0
        return depth_map[mid]

    # Compute depth for all messages
    for mid in msgs_by_id:
        if mid not in depth_map:
            _compute_depth(mid, set()) # Pass initial empty set for visited path

    # Has-responses calculation ---------------------------------------------
    has_responses: Dict[int, bool] = {mid: bool(children_map.get(mid)) for mid in msgs_by_id}

    # Author lookup ----------------------------------------------------------
    author_ids = {m.get("author_id") for m in messages if m.get("author_id") is not None}
    author_cache: Dict[int, str] = {}
    for aid in author_ids:
        member = db_handler.get_member(aid)
        if member and member.get("username"):
            author_cache[aid] = member["username"]
        else:
            author_cache[aid] = str(aid)

    # Helper to derive attachment URLs --------------------------------------
    def _extract_attachment_urls(raw: Optional[str]) -> Optional[str]:
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                urls = {item.get("url") for item in data if isinstance(item, dict) and item.get("url")}
                urls.discard(None)
                if urls:
                    return ",".join(sorted(urls))
        except Exception:
            pass  # Malformed JSON or unexpected structure – ignore
        return None

    # Build formatted list ---------------------------------------------------
    formatted: List[Dict] = []
    for m in messages:
        mid = m["message_id"]
        depth = depth_map.get(mid, 0)
        indent = " " * (depth * 4)
        if m.get("reference_id") is not None:
            prefix = "↳ "
        elif m.get("thread_id") is not None:
            prefix = "│ "
        else:
            prefix = ""
        content = m.get("content") or ""
        formatted_text = f"{indent}{prefix}{content}"

        include = (
            m.get("reference_id") is not None
            or m.get("thread_id") is not None
            or has_responses.get(mid, False)
            or (m.get("reaction_count", 0) >= 2)
            or ("http" in content.lower())
        )
        if not include:
            continue

        # root_id = m.get("reference_id") or m.get("thread_id") or mid # Old immediate parent logic
        ancestor_id = ancestor_map.get(mid, mid) # Get true ancestor
        formatted.append(
            {
                "message_id": mid,
                "ancestor_id": ancestor_id, # Use true ancestor for sorting
                "created_at": m.get("created_at"),
                "message": formatted_text,
                "author": author_cache.get(m.get("author_id")),
                "reactions": m.get("reaction_count"),
                "attachments": _extract_attachment_urls(m.get("attachments")),
            }
        )

    # Sort as per SQL ORDER BY true_ancestor_id, created_at ------------------
    formatted.sort(key=lambda x: (x["ancestor_id"], x["created_at"]))
    return formatted

# --- Main Script Logic ---

def main():
    parser = argparse.ArgumentParser(description="Fetch messages for a specific month, process them in batches with LLMs.")
    parser.add_argument("-m", "--month", help="The month to process in YYYY-MM format.", type=str)
    parser.add_argument("--dev", action='store_true', help="Use the development database.")
    args = parser.parse_args()

    dev_mode = args.dev or DEFAULT_DEV_MODE
    year_month = get_month_input(args.month)
    start_date, end_date = get_month_date_range(year_month)

    logger.info(f"Processing messages for {year_month} (from {start_date} to {end_date})")
    logger.info(f"Using {'development' if dev_mode else 'production'} database.")

    try:
        # Initialize database handler
        db_path = get_database_path(dev_mode)
        db_handler = DatabaseHandler(db_path=db_path, dev_mode=dev_mode)
        logger.info(f"Connected to database: {db_path}")

        # Fetch messages for the specified month
        logger.info("Fetching messages from the database...")
        raw_messages = db_handler.get_messages_in_range(start_date, end_date)
        logger.info(f"Fetched {len(raw_messages)} messages for {year_month}.")

        if not raw_messages:
            logger.info("No messages found for the specified month. Exiting.")
            return

        # Format messages hierarchically (indentation, prefixes, etc.)
        formatted_records = format_messages_hierarchical(raw_messages, db_handler)
        logger.info(f"After filtering & formatting, {len(formatted_records)} messages remain.")

        # Convert each record to a single string line resembling the SQL output
        formatted_lines: List[str] = []
        for rec in formatted_records:
            # rec['created_at'] is stored as ISO string – keep as-is for now or convert if desired
            time_str = rec["created_at"]
            attachments_str = rec["attachments"] or ""
            # Put author first
            line = f"{rec['author']}: {rec['message']} | {time_str} | {rec['reactions']} | {attachments_str}"
            formatted_lines.append(line)

        # Batch formatted lines
        batches = [formatted_lines[i:i + BATCH_SIZE] for i in range(0, len(formatted_lines), BATCH_SIZE)]
        logger.info(f"Split formatted lines into {len(batches)} batches of up to {BATCH_SIZE} lines each.")

        # Process each batch with the first LLM
        llm1_results = []
        for i, batch in enumerate(batches):
            logger.info(f"--- Processing Batch {i+1}/{len(batches)} ---")

            # Log preview of first messages in batch (data sent to LLM 1)
            logger.info(f"Previewing first {min(len(batch), LOG_PREVIEW_MESSAGE_COUNT)} formatted messages sent to LLM 1:")
            for msg_index, line in enumerate(batch[:LOG_PREVIEW_MESSAGE_COUNT]):
                logger.info(f"  Msg {msg_index+1}: {line}")
            if not batch:
                logger.info("  Batch is empty.")

            # Existing call to LLM 1 (now passing list[str])
            batch_result = process_batch_with_llm_1(batch)

            # Log the result for the current batch (now with limited IDs)
            logger.info(f"Batch {i+1} processing (dummy) result: {batch_result}")

            llm1_results.append(batch_result)

        # Process combined results with the second LLM
        logger.info("--- Starting Final Processing (LLM 2) ---")
        final_result = process_combined_results_with_llm_2(llm1_results)

        # Output the final result
        logger.info("--- Final Result ---")
        print(json.dumps(final_result, indent=2))

    except Exception as e:
        logger.error(f"An error occurred: {e}", exc_info=True) # Log traceback
        sys.exit(1)
    finally:
        if 'db_handler' in locals() and db_handler:
            logger.info("Closing database connection.")
            db_handler.close()

if __name__ == "__main__":
    main() 