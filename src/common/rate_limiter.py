import discord
import asyncio
import random
import logging
import traceback

class RateLimiter:
    """Manages rate limiting for Discord API calls with exponential backoff."""
    
    def __init__(self):
        self.backoff_times = {}  # Store backoff times per channel
        self.base_delay = 1.0    # Base delay in seconds
        self.max_delay = 64.0    # Maximum delay in seconds
        self.jitter = 0.1        # Random jitter factor
        self.logger = logging.getLogger('ChannelSummarizer')  # Initialize logger
    
    async def execute(self, key, coroutine_or_factory):
        """
        Executes a coroutine or coroutine factory with rate limit handling.
        
        Args:
            key: Identifier for the rate limit (e.g., channel_id)
            coroutine_or_factory: The coroutine or factory function to execute
            
        Returns:
            The result of the coroutine execution
        """
        max_retries = 5
        attempt = 0
        
        while attempt < max_retries:
            try:
                # Add jitter to prevent thundering herd
                if key in self.backoff_times:
                    jitter = random.uniform(-self.jitter, self.jitter)
                    await asyncio.sleep(self.backoff_times[key] * (1 + jitter))
                
                # Ensure coroutine_or_factory is a callable factory
                if not callable(coroutine_or_factory):
                    self.logger.error("RateLimiter.execute expects a callable coroutine factory.")
                    raise TypeError("coroutine_or_factory must be a callable that returns a coroutine")

                # Get a new coroutine object from the factory for each attempt
                current_coro = coroutine_or_factory()
                if not asyncio.iscoroutine(current_coro):
                    self.logger.error("Coroutine factory did not return a coroutine.")
                    raise TypeError("coroutine_or_factory must return a coroutine")
                
                result = await current_coro
                
                # Reset backoff on success
                self.backoff_times[key] = self.base_delay
                return result
                
            except discord.HTTPException as e:
                attempt += 1
                
                if e.status == 429:  # Rate limit hit
                    retry_after = e.retry_after if hasattr(e, 'retry_after') else None
                    
                    if retry_after:
                        self.logger.warning(f"Rate limit hit for {key}. Retry after {retry_after}s")
                        await asyncio.sleep(retry_after)
                    else:
                        # Calculate exponential backoff
                        current_delay = self.backoff_times.get(key, self.base_delay)
                        next_delay = min(current_delay * 2, self.max_delay)
                        self.backoff_times[key] = next_delay
                        
                        self.logger.warning(f"Rate limit hit for {key}. Using exponential backoff: {next_delay}s")
                        await asyncio.sleep(next_delay)
                        
                elif attempt == max_retries:
                    self.logger.error(f"Failed after {max_retries} attempts: {e}")
                    raise
                else:
                    self.logger.warning(f"Discord API error (attempt {attempt}/{max_retries}): {e}")
                    # Calculate exponential backoff for other errors
                    current_delay = self.backoff_times.get(key, self.base_delay)
                    next_delay = min(current_delay * 2, self.max_delay)
                    self.backoff_times[key] = next_delay
                    await asyncio.sleep(next_delay)
            
            except (OSError, ConnectionError, TimeoutError, asyncio.TimeoutError) as e:
                attempt += 1
                if attempt == max_retries:
                    self.logger.error(f"Network connectivity failed after {max_retries} attempts: {e}")
                    raise
                else:
                    # Use exponential backoff for network errors
                    current_delay = self.backoff_times.get(key, self.base_delay)
                    next_delay = min(current_delay * 2, self.max_delay)
                    self.backoff_times[key] = next_delay
                    self.logger.warning(f"Network error (attempt {attempt}/{max_retries}) for {key}: {e}. Retrying in {next_delay}s")
                    await asyncio.sleep(next_delay)
            
            except Exception as e:
                self.logger.error(f"Unexpected error: {e}")
                self.logger.debug(traceback.format_exc())
                raise 