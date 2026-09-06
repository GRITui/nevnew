"""
NevNew (นิวนิว) persona injection for LiteLLM Proxy.

LiteLLM's config.yaml has no native "system prompt" field, so persona
injection is implemented as a CustomLogger pre-call hook: on every chat
completion request routed through this proxy, if the incoming message list
has no system message, prepend one carrying NevNew's persona. If the client
(Open-WebUI, or any other caller) already supplied a system message, it is
left untouched so per-chat customization still works.

Referenced from config.yaml as:
    litellm_settings:
      callbacks: ["nevnew_persona.nevnew_persona_instance"]

LiteLLM auto-imports this module from the working directory / PYTHONPATH
of the proxy container (see docker-compose.yml volume mount + WORKDIR).
"""

from litellm.integrations.custom_logger import CustomLogger

from nevnew_persona_prompt import NEVNEW_SYSTEM_PROMPT


class NevNewPersonaHandler(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type not in ("completion", "acompletion"):
            return data

        messages = data.get("messages")
        if not messages:
            return data

        has_system_message = any(m.get("role") == "system" for m in messages)
        if not has_system_message:
            data["messages"] = [
                {"role": "system", "content": NEVNEW_SYSTEM_PROMPT}
            ] + messages

        return data


nevnew_persona_instance = NevNewPersonaHandler()
