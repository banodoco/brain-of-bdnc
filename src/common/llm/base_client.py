"""
Defines the base class for LLM clients.
"""
from typing import List, Dict, Any, Union

class BaseLLMClient:
    """Base class interface for LLM clients."""
    async def generate_chat_completion(self, model: str, system_prompt: str,
                                         messages: List[Dict[str, Union[str, List[Dict[str, Any]]]]],
                                         **kwargs) -> str:
        """
        Generates a chat completion asynchronously.

        Args:
            model: The specific model identifier.
            system_prompt: The system prompt.
            messages: The list of message dictionaries (can be multimodal).
            **kwargs: Additional provider-specific parameters.

        Returns:
            The generated text content as a string.
        
        Raises:
            NotImplementedError: This method must be implemented by subclasses.
        """
        raise NotImplementedError 