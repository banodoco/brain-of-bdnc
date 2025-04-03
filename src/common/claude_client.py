import os
import logging
import asyncio
import traceback
from typing import List, Dict, Any, Optional, Union

import anthropic
from dotenv import load_dotenv

logger = logging.getLogger(__name__)

class ClaudeClient:
    """A centralized client for interacting with the Anthropic Claude API."""

    def __init__(self):
        load_dotenv()
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not found in environment variables.")
            raise ValueError("ANTHROPIC_API_KEY not found in environment")
        
        try:
            # Using AsyncAnthropic for compatibility with async discord bots
            self.client = anthropic.AsyncAnthropic(api_key=api_key)
            logger.info("Anthropic Claude client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Anthropic client: {e}", exc_info=True)
            raise

    async def generate_text(
        self,
        content: Union[str, List[Dict]], 
        model: str = "claude-3-5-sonnet-latest", 
        max_tokens: int = 8192, 
        system_prompt: Optional[str] = None,
        max_retries: int = 3,
        retry_delay_seconds: int = 5
    ) -> Optional[str]:
        """
        Generates text using the specified Claude model with retry logic. 
        Can handle simple text prompts or complex content blocks for multimodal input.

        Args:
            content: The main user prompt (string) or a list of content blocks (for multimodal).
            model: The Claude model identifier.
            max_tokens: The maximum number of tokens to generate.
            system_prompt: An optional system prompt.
            max_retries: Maximum number of retries on API errors.
            retry_delay_seconds: Delay between retries.

        Returns:
            The generated text as a string, or None if generation failed after retries.
        """
        # Construct messages based on content type
        if isinstance(content, str):
            messages = [{"role": "user", "content": content}]
        elif isinstance(content, list):
            # Assume it's a list of content blocks, use directly
            messages = [{"role": "user", "content": content}] 
        else:
            logger.error(f"Invalid content type for ClaudeClient.generate_text: {type(content)}")
            return None
            
        # System prompt handling (check Anthropic docs for preferred placement)
        # Some models prefer it outside the messages list. Adjust if needed.
        api_kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages
        }
        if system_prompt:
            api_kwargs["system"] = system_prompt

        for attempt in range(max_retries):
            try:
                # Use **api_kwargs to pass parameters
                response = await self.client.messages.create(**api_kwargs) 
                
                if response.content and response.content[0].text:
                    return response.content[0].text.strip()
                else:
                    logger.warning(f"Claude response content is empty for model {model}. Attempt {attempt + 1}/{max_retries}")
                    if attempt == max_retries - 1:
                        return None # Failed after all retries
            except anthropic.APIConnectionError as e:
                logger.warning(f"Claude API connection error (Attempt {attempt + 1}/{max_retries}): {e}")
            except anthropic.RateLimitError as e:
                logger.warning(f"Claude rate limit exceeded (Attempt {attempt + 1}/{max_retries}): {e}")
            except anthropic.APIStatusError as e:
                logger.error(f"Claude API status error (Attempt {attempt + 1}/{max_retries}): {e.status_code} - {e.response}")
                if e.status_code >= 500: # Retry on server errors (5xx)
                    pass # Continue to retry logic below
                else: # Don't retry on client errors (4xx)
                    return None 
            except anthropic.BadRequestError as e:
                 logger.error(f"Claude Bad Request Error (non-retryable): {e}")
                 # Consider logging prompt details carefully, masking sensitive info
                 # logger.debug(f"Failed content (first 100 chars): {str(content)[:100]}...")
                 return None # Bad request, likely prompt issue, don't retry
            except Exception as e:
                logger.error(f"An unexpected error occurred while calling Claude (Attempt {attempt + 1}/{max_retries}): {e}", exc_info=True)
            
            # Retry logic
            if attempt < max_retries - 1:
                logger.info(f"Retrying Claude call in {retry_delay_seconds} seconds...")
                await asyncio.sleep(retry_delay_seconds)
            else:
                logger.error(f"Claude call failed after {max_retries} attempts for model {model}.")
                return None # Failed after all retries

        return None # Fallback return 