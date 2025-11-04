"""
SQL Translator - Converts SQLite SQL to PostgreSQL SQL for Supabase queries.
Handles dialect differences like date functions, JSON operations, and placeholders.
"""

import re
from typing import Tuple, List, Any

class SQLTranslator:
    """Translates SQLite SQL to PostgreSQL SQL."""
    
    @staticmethod
    def translate_to_postgres(sqlite_query: str, params: Tuple = None) -> Tuple[str, List]:
        """
        Translate a SQLite query to PostgreSQL.
        
        Args:
            sqlite_query: SQLite SQL query string
            params: Query parameters (for ? placeholders)
            
        Returns:
            Tuple of (postgres_query, postgres_params)
        """
        query = sqlite_query
        
        # 1. Replace SQLite datetime functions with PostgreSQL equivalents
        # datetime('now') → NOW()
        query = re.sub(r"datetime\s*\(\s*['\"]now['\"]\s*\)", "NOW()", query, flags=re.IGNORECASE)
        
        # datetime('now', '-N hours/days/minutes') → NOW() - INTERVAL 'N hours/days/minutes'
        def replace_datetime_offset(match):
            offset = match.group(1)
            # Remove the minus sign and quotes
            offset_clean = offset.strip().strip("'\"").lstrip('-')
            return f"NOW() - INTERVAL '{offset_clean}'"
        
        query = re.sub(
            r"datetime\s*\(\s*['\"]now['\"]\s*,\s*(['\"][^'\"]+['\"])\s*\)",
            replace_datetime_offset,
            query,
            flags=re.IGNORECASE
        )
        
        # 2. Replace JSON functions
        # json_array_length() → jsonb_array_length()
        query = re.sub(r'\bjson_array_length\b', 'jsonb_array_length', query, flags=re.IGNORECASE)
        
        # json_valid() → (column IS NOT NULL AND jsonb_typeof(column) IS NOT NULL)
        # For simplicity, just check if it's valid JSONB
        query = re.sub(
            r'json_valid\s*\(\s*(\w+\.\w+|\w+)\s*\)',
            r"(\1::text != 'null' AND \1::text != '[]')",
            query,
            flags=re.IGNORECASE
        )
        
        # json_each() → jsonb_array_elements()
        query = re.sub(r'\bjson_each\b', 'jsonb_array_elements', query, flags=re.IGNORECASE)
        
        # json_extract(value, '$.field') → value->>'field'
        # This is complex, so we'll do a simple version
        def replace_json_extract(match):
            obj = match.group(1)
            path = match.group(2).strip("'\"")
            # Remove $. prefix
            if path.startswith('$.'):
                path = path[2:]
            return f"{obj}->>{repr(path)}"
        
        query = re.sub(
            r"json_extract\s*\(\s*([^,]+),\s*(['\"][^'\"]+['\"])\s*\)",
            replace_json_extract,
            query,
            flags=re.IGNORECASE
        )
        
        # 3. Replace table names
        # SQLite uses 'messages', 'members', 'channels'
        # Supabase uses 'discord_messages', 'discord_members', 'discord_channels'
        
        # We need to be careful not to replace these in column names
        # Match whole words with FROM/JOIN context
        query = re.sub(r'\bFROM\s+messages\b', 'FROM discord_messages', query, flags=re.IGNORECASE)
        query = re.sub(r'\bJOIN\s+messages\b', 'JOIN discord_messages', query, flags=re.IGNORECASE)
        query = re.sub(r'\bFROM\s+members\b', 'FROM discord_members', query, flags=re.IGNORECASE)
        query = re.sub(r'\bJOIN\s+members\b', 'JOIN discord_members', query, flags=re.IGNORECASE)
        query = re.sub(r'\bFROM\s+channels\b', 'FROM discord_channels', query, flags=re.IGNORECASE)
        query = re.sub(r'\bJOIN\s+channels\b', 'JOIN discord_channels', query, flags=re.IGNORECASE)
        
        # Also handle aliases like 'm.', 'mem.', 'c.'
        # These are fine as-is since they're aliases
        
        # 4. Replace SQLite ? placeholders with PostgreSQL $1, $2, etc.
        postgres_params = list(params) if params else []
        placeholder_count = query.count('?')
        
        for i in range(placeholder_count, 0, -1):
            # Replace from right to left to avoid index issues
            query = query.replace('?', f'${i}', 1)
        
        # Reverse the replacements (we did it backwards)
        for i in range(1, placeholder_count + 1):
            query = query.replace(f'${placeholder_count - i + 1}', f'${i}', 1)
        
        # 5. Handle CASE WHEN with JSON
        # CASE WHEN reactors IS NULL OR reactors = '[]'
        # In PostgreSQL, we need to compare JSONB properly
        query = re.sub(
            r"(\w+)\s*=\s*'\[\]'",
            r"\1::text = '[]'",
            query
        )
        
        # 6. LOWER() for text search - same in both
        # EXISTS clause - same in both
        
        return query, postgres_params
    
    @staticmethod
    def needs_translation(query: str) -> bool:
        """Check if a query needs translation (contains SQLite-specific syntax)."""
        sqlite_patterns = [
            r"datetime\s*\(",
            r"json_valid\s*\(",
            r"json_array_length\s*\(",
            r"json_each\s*\(",
            r"json_extract\s*\(",
            r"\bFROM\s+messages\b",
            r"\bFROM\s+members\b",
            r"\bFROM\s+channels\b",
        ]
        
        for pattern in sqlite_patterns:
            if re.search(pattern, query, re.IGNORECASE):
                return True
        
        return False

