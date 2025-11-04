"""
Handles interactions with the Google Gemini API, attempting to use the older client structure.
"""
import os
import logging
import asyncio
from typing import List, Dict, Any, Union, Optional
from dotenv import load_dotenv

# Imports based on the user-provided example
import google.generativeai as genai
from google.generativeai import types
# from google.generativeai.types import Part, GenerationConfig # Remove these imports

from .base_client import BaseLLMClient

logger = logging.getLogger(__name__)

# Mapping from BaseLLMClient roles to Gemini roles
ROLE_MAPPING = {
    "system": "user",
    "user": "user",
    "assistant": "model",
}

# Default model if none provided
DEFAULT_GEMINI_MODEL = "gemini-2.5-pro-preview-03-25"

class GeminiClient(BaseLLMClient):
    """Handles interactions using the google.genai (potentially older) structure."""
    def __init__(self):
        """Initializes the Gemini client using genai.Client()."""
        load_dotenv()
        self.api_key = os.environ.get("GEMINI_API_KEY")
        if not self.api_key:
            logger.warning("GEMINI_API_KEY environment variable not set. GeminiClient will not function.")
            self.client = None # Keep client attribute, but it's None
            self._configured = False
        else:
            try:
                # Use the client initialization from the example
                self.client = genai.Client(api_key=self.api_key)
                self._configured = True
                logger.info("Gemini Client initialized via genai.Client() (API Key presence: Found)")
            except AttributeError:
                 # If genai.Client doesn't exist, this structure is wrong for the installed library
                 logger.error("Failed to initialize Gemini client using genai.Client(). The installed library might be google-generativeai, not the one expected by the example.", exc_info=True)
                 self._configured = False
                 self.client = None
                 raise RuntimeError("Failed to initialize GeminiClient. Check library version mismatch (google.genai vs google.generativeai).")
            except Exception as e:
                 logger.error(f"Failed to initialize Gemini client using genai.Client(): {e}", exc_info=True)
                 self._configured = False
                 self.client = None
                 # Consider raising the error depending on desired behavior
                 # raise

    def _convert_message_format(self, system_prompt: Optional[str], messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]]):
        """Converts the BaseLLMClient message format to types.Content format."""
        gemini_contents = []

        # Handle system prompt
        processed_messages = messages[:] # Shallow copy
        if system_prompt:
            # Logic to prepend system prompt (similar to before, but creating types.Content/Part)
            if processed_messages and processed_messages[0].get("role") == "user":
                 first_content = processed_messages[0].get("content")
                 if isinstance(first_content, str):
                     processed_messages[0]["content"] = f"{system_prompt}\\n\\n{first_content}"
                 elif isinstance(first_content, list):
                      processed_messages[0]["content"].insert(0, {"type": "text", "text": system_prompt})
                 else:
                      # Add as separate user message if first message isn't suitable
                      gemini_contents.append(types.Content(role="user", parts=[types.Part(text=system_prompt)]))
            else:
                 # Add as the very first message
                 gemini_contents.append(types.Content(role="user", parts=[types.Part(text=system_prompt)]))

        # Process the rest of the messages
        for msg in processed_messages:
            role = msg.get("role")
            content = msg.get("content")
            gemini_role = ROLE_MAPPING.get(role, "user")

            parts = [] # List to hold types.Part objects
            if isinstance(content, str):
                parts.append(types.Part(text=content))
            elif isinstance(content, list):
                # Handle multimodal content list
                for item in content:
                    if isinstance(item, dict):
                        item_type = item.get("type")
                        if item_type == "text":
                            parts.append(types.Part(text=item.get("text", "")))
                        elif item_type == "image_uri":
                            uri = item.get("uri")
                            mime_type = item.get("mime_type", None)
                            if uri:
                                if not mime_type:
                                    logger.warning(f"Mime type not provided for image URI: {uri}. May fail.")
                                try:
                                    # Use types.Part.from_uri as per example structure
                                    parts.append(types.Part.from_uri(file_uri=uri, mime_type=mime_type))
                                except Exception as e:
                                    logger.error(f"Failed to create types.Part from URI {uri}: {e}", exc_info=True)
                            else:
                                logger.warning("Image content item missing 'uri'. Skipping.")
                        else:
                             logger.warning(f"Unsupported content type: {item_type}. Skipping.")
                    else:
                        logger.warning(f"Unexpected item format in content list: {item}. Skipping.")
            else:
                 logger.warning(f"Unsupported content format for role {role}: {type(content)}. Skipping message.")
                 continue

            if parts:
                gemini_contents.append(types.Content(role=gemini_role, parts=parts))

        return gemini_contents

    async def generate_chat_completion(self, model: str, system_prompt: str,
                                         messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]],
                                         **kwargs: Any) -> str:
        """Generates a chat completion using the client.models.generate_content structure."""

        if not self._configured or not self.client:
            error_msg = "GeminiClient cannot generate completion: Client not initialized (check API key or init error, potentially library mismatch)."
            logger.error(error_msg)
            raise RuntimeError(error_msg)

        if not messages:
            raise ValueError("Messages list cannot be empty.")

        effective_model = model or DEFAULT_GEMINI_MODEL

        # Convert messages using the types.Content structure
        gemini_contents = self._convert_message_format(system_prompt, messages)
        if not gemini_contents:
             logger.error("Failed to convert messages to Gemini types.Content format.")
             raise ValueError("No valid content to send to Gemini API after format conversion.")

        # Prepare generation configuration using types.GenerateContentConfig
        allowed_params = [
            "candidate_count", "stop_sequences", "max_output_tokens",
            "temperature", "top_p", "top_k", "response_mime_type",
        ]
        config_params = {k: v for k, v in kwargs.items() if k in allowed_params and v is not None}
        generation_config = types.GenerateContentConfig(**config_params) if config_params else None

        # Prepare safety settings (assuming similar structure, might need adjustment)
        safety_settings = kwargs.get("safety_settings", None)

        is_multimodal = any(p.file_data for c in gemini_contents for p in c.parts if hasattr(p, 'file_data')) # Simple check for file data
        logger.info(f"Making Gemini call (using client.models structure): model={effective_model}, multimodal={is_multimodal}, config={config_params}")

        try:
            # Use the client.models.generate_content method (non-async version)
            # We need to run this potentially blocking call in an executor for async compatibility
            loop = asyncio.get_running_loop()
            response = await loop.run_in_executor(
                None, # Use default executor
                lambda: self.client.models.generate_content(
                    model=effective_model,
                    contents=gemini_contents,
                    config=generation_config,
                )
            )

            # Extract text (assuming response structure is similar)
            generated_text = ""
            if hasattr(response, 'text'):
                 generated_text = response.text.strip()
            elif response.parts:
                 generated_text = "".join(part.text for part in response.parts if hasattr(part, 'text')).strip()

            if not generated_text and hasattr(response, 'prompt_feedback') and response.prompt_feedback.block_reason:
                 block_reason = response.prompt_feedback.block_reason.name
                 logger.warning(f"Gemini response blocked: {block_reason}")
                 return f"[BLOCKED: {block_reason}]"
            elif not generated_text and response.candidates:
                 try:
                      candidate_text = response.candidates[0].content.parts[0].text
                      if candidate_text:
                           logger.warning("Using text from first candidate.")
                           return candidate_text.strip()
                 except (IndexError, AttributeError, TypeError):
                      pass # Ignore if candidate structure is different
            elif not generated_text:
                 logger.warning("Gemini response missing text.")
                 return ""

            return generated_text

        except AttributeError as e:
            # Catch if self.client.models.generate_content doesn't exist
            logger.error(f"AttributeError during API call: {e}. Likely library mismatch. Expected client.models.generate_content.", exc_info=True)
            raise RuntimeError(f"API call failed. Check library version mismatch (google.genai vs google.generativeai). Error: {e}")
        except Exception as e:
            logger.error(f"Error during Gemini API call (client.models structure): {e}", exc_info=True)
            raise 