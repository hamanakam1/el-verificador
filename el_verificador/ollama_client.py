"""
Async client for the local Ollama endpoint.

El Verificador uses a local Qwen model (no cloud dependency) to compose
bounded Correction Prompts when Taskrivet's output fails acceptance checks.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import aiohttp


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "qwen2.5:14b",
        temperature: float = 0.1,
        timeout: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    async def generate(
        self,
        prompt: str,
        system: str = "",
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
    ) -> str:
        """Generate a completion from local Ollama (non-streaming)."""
        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": f"{system}\n\n{prompt}" if system else prompt,
            "stream": False,
            "options": {
                "temperature": (
                    temperature if temperature is not None else self.temperature
                ),
                "num_predict": max_tokens,
                "top_p": 0.9,
            },
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/generate",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Ollama error {resp.status}: {text[:200]}")
                data = await resp.json()
                return str(data.get("response", ""))

    async def chat(
        self,
        messages: List[Dict[str, str]],
        temperature: Optional[float] = None,
        max_tokens: int = 4096,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": (
                    temperature if temperature is not None else self.temperature
                ),
                "num_predict": max_tokens,
                "top_p": 0.9,
            },
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/api/chat",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(f"Ollama error {resp.status}: {text[:200]}")
                data = await resp.json()
                return str(data.get("message", {}).get("content", ""))

    async def is_available(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.base_url}/api/tags",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return resp.status == 200
        except Exception:
            return False
