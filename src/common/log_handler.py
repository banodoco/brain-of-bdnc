import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import threading
import traceback
from datetime import datetime
from queue import Queue, Empty
from typing import Optional, List, Dict, Any

# Supabase imports - optional
try:
    from supabase import create_client, Client
    SUPABASE_AVAILABLE = True
except ImportError:
    SUPABASE_AVAILABLE = False

class LineCountRotatingFileHandler(RotatingFileHandler):
    """A handler that rotates based on both size and line count"""
    def __init__(self, filename, mode='a', maxBytes=0, backupCount=0, 
                 encoding=None, delay=False, max_lines=None):
        super().__init__(filename, mode, maxBytes, backupCount, encoding, delay)
        self.max_lines = max_lines
        
        # Initialize line count from existing file
        if os.path.exists(filename):
            try:
                with open(filename, 'r', encoding=encoding or 'utf-8', errors='replace') as f:
                    self.line_count = sum(1 for _ in f)
            except Exception:
                # If we can't read the file, start with 0 lines
                self.line_count = 0
        else:
            self.line_count = 0
    
    def doRollover(self):
        """Override doRollover to keep last N lines"""
        if self.stream:
            self.stream.close()
            self.stream = None
            
        if self.max_lines:
            try:
                # Read all lines from the current file
                with open(self.baseFilename, 'r', encoding=self.encoding, errors='replace') as f:
                    lines = f.readlines()
                
                # Keep only the last max_lines
                lines = lines[-self.max_lines:]
                
                # Write the last max_lines back to the file
                with open(self.baseFilename, 'w', encoding=self.encoding) as f:
                    f.writelines(lines)
                
                self.line_count = len(lines)
            except Exception:
                # If we can't rotate properly, just truncate the file
                with open(self.baseFilename, 'w', encoding=self.encoding) as f:
                    f.write('')
                self.line_count = 0
        
        if not self.delay:
            self.stream = self._open()
    
    def emit(self, record):
        """Emit a record and check line count"""
        if self.max_lines and self.line_count >= self.max_lines:
            self.doRollover()
            
        super().emit(record)
        self.line_count += 1

class LogHandler:
    """Handles logging configuration with separate files for dev/prod and rotation"""
    
    def __init__(self, 
                 logger_name: str = 'Application',
                 prod_log_file: str = 'app.log',
                 dev_log_file: Optional[str] = 'app_dev.log'):
        """
        Initialize the log handler.
        
        Args:
            logger_name: Name of the logger
            prod_log_file: Path to production log file
            dev_log_file: Path to development log file (optional)
        """
        self.logger_name = logger_name
        self.prod_log_file = prod_log_file
        self.dev_log_file = dev_log_file
        self.logger = None

    def setup_logging(self, dev_mode: bool = False) -> logging.Logger:
        """
        Configure logging with separate files for dev/prod and rotation.
        
        Args:
            dev_mode: Whether to run in development mode
            
        Returns:
            Configured logger instance
        """
        try:
            logger = logging.getLogger(self.logger_name)
            # Get log level from environment, default to INFO
            env_level = os.getenv('LOG_LEVEL', 'INFO').upper()
            log_level = getattr(logging, env_level, logging.INFO)
            if dev_mode:
                log_level = logging.DEBUG
            logger.setLevel(log_level)
            
            # Clear any existing handlers
            logger.handlers.clear()
            
            # Prevent propagation to avoid duplicate logs
            logger.propagate = False
            
            # Ensure log directory exists for production log
            log_dir = os.path.dirname(self.prod_log_file)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)
            
            if dev_mode and self.dev_log_file:
                dev_log_dir = os.path.dirname(self.dev_log_file)
                if dev_log_dir and not os.path.exists(dev_log_dir):
                    os.makedirs(dev_log_dir)
            
            # Console handler - show INFO and above for clean operational logs
            # DEBUG logs still go to dev file, but console stays clean
            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)  # Always INFO on console, DEBUG goes to file
            console_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)
            
            # Production file handler - log WARNING and above for important operational logs
            try:
                prod_handler = LineCountRotatingFileHandler(
                    self.prod_log_file,
                    maxBytes=2 * 1024 * 1024,  # 2MB per file
                    backupCount=3,  # Keep fewer backup files
                    encoding='utf-8',
                    max_lines=5000  # Reduce lines to keep
                )
                prod_handler.setLevel(logging.WARNING)  # Only log warnings and errors in production
                prod_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
                prod_handler.setFormatter(prod_formatter)
                logger.addHandler(prod_handler)
            except Exception as e:
                print(f"Failed to setup production log file handler: {e}")
                logger.error(f"Failed to setup production log file handler: {e}")
            
            # Development file handler (only when in dev mode)
            if dev_mode and self.dev_log_file:
                dev_handler = LineCountRotatingFileHandler(
                    self.dev_log_file,
                    maxBytes=512 * 1024,  # 512KB per file
                    backupCount=200,
                    encoding='utf-8',
                    max_lines=200
                )
                dev_handler.setLevel(logging.DEBUG)  # Log everything in dev
                dev_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
                dev_handler.setFormatter(dev_formatter)
                
                # Only do rollover if file exists AND exceeds limits
                if os.path.exists(self.dev_log_file):
                    if os.path.getsize(self.dev_log_file) > 512 * 1024:
                        dev_handler.doRollover()
                    else:
                        # Count lines in existing file
                        with open(self.dev_log_file, 'r', encoding='utf-8') as f:
                            line_count = sum(1 for _ in f)
                        if line_count > 200:
                            dev_handler.doRollover()
                
                logger.addHandler(dev_handler)
            
            # Log startup info - critical setup logs always show
            logger.info(f"Logging configured in {'development' if dev_mode else 'production'} mode")
            logger.info(f"Production log file: {self.prod_log_file}")
            if dev_mode and self.dev_log_file:
                logger.info(f"Development log file: {self.dev_log_file}")
            
            if not self._verify_file_writable(self.prod_log_file):
                print("WARNING: Cannot write to production log file - logging to console only")
                return logger  # Return logger with only console handler
            
            self.logger = logger
            return logger
            
        except Exception as e:
            print(f"Failed to setup logging: {e}")
            raise

    def get_logger(self) -> Optional[logging.Logger]:
        """Get the configured logger instance."""
        return self.logger 

    def _verify_file_writable(self, filepath):
        """Verify that we can write to the log file"""
        try:
            # Try to open file for writing
            log_dir = os.path.dirname(filepath)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)
            
            with open(filepath, 'a') as f:
                f.write('')
            return True
        except Exception as e:
            print(f"Cannot write to log file {filepath}: {e}")
            return False


class SupabaseLogHandler(logging.Handler):
    """
    A logging handler that writes logs to Supabase in batches.
    
    Logs are buffered and sent in batches to reduce API calls.
    Uses a background thread for non-blocking writes.
    """
    
    def __init__(
        self, 
        supabase_url: str, 
        supabase_key: str,
        table_name: str = 'system_logs',
        batch_size: int = 50,
        flush_interval: float = 5.0,
        level: int = logging.INFO
    ):
        """
        Initialize the Supabase log handler.
        
        Args:
            supabase_url: Supabase project URL
            supabase_key: Supabase service key
            table_name: Name of the logs table
            batch_size: Number of logs to batch before sending
            flush_interval: Seconds between automatic flushes
            level: Minimum log level to capture
        """
        super().__init__(level)
        
        if not SUPABASE_AVAILABLE:
            raise ImportError("Supabase client not available. Install with: pip install supabase")
        
        self.supabase: Client = create_client(supabase_url, supabase_key)
        self.table_name = table_name
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.hostname = socket.gethostname()
        
        # Thread-safe queue for log records
        self._queue: Queue = Queue()
        self._buffer: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        
        # Background thread for flushing logs
        self._shutdown = threading.Event()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()
    
    def emit(self, record: logging.LogRecord):
        """Add a log record to the queue for batching."""
        try:
            log_entry = self._format_record(record)
            self._queue.put(log_entry)
        except Exception:
            self.handleError(record)
    
    def _format_record(self, record: logging.LogRecord) -> Dict[str, Any]:
        """Format a log record for Supabase insertion."""
        # Get exception info if present
        exception_text = None
        if record.exc_info:
            exception_text = ''.join(traceback.format_exception(*record.exc_info))
        
        # Extract extra fields (anything added via extra= parameter)
        extra = {}
        standard_attrs = {
            'name', 'msg', 'args', 'created', 'filename', 'funcName', 
            'levelname', 'levelno', 'lineno', 'module', 'msecs',
            'pathname', 'process', 'processName', 'relativeCreated',
            'stack_info', 'exc_info', 'exc_text', 'thread', 'threadName',
            'message', 'asctime'
        }
        for key, value in record.__dict__.items():
            if key not in standard_attrs:
                try:
                    # Only include JSON-serializable values
                    import json
                    json.dumps(value)
                    extra[key] = value
                except (TypeError, ValueError):
                    extra[key] = str(value)
        
        return {
            'timestamp': datetime.utcfromtimestamp(record.created).isoformat(),
            'level': record.levelname,
            'logger_name': record.name,
            'message': record.getMessage(),
            'module': record.module,
            'function_name': record.funcName,
            'line_number': record.lineno,
            'exception': exception_text,
            'extra': extra if extra else {},
            'hostname': self.hostname
        }
    
    def _flush_loop(self):
        """Background loop that flushes logs periodically."""
        while not self._shutdown.is_set():
            try:
                # Collect logs from queue
                while True:
                    try:
                        log_entry = self._queue.get_nowait()
                        with self._lock:
                            self._buffer.append(log_entry)
                    except Empty:
                        break
                
                # Flush if buffer is full or interval elapsed
                with self._lock:
                    if len(self._buffer) >= self.batch_size:
                        self._flush_buffer()
                
                # Wait for flush interval
                self._shutdown.wait(self.flush_interval)
                
                # Flush any remaining logs
                with self._lock:
                    if self._buffer:
                        self._flush_buffer()
                        
            except Exception as e:
                print(f"Error in Supabase log flush loop: {e}")
    
    def _flush_buffer(self):
        """Flush buffered logs to Supabase."""
        if not self._buffer:
            return
        
        logs_to_send = self._buffer.copy()
        self._buffer.clear()
        
        try:
            self.supabase.table(self.table_name).insert(logs_to_send).execute()
        except Exception as e:
            # Don't lose logs - print to stderr as fallback
            print(f"Failed to send {len(logs_to_send)} logs to Supabase: {e}")
            # Could optionally re-queue logs here, but risk infinite loop
    
    def flush(self):
        """Force flush all buffered logs."""
        # Drain queue first
        while True:
            try:
                log_entry = self._queue.get_nowait()
                with self._lock:
                    self._buffer.append(log_entry)
            except Empty:
                break
        
        # Then flush buffer
        with self._lock:
            self._flush_buffer()
    
    def close(self):
        """Clean up handler resources."""
        self._shutdown.set()
        self.flush()
        self._flush_thread.join(timeout=5.0)
        super().close()


def setup_supabase_logging(
    logger: logging.Logger,
    supabase_url: Optional[str] = None,
    supabase_key: Optional[str] = None,
    min_level: int = logging.WARNING,
    batch_size: int = 50,
    flush_interval: float = 5.0
) -> Optional[SupabaseLogHandler]:
    """
    Add Supabase logging to an existing logger.
    
    Args:
        logger: The logger to add Supabase logging to
        supabase_url: Supabase URL (defaults to SUPABASE_URL env var)
        supabase_key: Supabase key (defaults to SUPABASE_SERVICE_KEY env var)
        min_level: Minimum log level to send to Supabase
        batch_size: Number of logs to batch
        flush_interval: Seconds between flushes
        
    Returns:
        The SupabaseLogHandler if successful, None otherwise
    """
    if not SUPABASE_AVAILABLE:
        print("Supabase client not available - skipping Supabase logging")
        return None
    
    url = supabase_url or os.getenv('SUPABASE_URL')
    key = supabase_key or os.getenv('SUPABASE_SERVICE_KEY')
    
    if not url or not key:
        print("Supabase credentials not configured - skipping Supabase logging")
        return None
    
    try:
        handler = SupabaseLogHandler(
            supabase_url=url,
            supabase_key=key,
            batch_size=batch_size,
            flush_interval=flush_interval,
            level=min_level
        )
        
        # Use a simple formatter for Supabase (message is already formatted)
        formatter = logging.Formatter('%(message)s')
        handler.setFormatter(formatter)
        
        logger.addHandler(handler)
        logger.info("Supabase logging enabled")
        return handler
        
    except Exception as e:
        print(f"Failed to setup Supabase logging: {e}")
        return None