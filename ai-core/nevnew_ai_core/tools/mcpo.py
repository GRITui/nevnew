"""Dynamic tool discovery + execution via mcpo (open-webui's MCP→OpenAPI
proxy), which exposes the n8n MCP tool server ("NevNew macOS + GitHub
Tools" — Reminders/Calendar/Notes/GitHub, see n8n/mcp-tools.json) as an
OpenAPI service at http://mcpo:8000.

Discovery: fetch /openapi.json, convert every POST endpoint (one per MCP
tool) into an OpenAI function schema (resolving $refs into
components/schemas, capping depth to survive recursive schemas).

Execution: POST the arguments as the JSON body to the tool's path with the
MCPO_API_KEY bearer token; the response body becomes the tool result.
Failures raise and surface as "ERROR: ..." tool results via the registry.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from .base import Tool, ToolContext

logger = logging.getLogger("nevnew-ai-core")

_MAX_REF_DEPTH = 20
_NAME_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def _sanitize_tool_name(raw: str) -> str:
    name = _NAME_SAFE.sub("_", raw.strip().strip("/"))
    name = name.strip("_-") or "tool"
    return name[:64]


def _dereference(schema: Any, spec: Dict[str, Any], depth: int, seen: List[str]) -> Any:
    """Resolve local $refs (#/components/schemas/...) into inline schemas.

    Recursive schemas are cut off with a stub instead of infinite recursion.
    """
    if depth > _MAX_REF_DEPTH:
        return {"type": "object", "description": "(schema omitted: too deeply nested)"}
    if not isinstance(schema, dict):
        return schema
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/"):
        if ref in seen:
            return {"type": "object", "description": "(recursive schema omitted)"}
        node: Any = spec
        for part in ref[2:].split("/"):
            if not isinstance(node, dict) or part not in node:
                return {"type": "object"}
            node = node[part]
        return _dereference(node, spec, depth + 1, seen + [ref])
    resolved: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in ("title", "example", "examples"):
            continue  # noise for tool schemas
        resolved[key] = _dereference(value, spec, depth + 1, seen)
    return resolved


def _operation_parameters(operation: Dict[str, Any], spec: Dict[str, Any]) -> Dict[str, Any]:
    """Build the OpenAI `parameters` JSON schema for an mcpo POST endpoint."""
    request_body = operation.get("requestBody") or {}
    content = request_body.get("content") or {}
    json_content = content.get("application/json") or {}
    schema = _dereference(json_content.get("schema") or {}, spec, 0, [])
    if not isinstance(schema, dict):
        schema = {}
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": schema.get("properties", {})}
    schema.setdefault("properties", {})
    return schema
class McpoTool(Tool):
    """One MCP tool exposed through mcpo as a POST endpoint."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: Dict[str, Any],
        path: str,
        client: httpx.AsyncClient,
        result_max_chars: int,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.path = path
        self._client = client
        self._result_max_chars = result_max_chars
        self.source = "mcpo"

    async def execute(self, arguments: Dict[str, Any], context: ToolContext) -> str:
        response = await self._client.post(self.path, json=arguments)
        if response.status_code >= 400:
            snippet = response.text[:300].replace("\n", " ")
            raise RuntimeError(f"mcpo returned HTTP {response.status_code}: {snippet}")
        body = response.text.strip()
        if not body:
            return "(tool returned no output)"
        if len(body) > self._result_max_chars:
            body = body[: self._result_max_chars] + "…[truncated]"
        return body


class McpoTools:
    """Fetches and converts the mcpo OpenAPI spec into executable tools."""

    def __init__(
        self,
        base_url: str,
        api_key: Optional[str],
        timeout_seconds: float,
        result_max_chars: int,
        max_tools: int,
    ):
        base = base_url.rstrip("/")
        self._openapi_url = f"{base}/openapi.json"
        headers: Dict[str, str] = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout_seconds)
        self._result_max_chars = result_max_chars
        self._max_tools = max_tools

    async def aclose(self) -> None:
        await self._client.aclose()

    async def ping(self, timeout: float = 3.0) -> bool:
        try:
            response = await self._client.get(self._openapi_url, timeout=timeout)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def fetch_tools(self) -> List[Tool]:
        response = await self._client.get(self._openapi_url)
        if response.status_code != 200:
            raise RuntimeError(f"mcpo /openapi.json returned HTTP {response.status_code}")
        try:
            spec = response.json()
        except ValueError as exc:
            raise RuntimeError("mcpo /openapi.json is not valid JSON") from exc
        if not isinstance(spec, dict):
            raise RuntimeError("mcpo /openapi.json has an unexpected shape")

        tools: List[Tool] = []
        for path, methods in (spec.get("paths") or {}).items():
            if not isinstance(path, str) or not path.startswith("/") or path == "/":
                continue
            operation = methods.get("post") if isinstance(methods, dict) else None
            if not isinstance(operation, dict):
                continue  # mcpo exposes one POST per MCP tool; skip the rest
            name = _sanitize_tool_name(path)
            description = str(operation.get("summary") or operation.get("description") or name).strip()
            parameters = _operation_parameters(operation, spec)
            tools.append(
                McpoTool(
                    name=name,
                    description=description or name,
                    parameters=parameters,
                    path=path,
                    client=self._client,
                    result_max_chars=self._result_max_chars,
                )
            )
            if len(tools) >= self._max_tools:
                logger.warning(
                    "Capping mcpo tools at AICORE_MCPO_MAX_TOOLS=%d — raise the "
                    "limit if all tools are needed.",
                    self._max_tools,
                )
                break
        return tools

