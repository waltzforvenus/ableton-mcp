"""Explicit transport adapter; direct controllers retain legacy presentation.

JSON object presenters become the canonical object, never {result: JSON text}.
Legacy prose has only a message; this adapter does not invent missing model data.
"""
import functools
import inspect
import json
from mcp.types import CallToolResult, TextContent
from .handshake import CapabilityError
from .live_lock import LockError
from .render import RenderRefused
from .services import AudioAuthRequired


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
            payload = canonical_payload(controller(*args, **kwargs))
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
