"""Actual MCP memory transport; no listeners/Live."""
import asyncio
import json
from mcp.shared.memory import create_connected_server_and_client_session
from ableton_mcp.app import build_app
from test_full_stack import _make_deps,_ctx
from ableton_mcp import tools


def call(mode,name,args):
    deps,_=_make_deps()
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps,output_mode=mode)) as session:
            return await session.call_tool(name,args)
    return asyncio.run(run())


def test_canonical_structured_object_no_nested_json():
    result=call('structured','get_session_info',{})
    assert result.content==[] and isinstance(result.structuredContent,dict)
    assert 'result' not in result.structuredContent
    assert result.structuredContent['track_count']==3
    encoded=json.loads(result.model_dump_json(by_alias=True,exclude_none=True))
    assert encoded['structuredContent']['track_count']==3


def test_explicit_legacy_adapter_keeps_existing_text():
    result=call('legacy','get_session_info',{})
    deps,_=_make_deps()
    assert result.content[0].text==tools.get_session_info(_ctx(deps))


def test_both_adapter_text_is_same_canonical_object():
    result=call('both','get_session_info',{})
    assert json.loads(result.content[0].text)==result.structuredContent


def test_structured_errors_have_stable_code_message_details():
    result=call('structured','set_device_parameter',{'track_index':0,'device_index':0,'value':.5})
    assert result.isError and result.content==[]
    assert set(result.structuredContent)=={'code','message','details'}
    assert result.structuredContent['code']=='invalid_argument'


def test_legacy_prose_success_is_explicit_message_not_json_string():
    result=call('structured','set_track_volume',{'track_index':0,'value':.4})
    assert isinstance(result.structuredContent['message'],str) and not result.isError


def test_bad_output_mode_refused():
    import pytest
    with pytest.raises(ValueError): build_app(output_mode='unknown')


def test_unicode_payload_round_trip_without_text_duplication():
    deps,_=_make_deps()
    deps.service.get_session_info=lambda: {'name':'音楽 🎹','nested':{'notes':[1,2]}}
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps)) as session:
            return await session.call_tool('get_session_info',{})
    result=asyncio.run(run())
    assert result.structuredContent=={'name':'音楽 🎹','nested':{'notes':[1,2]}}
    wire=result.model_dump_json(by_alias=True,exclude_none=True)
    assert len(wire.encode('utf-8'))>len(wire) and result.content==[]


def test_service_failure_is_error_not_success_message():
    deps,_=_make_deps()
    def fail(): raise RuntimeError('read failed')
    deps.service.get_session_info=fail
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps)) as session:
            return await session.call_tool('get_session_info',{})
    result=asyncio.run(run())
    assert result.isError and result.structuredContent=={
        'code':'operation_failed','message':'read failed','details':{'tool':'get_session_info'}}


def test_nonfinite_json_never_leaks_invalid_structured_numbers():
    deps,_=_make_deps()
    deps.service.get_session_info=lambda: {'value':float('nan')}
    async def run():
        async with create_connected_server_and_client_session(build_app(deps=deps)) as session:
            return await session.call_tool('get_session_info',{})
    result=asyncio.run(run())
    assert result.isError and result.structuredContent['code']=='invalid_argument'
