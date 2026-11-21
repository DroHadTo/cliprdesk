"""User-configured chat APIs for Clip Desk's text and vision jobs."""
from dataclasses import dataclass, field
import json

import requests

import network_policy
import secret_store


PRESETS = {
    "openai": ("OpenAI (GPT)", "https://api.openai.com/v1"),
    "anthropic": ("Anthropic (Claude)", "https://api.anthropic.com/v1"),
    "gemini": ("Google (Gemini)", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "openrouter": ("OpenRouter (multi-provider)", "https://openrouter.ai/api/v1"),
    "groq": ("Groq", "https://api.groq.com/openai/v1"),
    "mistral": ("Mistral AI", "https://api.mistral.ai/v1"),
    "xai": ("xAI (Grok)", "https://api.x.ai/v1"),
    "deepseek": ("DeepSeek", "https://api.deepseek.com/v1"),
    "together": ("Together AI", "https://api.together.xyz/v1"),
    "cerebras": ("Cerebras", "https://api.cerebras.ai/v1"),
    "custom": ("Custom OpenAI-compatible API", ""),
    "disabled": ("Use Ollama only", ""),
}


def provider():
    return secret_store.value("AI_PROVIDER") or "custom"


def base_url(name, custom, key):
    if name not in PRESETS or name == "disabled":
        return ""
    if name != "custom" and not key:
        return ""
    base = custom if name == "custom" else PRESETS[name][1]
    return network_policy.endpoint(base, local_http=name == "custom" and not key)


@dataclass(frozen=True)
class Connection:
    provider: str
    base: str
    model: str
    key: str = field(repr=False)

    def complete(self, prompt, *, image=None, response_format=None, tokens=220, timeout=100):
        if response_format:
            prompt += "\nReturn only a JSON object. Do not wrap it in Markdown."
            if isinstance(response_format, dict):
                prompt += "\nJSON schema: " + json.dumps(response_format)
        headers = {}
        if self.provider == "anthropic":
            route = "messages"
            headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
            content = [{"type": "text", "text": prompt}]
            if image:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image}})
            budget = {"max_tokens": max(tokens, 256)}
        else:
            route = "chat/completions"
            if self.key:
                headers["Authorization"] = f"Bearer {self.key}"
            content = prompt
            if image:
                content = [{"type": "text", "text": prompt},
                           {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + image}}]
            budget = ({"max_completion_tokens": max(1024, tokens * 3)} if self.provider == "openai"
                      else {"max_tokens": max(tokens, 256)})
        payload = {"model": self.model, "messages": [{"role": "user", "content": content}], **budget}
        with requests.Session() as session:
            session.trust_env = False
            response = session.post(f"{self.base}/{route}", json=payload, headers=headers,
                                    timeout=(5, timeout), allow_redirects=False)
            if not 200 <= response.status_code < 300:
                response.raise_for_status()
                raise ValueError("The AI provider redirected the request.")
            result = response.json()
        if self.provider == "anthropic":
            text = "\n".join(block["text"] for block in result["content"] if block.get("type") == "text")
        else:
            text = result["choices"][0]["message"]["content"]
        if not isinstance(text, str) or not text.strip():
            raise ValueError("The model returned no text. Check model capabilities and output settings.")
        return text.strip()

    def json_reply(self, prompt, **options):
        text = self.complete(prompt, **options)
        if text.startswith("```") and text.endswith("```") and "\n" in text:
            text = text.split("\n", 1)[1][:-3].strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("The model must return a JSON object.")
        return data


def connection(role):
    with secret_store.LOCK:
        if role == "vision" and secret_store.value("AI_VISION_MODE") != "remote":
            return None
        name = provider()
        key = secret_store.value("AI_API_KEY").strip()
        base = base_url(name, secret_store.value("AI_BASE_URL"), key)
        model = secret_store.value("AI_VISION_MODEL" if role == "vision" else "AI_MODEL").strip()
    return Connection(name, base, model, key) if base and model else None
