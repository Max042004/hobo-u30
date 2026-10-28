import base64
import csv
import io
import json
import math
import sys
import threading
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
import web_server
from web_server import Store, App, Handler
from data_assistants import grains_per_pound, validate_assistant, gdd, utc


def seed(store, n=10):
    with store.connect() as db:
        for key, units in [('T','*C'),('RH','%'),('PAR','uE'),('pulse','count')]:
            store.channel(db, {'key':key,'label':key,'units':units,'measurement':key,'kind':'pulse' if key=='pulse' else 'sensor'})
        start=datetime(2024,1,1,tzinfo=timezone.utc)
        store.insert_samples(db, [('dev','launch','live',key,(start+timedelta(seconds=i)).isoformat(),i,
                                  50 if key=='RH' else None if i==4 else i,'ok')
                                 for key in ('T','RH','PAR','pulse') for i in range(n)])


def series(channel='T', **kwargs):
    return {'device':'dev','deployment':'launch','source':'live','channel':channel,**kwargs}


def file_request(content, filename='test.csv', **kwargs):
    return {'filename':filename,'content_base64':base64.b64encode(content.encode() if isinstance(content,str) else content).decode(),**kwargs}


def test_query_precise_full_statistics_and_scope(tmp_path):
    store=Store(tmp_path);seed(store,1000)
    r=store.workbench_query({'series':[series(),series('PAR',assistant={'kind':'linear','slope':2,'intercept':1,'units':'ppm'})],'limit':100})
    s=r['series'][0]
    assert s['rows']==1000 and s['decimated'] and len(s['points'])<=150
    assert s['statistics']['count']==999 and s['statistics']['missing']==1
    assert s['statistics']['mean']==pytest.approx((sum(range(1000))-4)/999)
    assert r['series'][1]['statistics']['max']==1999
    assert r['series'][1]['configuration']['units']=='ppm'
    assert any(p['value'] is None for p in s['points'])


def test_range_device_deployment_isolation_and_injection(tmp_path):
    store=Store(tmp_path);seed(store)
    r=store.workbench_query({'series':[series(start='2024-01-01T08:00:05+08:00',end='2024-01-01T08:00:07+08:00')]})
    assert r['series'][0]['statistics']['count']==3
    assert store.workbench_query({'series':[series(device="dev' OR 1=1 --")]})['series'][0]['rows']==0
    with pytest.raises(ValueError):store.workbench_query({'series':[series(start='2025-01-01',end='2024-01-01')]})


def test_grains_reference_and_missing_pairs(tmp_path):
    assert grains_per_pound(25,50,101325)==pytest.approx(69.1673179,rel=1e-6)
    assert grains_per_pound(25,0,101325)==0
    assert grains_per_pound(-10,50,101325)>0
    with pytest.raises(ValueError):grains_per_pound(25,110,101325)
    store=Store(tmp_path);seed(store)
    with store.connect() as db:db.execute("DELETE FROM samples WHERE channel='RH' AND raw=5")
    r=store.workbench_query({'series':[series(assistant={'kind':'gpp','humidity_channel':'RH'})]})['series'][0]
    assert r['statistics']['missing']==2
    assert r['points'][5]['quality']=='assistant_missing_matching_humidity'


def test_pulse_is_not_arbitrary_par_scaling(tmp_path):
    store=Store(tmp_path);seed(store)
    with pytest.raises(ValueError,match='pulse/count'):
        store.workbench_query({'series':[series('PAR',assistant={'kind':'pulse','raw_units':1,'scaled_units':.2})]})
    r=store.workbench_query({'series':[series('pulse',assistant={'kind':'pulse','raw_units':2,'scaled_units':.4})]})
    assert r['series'][0]['statistics']['max']==pytest.approx(1.8)
    with pytest.raises(ValueError):validate_assistant({'kind':'pulse','raw_units':0,'scaled_units':1},{'kind':'pulse'})


@pytest.mark.parametrize('method',['average','triangle','triangle_horizontal','sine','sine_horizontal','actual','actual_horizontal','actual_vertical'])
def test_gdd_full_calendar_days(method):
    start=datetime(2024,1,1,tzinfo=timezone(timedelta(hours=8)))
    rows=[{'timestamp_utc':(start+timedelta(hours=i)).astimezone(timezone.utc).isoformat(),'value':20} for i in range(49)]
    config=validate_assistant({'kind':'gdd','method':method,'lower':10,'upper':30,'max_gap_seconds':3600},{'units':'*C'})
    result=list(gdd(rows,config,'*C'))
    assert len(result)==2 and [r['value'] for r in result]==pytest.approx([10,10])
    assert result[0]['timestamp_utc']=='2024-01-01T04:00:00+00:00'
    broken=rows[:10]+rows[11:]
    assert list(gdd(broken,{**config,'cumulative':True},'*C'))[1]['value'] is None


def test_average_original_high_endpoint_behavior():
    start=datetime(2024,1,1,tzinfo=timezone(timedelta(hours=8)))
    rows=[{'timestamp_utc':(start+timedelta(hours=i)).isoformat(),'value':35 if i%2 else 40} for i in range(25)]
    c=validate_assistant({'kind':'gdd','method':'average','lower':10,'upper':30},{'units':'*C'})
    assert list(gdd(rows,c,'*C'))[0]['value']==22.5


def test_import_merge_conflicts_rollback_and_metadata(tmp_path):
    s=Store(tmp_path)
    text='timestamp_gmt8,device,deployment,channel,value,units\n2024-01-01T08:00:00+08:00,A,D,T,10,*C\n'
    r=s.import_table(file_request(text));assert r['inserted']==1
    assert s.import_table(file_request(text))['already_imported']
    more=text+'2024-01-01T08:00:01+08:00,A,D,T,11,*C\n'
    assert s.import_table(file_request(more))['inserted']==1
    conflict=text+'2024-01-01T08:00:02+08:00,A,D,T,12,*C\n2024-01-01T08:00:00+08:00,A,D,T,999,*C\n'
    with pytest.raises(ValueError,match='Conflicting'):s.import_table(file_request(conflict))
    assert sum(c['rows'] for c in s.catalog()['series'])==2
    assert s.catalog()['series'][0]['first_utc']=='2024-01-01T00:00:00+00:00'
    with pytest.raises(ValueError,match='different metadata'):s.import_table(file_request(text.replace('*C','ppm')))


def test_import_deleted_stays_deleted(tmp_path):
    s=Store(tmp_path);r=s.import_table(file_request('timestamp,T\n2024-01-01,10\n'))
    c=s.catalog()['series'][0]
    filters={k:[c[k]] for k in ('device','deployment','source','channel')}
    filters.update(start=['2024-01-01T00:00:00+08:00'],end=['2024-01-01T00:00:01+08:00'])
    plan=s.preview_delete(filters)
    s.trash_samples(plan)
    assert s.import_table(file_request('timestamp,T\n2024-01-01,10\n2024-01-01T00:00:01,11\n'))['inserted']==1


def test_import_wide_missing_and_pulse_declaration(tmp_path):
    s=Store(tmp_path)
    r=s.import_table(file_request('timestamp\tP\tT\n2024-01-01T08:00:00\t2\t\n',filename='wide.txt',pulse_channels=['P'],units='count'))
    assert r['inserted']==2
    channels=s.catalog()['series']
    assert any(c['metadata']['kind']=='pulse' for c in channels)
    assert utc('2024-01-01T08:00:00')=='2024-01-01T00:00:00+00:00'


@pytest.mark.parametrize('format',['csv','txt','xls'])
def test_exports_timezone_numeric_and_formula_safety(tmp_path,format):
    s=Store(tmp_path);seed(s)
    with s.workbench_export({'series':[series(name='=1+1')],'events':True},format) as f:content=f.read()
    if format=='xls':
        import xlrd
        assert content.startswith(bytes.fromhex('D0CF11E0A1B11AE1'))
        book=xlrd.open_workbook(file_contents=content);sheet=book.sheet_by_index(0)
        assert sheet.cell_value(1,1)=='2024-01-01T08:00:00+08:00'
        assert sheet.cell_type(1,10)==xlrd.XL_CELL_NUMBER
        assert sheet.cell_value(1,6)=='=1+1' and sheet.cell_type(1,6)==xlrd.XL_CELL_TEXT
    else:
        rows=list(csv.reader(io.StringIO(content.decode('utf-8-sig')),delimiter='\t' if format=='txt' else ','))
        assert len(rows)==11 and rows[1][1]=='2024-01-01T08:00:00+08:00' and rows[1][6]=="'=1+1"


def test_xls_sheet_boundary_and_import(tmp_path):
    s=Store(tmp_path);seed(s,65537)
    with s.workbench_export({'series':[series()]},'xls') as f:content=f.read()
    import xlrd
    book=xlrd.open_workbook(file_contents=content)
    assert [sheet.nrows for sheet in book.sheets()]==[65536,3]
    other=Store(tmp_path/'other')
    with s.workbench_export({'series':[series(end='2024-01-01T00:00:09+00:00')]},'xls') as f:small=f.read()
    assert other.import_table(file_request(small,filename='roundtrip.xls'))['inserted']==10


def test_observed_events_do_not_claim_native_alarm_timestamp(tmp_path):
    s=Store(tmp_path)
    before={'serial':'A','observed_utc':'2024-01-01T00:00:00+00:00','archive':{'deployment':1,'launched_utc':'2024-01-01'},'logging':True,'sensors':[],
            'alarms':{'current':'open'},'battery_percent':30}
    with s.connect() as db:db.execute("INSERT INTO state VALUES ('snapshot',?)",(json.dumps(before),))
    after={**before,'observed_utc':'2024-01-01T00:00:02+00:00','alarms':{'current':'closed'},'battery_percent':19}
    with s.connect() as db:s.observe_snapshot(db,after)
    events=s.workbench_events({'series':[{'device':'A'}]})
    assert {e['event_type'] for e in events}=={'relay_state','low_battery_20_percent'}
    assert all(e['origin']=='pi_observed' for e in events)
    assert next(e for e in events if e['event_type']=='relay_state')['details']['previous_observation_utc']==before['observed_utc']


def test_http_workbench_readonly_export_and_authenticated_import(tmp_path, monkeypatch):
    app=App(tmp_path,2);seed(app.store)
    monkeypatch.setattr(web_server,'network_request',lambda action='status',data=None: ({'authorized':True},200) if data and data.get('setup_key')=='test-only-key' else ({'error':'wrong key'},403))
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);server.app=app
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    def request(path,body,origin=None):
        r=urllib.request.Request(base+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':origin or base})
        try:
            with urllib.request.urlopen(r,timeout=5) as response:return response.status,response.read(),response.headers
        except urllib.error.HTTPError as e:return e.code,e.read(),e.headers
    try:
        status,body,_=request('/api/studio/query',{'series':[series()]})
        assert status==200 and json.loads(body)['series'][0]['rows']==10
        assert not app.sync_requested and not app.deployment_requested
        status,body,headers=request('/api/studio/export',{'series':[series()],'format':'xls'})
        assert status==200 and body.startswith(bytes.fromhex('D0CF11E0A1B11AE1')) and 'ms-excel' in headers['Content-Type']
        upload=file_request('timestamp,X\n2024-01-01,1\n')
        assert request('/api/studio/import',upload)[0]==403
        upload['setup_key']='test-only-key'
        assert request('/api/studio/import',upload,origin='http://other.invalid')[0]==403
        assert request('/api/studio/import',upload)[0]==200
        assert request('/api/studio/query',{'series':[series(assistant={'kind':'arbitrary_code'})]})[0]==400
    finally:
        server.shutdown();server.server_close();thread.join()
