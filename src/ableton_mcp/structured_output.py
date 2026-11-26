"""Explicit transport adapter; direct controllers retain legacy presentation.

JSON object presenters become the canonical object, never {result: JSON text}.
Legacy prose has only a message; this adapter does not invent missing model data.
"""
import functools
import inspect
import json
from mcp.types import CallToolResult, TextContent, ImageContent, AudioContent, EmbeddedResource, ResourceLink
from .handshake import CapabilityError
from .live_lock import LockError
from .render import RenderRefused
from .services import AudioAuthRequired
from .snapshot_output import paginate
from .presentation import MetadataText, MetadataBlocks
from mcp.server.fastmcp import Image, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError


def canonical_payload(value):
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (ValueError, TypeError):
            return {'message': value}
        value = decoded
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {'items': value}
    return {'value': value}


def error_payload(exc, tool):
    if isinstance(exc, CapabilityError):
        code = 'capability_unavailable'
    elif isinstance(exc, LockError):
        code = 'lock_refused'
    elif isinstance(exc, AudioAuthRequired):
        code = 'audio_authorization_required'
    elif isinstance(exc, RenderRefused):
        code = 'render_refused'
    elif isinstance(exc, (ValueError, TypeError, IndexError)):
        code = 'invalid_argument'
    else:
        code = 'operation_failed'
    return {'code': code, 'message': str(exc), 'details': {'tool': tool}}


def structured_tool(fn, mode):
    if mode not in ('structured', 'both'):
        raise ValueError('structured adapter mode must be structured or both')
    controller = fn.__wrapped__  # typed exceptions must not become legacy prose

    @functools.wraps(fn)
    def invoke(*args, **kwargs):
        failed = False
        try:
            bound = inspect.signature(fn).bind(*args, **kwargs)
            bound.apply_defaults()
            snapshot_budget = fn.__name__ == 'get_session_snapshot'
            if snapshot_budget:
                budget = bound.arguments['max_bytes']
                if budget is None:
                    legacy_cap = bound.arguments['max_chars']
                    # Legacy text retains its original character heuristic.
                    # Structured mode uses the positive compatibility cap as
                    # bytes; old unlimited switches retain the safe default.
                    budget = 40000 if legacy_cap is None or legacy_cap == 0 else legacy_cap
                cursor = bound.arguments['cursor']
                if type(budget) is not int or budget <= 0:
                    raise ValueError('max_bytes budget must be a positive integer')
                scope = {key: bound.arguments[key] for key in (
                    'include_notes', 'include_params', 'include_warp_markers',
                    'include_rack_chains', 'include_empty_slots', 'tracks')}
                bound.arguments.update(max_chars=0, max_bytes=None, cursor=None)
                payload = canonical_payload(controller(*bound.args, **bound.kwargs))
                return paginate(payload, scope, budget, cursor, mode)
            value = controller(*args, **kwargs)
            if isinstance(value, (MetadataText, MetadataBlocks)):
                # Multimodal views deliberately retain their original human
                # text/image blocks in both structured and compatibility modes.
                # Only the explicit raw model metadata enters structuredContent.
                payload = value.metadata
                json.dumps(payload, ensure_ascii=False, allow_nan=False)
                blocks = [value] if isinstance(value, MetadataText) else value
                content = []
                for block in blocks:
                    if isinstance(block, Image):
                        content.append(block.to_image_content())
                    elif isinstance(block, (TextContent, ImageContent, AudioContent,
                                            EmbeddedResource, ResourceLink)):
                        content.append(block)
                    elif isinstance(block, str):
                        content.append(TextContent(type='text', text=block))
                    else:
                        raise TypeError('unsupported multimodal content block')
                return CallToolResult(content=content, structuredContent=payload, isError=False)
            payload = canonical_payload(value)
            # Validate finite JSON before a transport can serialize NaN differently.
            encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        except Exception as exc:
            failed = True
            payload = error_payload(exc, fn.__name__)
            encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        content = [TextContent(type='text', text=encoded)] if mode == 'both' else []
        return CallToolResult(content=content, structuredContent=payload, isError=failed)

    invoke.__signature__ = inspect.signature(fn).replace(return_annotation=CallToolResult)
    invoke.__annotations__ = dict(fn.__annotations__)
    invoke.__annotations__['return'] = CallToolResult
    return invoke


# Override the public dispatch seam, not SDK private handler/manager tables.
# The SDK wraps argument-validation exceptions in ToolError with a typed cause.
class StructuredFastMCP(FastMCP):
    """Normalize supported tool dispatch failures; leave protocol errors alone."""

    def __init__(self, *args, output_mode='structured', **kwargs):
        if output_mode not in ('structured', 'both'):
            raise ValueError('structured dispatch mode must be structured or both')
        self.output_mode = output_mode
        super().__init__(*args, **kwargs)

    async def call_tool(self, name, arguments):
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            cause = exc.__cause__
            details = {'tool': name}
            if isinstance(cause, ValidationError):
                code = 'invalid_argument'
                message = 'Tool arguments failed validation'
                # No input echo, context objects or prose parsing. SDK locations
                # and error types are the actual validation evidence.
                details['validation'] = [
                    {'loc': list(error['loc']), 'type': error['type']}
                    for error in cause.errors(include_input=False, include_context=False,
                                              include_url=False)
                ]
            elif name not in {tool.name for tool in await self.list_tools()}:
                code = 'unknown_tool'
                message = 'Unknown tool'
            else:
                code = 'operation_failed'
                message = str(exc)
            payload = {'code': code, 'message': message, 'details': details}
            content = []
            if self.output_mode == 'both':
                content = [TextContent(type='text', text=json.dumps(
                    payload, ensure_ascii=False, separators=(',', ':'), allow_nan=False))]
            return CallToolResult(content=content, structuredContent=payload, isError=True)
