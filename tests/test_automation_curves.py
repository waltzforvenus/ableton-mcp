"""Synthetic lexical fixtures; no playback/interpolation certification."""
import pytest
from ableton_mcp.automation import Automation


@pytest.fixture
def curve(tmp_path):
    p=tmp_path/'curve.xml'
    p.write_text('''<Ableton MajorVersion="5" MinorVersion="fixture" Creator="Synthetic">
<AutomationEnvelope><EnvelopeTarget><PointeeId Value="10"/></EnvelopeTarget><Automation><Events>
<FloatEvent Id="0" Time="-63072000" Value="0.2"/>
<FloatEvent Id="1" Time="0" Value="0.2" CurveControl1X="0.25" CurveControl1Y="0" CurveControl2X="0.75" CurveControl2Y="1"/>
<FloatEvent Id="2" Time="16" Value="0.8"/>
</Events></Automation></AutomationEnvelope></Ableton>''')
    return p


def test_verse_chorus_stored_points_never_invent_interpolation(curve):
    before=curve.read_bytes()
    result=Automation().inspect_curve(str(curve),'10',[-63072000,0,8,16,32])
    assert result['schema_qualified'] and not result['native_qualified']
    assert result['version_metadata']['MinorVersion']=='fixture'
    assert result['events'][1]['curve_controls']['CurveControl1X']==.25
    assert [s['stored_value'] for s in result['samples']]==[None,.2,None,.8,None]
    assert all(s['evaluated_value'] is None for s in result['samples'])
    assert result['samples'][2]['status']=='interpolation_unsupported'
    assert result['samples'][0]['status']=='negative_time_semantics_unqualified'
    assert not result['evaluation_supported'] and curve.read_bytes()==before


@pytest.mark.parametrize('fault',['partial','nested','unknown','duplicate','nan','ambiguous'])
def test_unknown_curve_schema_refuses(curve,fault):
    s=curve.read_text()
    if fault=='partial': s=s.replace(' CurveControl2Y="1"','')
    if fault=='nested': s=s.replace('Time="16" Value="0.8"/>','Time="16" Value="0.8"><Opaque/></FloatEvent>')
    if fault=='unknown': s=s.replace('Time="16"','Mystery="x" Time="16"')
    if fault=='duplicate': s=s.replace('Time="16"','Time="0"')
    if fault=='nan': s=s.replace('CurveControl1X="0.25"','CurveControl1X="NaN"')
    if fault=='ambiguous': s=s.replace('</Ableton>',s[s.index('<AutomationEnvelope>'):s.index('</Ableton>')]+'</Ableton>')
    curve.write_text(s); before=curve.read_bytes()
    with pytest.raises(ValueError): Automation().inspect_curve(str(curve),'10',[8])
    assert curve.read_bytes()==before


@pytest.mark.parametrize('times',[[True],[float('inf')],list(range(1025))])
def test_sample_bound_and_numeric_validation(curve,times):
    with pytest.raises(ValueError): Automation().inspect_curve(str(curve),'10',times)
