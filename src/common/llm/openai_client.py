"""
Handles interactions with the OpenAI API (v1.x.x).
"""
import os
import logging
from typing import List, Dict, Any, Union

# Need AsyncOpenAI for async calls
from openai import AsyncOpenAI

from .base_client import BaseLLMClient

logger = logging.getLogger(__name__)

class OpenAIClient(BaseLLMClient):
    """Handles interactions with the OpenAI API (v1.x.x syntax)."""
    def __init__(self):
        """Initializes the OpenAI client using v1.x.x syntax."""
        self.api_key = os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            logger.warning("OPENAI_API_KEY environment variable not set. OpenAIClient will not function.")
            # Set client to None or raise error if key is strictly needed at init
            self.client = None 
        else:
            # Use the new client initialization
            try:
                self.client = AsyncOpenAI(api_key=self.api_key)
                logger.info("Initializing OpenAI Client (API Key presence: Found)")
            except Exception as e:
                 logger.error(f"Failed to initialize OpenAI client: {e}", exc_info=True)
                 self.client = None # Ensure client is None on init failure
                 raise # Re-raise the error

    async def generate_chat_completion(self, model: str, system_prompt: str, 
                                         messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]],
                                         **kwargs: Any) -> str:
        """Generates a chat completion using the OpenAI API v1.x.x asynchronously."""
        
        # Check if client initialized properly
        if self.client is None:
            error_msg = "OpenAIClient cannot generate completion: Client not initialized (check API key or init error)."
            logger.error(error_msg)
            raise ValueError(error_msg)

        # Basic validation
        if not messages:
            raise ValueError("Messages list cannot be empty.")
        for msg in messages:
            if not isinstance(msg, dict) or 'role' not in msg or 'content' not in msg:
                raise ValueError("Invalid message structure in messages list.")

        # Prepend system message if not already present
        if not messages or messages[0].get("role") != "system":
            # Ensure content for system message is just text
            system_content = system_prompt if isinstance(system_prompt, str) else str(system_prompt)
            formatted_messages = [{"role": "system", "content": system_content}] + messages
        else:
            formatted_messages = messages

        # Filter allowed additional parameters for the new API structure
        allowed_params = [
            "response_format", "temperature", "max_tokens", "max_completion_tokens", # Include both token params
            "top_p", "frequency_penalty", "presence_penalty", "seed",
            "reasoning_effort", "store" # Keep potentially non-standard ones for flexibility
            ]
        params = {"model": model, "messages": formatted_messages}
        
        # Populate params, handling potential max_tokens variations later
        for key in allowed_params:
            if key in kwargs and kwargs[key] is not None:
                 # Temporarily store both possible token keys if provided
                 params[key] = kwargs[key]

        # --- Model-Specific Parameter Adjustment --- 
        is_o_model = model.startswith("o")
        
        if is_o_model:
            # Expect max_completion_tokens for 'o' models
            if "max_tokens" in params and "max_completion_tokens" not in params:
                 logger.debug(f"Model '{model}' expects 'max_completion_tokens', translating from 'max_tokens'.")
                 params["max_completion_tokens"] = params.pop("max_tokens")
            elif "max_tokens" in params and "max_completion_tokens" in params:
                 logger.warning(f"Both 'max_tokens' and 'max_completion_tokens' provided for model '{model}'. Using 'max_completion_tokens'.")
                 params.pop("max_tokens") # Prioritize the expected one
        else:
            # Expect max_tokens for standard models
            if "max_completion_tokens" in params and "max_tokens" not in params:
                 logger.debug(f"Model '{model}' expects 'max_tokens', translating from 'max_completion_tokens'.")
                 params["max_tokens"] = params.pop("max_completion_tokens")
            elif "max_tokens" in params and "max_completion_tokens" in params:
                 logger.warning(f"Both 'max_tokens' and 'max_completion_tokens' provided for model '{model}'. Using 'max_tokens'.")
                 params.pop("max_completion_tokens") # Prioritize the expected one

        # Remove non-standard params if they cause issues, or keep if 'o3' needs them
        # Example: Remove if not 'o' model
        # if not is_o_model:
        #     params.pop("reasoning_effort", None)
        #     params.pop("store", None)
        # --- End Parameter Adjustment ---

        # Log call details (use adjusted params)
        # Define loggable params *after* adjustments
        final_allowed_params = [p for p in allowed_params if p in params]
        loggable_params = {k: params[k] for k in final_allowed_params}
        is_multimodal = any(isinstance(m.get('content'), list) for m in formatted_messages)
        logger.info(f"Making OpenAI call: model={model}, multimodal={is_multimodal}, additional_params={loggable_params}")

        try:
            # Use the new API call structure
            response = await self.client.chat.completions.create(**params)
            
            # Access response differently
            if response.choices and response.choices[0].message and response.choices[0].message.content:
                 generated_text = response.choices[0].message.content.strip()
                 return generated_text
            else:
                 logger.error(f"OpenAI API response missing expected content structure: {response}")
                 raise RuntimeError("OpenAI API response format unexpected.")
                 
        except Exception as e:
            # Catch specific OpenAI errors if needed, e.g., openai.APIError
            logger.error(f"Error during OpenAI API call: {e}", exc_info=True)
            raise # Re-raise the original error or a custom one 