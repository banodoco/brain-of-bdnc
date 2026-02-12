"""Client for interacting with the Anthropic Claude API."""
import os
import logging
import asyncio
from typing import List, Dict, Any, Union

import anthropic
from dotenv import load_dotenv

# Import from base_client.py
from .base_client import BaseLLMClient

logger = logging.getLogger(__name__)

class ClaudeClient(BaseLLMClient):
    """A centralized client for interacting with the Anthropic Claude API."""

    def __init__(self):
        load_dotenv()
        api_key = os.getenv('ANTHROPIC_API_KEY')
        if not api_key:
            logger.error("ANTHROPIC_API_KEY not found in environment variables.")
            raise ValueError("ANTHROPIC_API_KEY not found in environment")

        try:
            # Using AsyncAnthropic for compatibility with async contexts (like discord bots)
            self.client = anthropic.AsyncAnthropic(api_key=api_key)
            logger.info("Anthropic Claude client initialized successfully.")
        except Exception as e:
            logger.error(f"Failed to initialize Anthropic client: {e}", exc_info=True)
            raise

    async def generate_chat_completion(
        self,
        model: str,
        system_prompt: str,
        messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]],
        max_tokens: int = 4096,
        max_retries: int = 3,
        retry_delay_seconds: int = 5,
        **kwargs: Any
    ) -> str:
        """
        Generates text using the specified Claude model with retry logic.
        Handles both text-only and multimodal (text + image) messages.

        Args:
            model: The Claude model identifier.
            system_prompt: The system prompt.
            messages: A list of message dictionaries. For multimodal input,
                      the 'content' field of a message should be a list of blocks,
                      e.g., [{'role': 'user', 'content': [
                          {'type': 'text', 'text': 'Describe this image.'},
                          {'type': 'image', 'source': {'type': 'base64', ...}}
                      ]}]. For text-only, it's [{'role': 'user', 'content': 'Plain text'}].
            max_tokens: The maximum number of tokens to generate.
            max_retries: Maximum number of retries on API errors.
            retry_delay_seconds: Delay between retries.
            **kwargs: Catches extra arguments passed from the dispatcher (e.g., temperature).

        Returns:
            The generated text as a string.

        Raises:
            RuntimeError: If generation failed after all retries or for non-retryable errors.
            ValueError: If input arguments are invalid (though basic validation is in dispatcher).
            anthropic.APIError subclasses: Can propagate specific API errors if needed.
        """
        if not messages:
             raise ValueError("Messages list cannot be empty.")
        
        # Validate message structure (basic check)
        for msg in messages:
             if not isinstance(msg, dict) or 'role' not in msg or 'content' not in msg:
                 raise ValueError("Invalid message structure in messages list.")
             # Content can be str or list, leave further validation to Anthropic API

        # Combine system_prompt and messages for the API call
        # The 'messages' list is passed directly as it should now contain
        # the structure Anthropic expects (including complex content blocks).
        api_kwargs = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages, 
            "system": system_prompt,
             **kwargs
        }

        for attempt in range(max_retries):
            try:
                logger.debug(f"Attempt {attempt + 1}/{max_retries}: Calling Claude model {model} with {len(messages)} messages. Multimodal: {any(isinstance(m.get('content'), list) for m in messages)}")
                response = await self.client.messages.create(**api_kwargs)

                # Check response structure (Claude API returns content in a list)
                # Assuming the response is primarily text even for multimodal input
                if response.content and isinstance(response.content, list) and len(response.content) > 0 and hasattr(response.content[0], 'text') and response.content[0].text:
                    generated_text = response.content[0].text.strip()
                    logger.debug(f"Claude call successful. Response length: {len(generated_text)}")
                    return generated_text
                else:
                    # Log the actual response structure if it's unexpected
                    logger.warning(f"Claude response content is empty or unexpected structure for model {model}. Response type: {type(response.content)}, Response: {response.content}. Attempt {attempt + 1}/{max_retries}")
                    # Continue to retry logic

            except anthropic.APIConnectionError as e:
                logger.warning(f"Claude API connection error (Attempt {attempt + 1}/{max_retries}): {e}")
            except anthropic.RateLimitError as e:
                logger.warning(f"Claude rate limit exceeded (Attempt {attempt + 1}/{max_retries}): {e}. Retrying...")
            except anthropic.APIStatusError as e:
                logger.error(f"Claude API status error (Attempt {attempt + 1}/{max_retries}): {e.status_code} - {e.response}")
                if e.status_code < 500: # Don't retry on client errors (4xx) like BadRequestError
                    raise RuntimeError(f"Claude API client error: {e.status_code}") from e
                # Retry on server errors (5xx)
            except anthropic.BadRequestError as e:
                 logger.error(f"Claude Bad Request Error (non-retryable): {e}")
                 # Log details carefully, potentially masking sensitive parts of messages
                 # Consider logging only the types of content blocks for multimodal
                 logger.debug(f"Failed messages structure (types): {[type(m.get('content')) for m in messages]}")
                 raise RuntimeError("Claude API Bad Request Error (check prompts/parameters/content structure)") from e
            except Exception as e:
                logger.error(f"An unexpected error occurred while calling Claude (Attempt {attempt + 1}/{max_retries}): {e}", exc_info=True)
                # Could be network issues, unexpected API changes, etc.

            # Retry logic
            if attempt < max_retries - 1:
                wait_time = retry_delay_seconds * (2 ** attempt) # Exponential backoff
                logger.info(f"Retrying Claude call in {wait_time} seconds...")
                await asyncio.sleep(wait_time)
            else:
                logger.error(f"Claude call failed after {max_retries} attempts for model {model}.")
                raise RuntimeError(f"Claude call failed after {max_retries} attempts.")

        # Should not be reached if loop completes, but added for safety
        raise RuntimeError("Claude generation failed unexpectedly after retries.") 