import sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from interval_analysis import validate_analysis, numeric_intervals, state_intervals
from test_workbench import Store, seed, series, file_request


def rows(values,times=None):
    start=datetime(2024,1,1,tzinfo=timezone.utc)
    return [{'timestamp_utc':(start+timedelta(seconds=i)).isoformat(),'value':v,'raw':v,'quality':'ok'} for i,v in zip(times or range(len(values)),values)]


@pytest.mark.parametrize('fn,expected',[('min',1),('max',3),('mean',2),('sum',6),('count',3),('stddev',(2/3)**.5)])
def test_numeric_bins(fn,expected):
    c=validate_analysis({'kind':'numeric','function':fn,'interval':10,'unit':'second'})
    result=list(numeric_intervals(rows([1,2,None,3,9],[0,1,2,9,10]),c))
    assert len(result)==2 and result[0]['value']==pytest.approx(expected)
    assert result[0]['timestamp_utc']=='2024-01-01T00:00:10+00:00'
    assert 'input_samples=3;missing=1' in result[0]['quality']


@pytest.mark.parametrize('unit,interval,expected_end',[('second',10,'00:00:10'),('minute',1,'00:01:00'),('hour',1,'01:00:00'),('day',1,'16:00:00')])
def test_alignment_taipei(unit,interval,expected_end):
    c=validate_analysis({'kind':'numeric','function':'mean','interval':interval,'unit':unit})
    assert list(numeric_intervals(rows([1,2]),c))[0]['timestamp_utc']=='2024-01-01T'+expected_end+'+00:00'


@pytest.mark.parametrize('fn,expected',[('on_seconds',6),('off_seconds',4),('on_percent',60),('switches',2),('on_count',1),('off_count',1)])
def test_state_durations_and_transition_boundary(fn,expected):
    c=validate_analysis({'kind':'state','function':fn,'interval':10,'unit':'second','max_gap_seconds':10})
    result=list(state_intervals(rows([0,1,0,0],[0,2,8,10]),c))
    assert result[0]['value']==pytest.approx(expected)
    assert 'covered_seconds=10' in result[0]['quality']


def test_state_missing_and_gaps_are_unknown_and_no_extrapolation():
    c=validate_analysis({'kind':'state','function':'on_seconds','interval':10,'unit':'second','max_gap_seconds':5})
    result=list(state_intervals(rows([1,1,None,0,0],[0,2,4,20,22]),c))
    assert result[0]['value']==2
    assert 'unknown_seconds=8' in result[0]['quality']
    assert result[1]['value'] is None
    assert result[2]['value']==0 and 'covered_seconds=2' in result[2]['quality']
    assert list(state_intervals(rows([1]),c))==[]


def test_switch_at_boundary_belongs_to_next_interval():
    c=validate_analysis({'kind':'state','function':'switches','interval':10,'unit':'second','max_gap_seconds':10})
    result=list(state_intervals(rows([0,1],[0,10]),c))
    assert [r['value'] for r in result]==[0,1]


def test_new_series_can_plot_and_export_without_mutating_original(tmp_path):
    s=Store(tmp_path);seed(s)
    config=series(name='Mean per 5 seconds',analysis={'kind':'numeric','function':'mean','interval':5,'unit':'second'})
    r=s.workbench_query({'series':[series(),config]})
    assert r['series'][0]['rows']==10 and r['series'][1]['rows']==2
    assert [p['value'] for p in r['series'][1]['points']]==[1.5,7]
    with s.workbench_export({'series':[config]},'csv') as f:content=f.read().decode('utf-8-sig')
    assert 'analysis_mean' in content and len(content.splitlines())==3
    with s.workbench_export({'series':[series(),config]},'csv') as f:both=f.read()
    imported=Store(tmp_path/'roundtrip')
    assert imported.import_table(file_request(both))['inserted']==12
    assert len(imported.catalog()['series'])==2
    with s.connect() as db:assert db.execute('SELECT COUNT(*) FROM samples').fetchone()[0]==40


def test_strict_merge_same_deployment_series_and_no_overlap(tmp_path):
    s=Store(tmp_path)
    head='timestamp,device,deployment,channel,value,units,label,measurement\n'
    def file(stamp,channel='T',label='Temperature',measurement='Temperature',deployment='D'):
        return file_request(head+f'{stamp},A,{deployment},{channel},10,*C,{label},{measurement}\n',mode='strict_merge')
    assert s.import_table(file('2024-01-01T08:00:00'))['inserted']==1
    assert s.import_table(file('2024-01-01T08:10:00'))['inserted']==1
    # Another content hash at the same timestamp is still an overlap, not a merge.
    overlap=file('2024-01-01T08:10:00');overlap['content_base64']=file_request(head+'2024-01-01T08:10:00+08:00,A,D,T,10,*C,Temperature,Temperature\n')['content_base64']
    with pytest.raises(ValueError,match='overlapping'):s.import_table(overlap)
    with pytest.raises(ValueError,match='identical series'):s.import_table(file('2024-01-01T08:20:00',channel='Other'))
    with pytest.raises(ValueError,match='different metadata'):s.import_table(file('2024-01-01T08:20:00',label='Different'))
    two=head+'2024-01-01T08:20:00,A,D,T,10,*C,Temperature,Temperature\n2024-01-01T08:30:00,A,Other,T,10,*C,Temperature,Temperature\n'
    with pytest.raises(ValueError,match='one deployment'):s.import_table(file_request(two,mode='strict_merge'))
    assert sum(c['rows'] for c in s.catalog()['series'])==2
