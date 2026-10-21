from .base_adapter import BaseAIAdapter
from .chatgpt_adapter import ChatGPTAdapter
from .chatgptanon_adapter import ChatGPTAnonAdapter
from .deepseek_adapter import DeepSeekAdapter
from .qwen_adapter import QwenAdapter
from .genspark_adapter import GenSparkAdapter
from .deepai_adapter import DeepAIAdapter
from .zai_adapter import ZAIAdapter
from .grok_adapter import GrokAdapter
from .chatbotapp_adapter import ChatbotAppAdapter
from .chatbotchatapp_adapter import ChatbotChatAppAdapter
from .gemini_adapter import GeminiAdapter

ADAPTERS = {
    "chatgpt": ChatGPTAdapter(),
    "chatgptanon": ChatGPTAnonAdapter(),
    "deepseek": DeepSeekAdapter(),
    "qwen": QwenAdapter(),
    "genspark": GenSparkAdapter(),
    "deepai": DeepAIAdapter(),
    "zai": ZAIAdapter(),
    "grok": GrokAdapter(),
    "chatbotapp": ChatbotAppAdapter(),
    "chatbotchatapp": ChatbotChatAppAdapter(),
    "gemini": GeminiAdapter(),
}

def get_adapter(model_name: str) -> BaseAIAdapter:
    name = model_name.lower()
    return ADAPTERS.get(name)
