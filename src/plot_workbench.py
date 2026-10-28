"""Multi-series queries, portable data imports and genuine BIFF8 exports."""
import base64
import csv
import hashlib
import io
import json
import math
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from data_assistants import TAIPEI, number, utc, celsius, grains_per_pound, validate_assistant, gdd
from interval_analysis import validate_analysis, interval_rows

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'vendor'))
MAX_ROWS = 500000
EXPORT_COLUMNS = ('record_type', 'timestamp_gmt8', 'device', 'deployment', 'source', 'channel',
                  'label', 'measurement', 'units', 'raw', 'value', 'quality', 'event_type', 'event_origin',
                  'source_channel', 'data_assistant', 'interval_analysis')


def text(value, name='Text', maximum=256):
    if not isinstance(value, str) or len(value) > maximum or '\0' in value:
        raise ValueError(name+' is invalid or too long')
    return value


def stats_update(state, value):
    if value is None:
        state['missing'] += 1
        return
    state['count'] += 1
    delta = value-state['mean']
    state['mean'] += delta/state['count']
    state['m2'] += delta*(value-state['mean'])
    state['min'] = value if state['min'] is None else min(value, state['min'])
    state['max'] = value if state['max'] is None else max(value, state['max'])
    state['sum'] += value


def new_stats():
    return dict(count=0, missing=0, min=None, max=None, mean=0, m2=0, sum=0)


def final_stats(state):
    state = dict(state)
    state['stddev'] = math.sqrt(max(0, state.pop('m2')/state['count'])) if state['count'] else None
    if not state['count']:
        state['mean'] = None
    if any(isinstance(v, float) and not math.isfinite(v) for v in state.values()):
        raise ValueError('Statistics overflow; use a smaller conversion scale')
    return state


class PlotWorkbench:
    @staticmethod
    def init_workbench(db):
        db.executescript('''
          CREATE INDEX IF NOT EXISTS samples_series ON samples(device,deployment,source,channel,timestamp_utc);
          CREATE TABLE IF NOT EXISTS events (
            id TEXT PRIMARY KEY, device TEXT, deployment TEXT, timestamp_utc TEXT,
            event_type TEXT, origin TEXT, details TEXT);
          CREATE INDEX IF NOT EXISTS events_time ON events(timestamp_utc);
          CREATE TABLE IF NOT EXISTS imports (id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
        ''')

    def catalog(self):
        with self.connect() as db:
            metadata = {r[0]: json.loads(r[1]) for r in db.execute('SELECT * FROM channels')}
            result = []
            for row in db.execute('''SELECT device,deployment,source,channel,COUNT(*) AS rows,
                    MIN(timestamp_utc) AS first_utc,MAX(timestamp_utc) AS last_utc
                    FROM samples GROUP BY device,deployment,source,channel ORDER BY device,deployment,source,channel'''):
                result.append({**dict(row), 'metadata': metadata.get(row['channel'], {})})
            imports = [json.loads(r[0]) for r in db.execute('SELECT metadata FROM imports ORDER BY rowid DESC LIMIT 100')]
        return {'series': result, 'imports': imports, 'timezone': 'Asia/Taipei', 'query_row_limit': MAX_ROWS,
                'hardware_batch': False, 'native_event_decoding': False}

    def validate_series(self, config, db):
        if not isinstance(config, dict):
            raise ValueError('Invalid series configuration')
        result = {k: text(config.get(k, ''), k) for k in ('device', 'deployment', 'source', 'channel')}
        if not all(result.values()) or result['source'] not in ('live', 'archive', 'import'):
            raise ValueError('Select a device, deployment, source and channel')
        metadata = db.execute('SELECT metadata FROM channels WHERE key=?', (result['channel'],)).fetchone()
        if not metadata:
            raise ValueError('Channel no longer exists; open the embedded project snapshot instead')
        result['metadata'] = json.loads(metadata[0])
        for k in ('start', 'end'):
            result[k] = utc(config[k]) if config.get(k) else None
        if result['start'] and result['end'] and result['start'] > result['end']:
            raise ValueError('End time must not precede start time')
        result['assistant'] = validate_assistant(config.get('assistant', {}), result['metadata'])
        result['name'] = text(config.get('name', '') or result['metadata'].get('label') or result['channel'], 'Series name')
        result['units'] = result['assistant'].get('units', result['metadata'].get('units', ''))
        if result['assistant']['kind'] == 'gpp':
            result['units'] = 'grains/lb dry air'
            rh = db.execute('SELECT metadata FROM channels WHERE key=?', (result['assistant']['humidity_channel'],)).fetchone()
            if not rh or json.loads(rh[0]).get('units') not in ('%', '%RH', 'RH%'):
                raise ValueError('Humidity channel must use % RH units')
        elif result['assistant']['kind'] == 'gdd':
            result['units'] = '°C·day'
        result['analysis'] = validate_analysis(config.get('analysis'))
        if result['analysis']:
            fn = result['analysis']['function']
            if fn in ('count','switches','on_count','off_count'):
                result['units'] = 'count'
            elif fn in ('on_seconds','off_seconds'):
                result['units'] = 's'
            elif fn == 'on_percent':
                result['units'] = '%'
        return result

    @staticmethod
    def series_where(config):
        where = ' WHERE device=? AND deployment=? AND source=? AND channel=?'
        args = [config[k] for k in ('device', 'deployment', 'source', 'channel')]
        for k, op in (('start', '>='), ('end', '<=')):
            if config[k]:
                where += ' AND timestamp_utc'+op+'?'
                args.append(config[k])
        return where, args

    def series_rows(self, db, config):
        rows = self.converted_rows(db, config)
        if not config.get('analysis'):
            yield from rows
            return
        for i, row in enumerate(interval_rows(rows, config['analysis'])):
            if i >= 100000:
                raise ValueError('Analysis exceeds 100,000 output intervals; increase the interval or shorten the range')
            yield row

    def converted_rows(self, db, config):
        where, args = self.series_where(config)
        count = db.execute('SELECT COUNT(*) FROM samples'+where, args).fetchone()[0]
        if count > MAX_ROWS:
            raise ValueError(f'Series exceeds {MAX_ROWS:,} rows. Select a shorter range or use the full CSV export.')
        rows = db.execute('SELECT timestamp_utc,raw,value,quality FROM samples'+where+' ORDER BY timestamp_utc', args)
        assistant = config['assistant']
        if assistant['kind'] == 'gdd':
            yield from gdd((dict(r) for r in rows), assistant, config['metadata'].get('units'))
            return
        rh_cursor = None
        rh_row = None
        if assistant['kind'] == 'gpp':
            rh_config = {**config, 'channel': assistant['humidity_channel']}
            rw, ra = self.series_where(rh_config)
            rh_cursor = iter(db.execute('SELECT timestamp_utc,value FROM samples'+rw+' ORDER BY timestamp_utc', ra))
            rh_row = next(rh_cursor, None)
        for raw in rows:
            row = dict(raw)
            value = row['value']
            if value is not None:
                try:
                    if assistant['kind'] == 'linear':
                        value = value*assistant['slope']+assistant['intercept']
                    elif assistant['kind'] == 'pulse':
                        value *= assistant['scaled_units']/assistant['raw_units']
                    elif assistant['kind'] == 'gpp':
                        while rh_row is not None and rh_row['timestamp_utc'] < row['timestamp_utc']:
                            rh_row = next(rh_cursor, None)
                        if rh_row is None or rh_row['timestamp_utc'] != row['timestamp_utc'] or rh_row['value'] is None:
                            value = None
                            row['quality'] = 'assistant_missing_matching_humidity'
                        else:
                            value = grains_per_pound(celsius(value, config['metadata'].get('units')), rh_row['value'], assistant['pressure_pa'])
                    if value is not None and not math.isfinite(value):
                        raise ValueError('Conversion overflow')
                except ValueError:
                    value = None
                    row['quality'] = 'assistant_invalid_input'
            row['value'] = value
            yield row

    def workbench_query(self, request):
        configs = request.get('series')
        if not isinstance(configs, list) or not 1 <= len(configs) <= 24:
            raise ValueError('Select between 1 and 24 series')
        limit = request.get('limit', 2000)
        if type(limit) is not int or not 100 <= limit <= 10000:
            raise ValueError('Point limit must be between 100 and 10000')
        result = []
        with self.connect() as db:
            db.execute('BEGIN')
            for config in configs:
                config = self.validate_series(config, db)
                where, args = self.series_where(config)
                count = db.execute('SELECT COUNT(*) FROM samples'+where, args).fetchone()[0]
                if config.get('analysis'):
                    bounds = db.execute('SELECT MIN(timestamp_utc),MAX(timestamp_utc) FROM samples'+where,args).fetchone()
                    if bounds[0]:
                        windows = int((datetime.fromisoformat(bounds[1])-datetime.fromisoformat(bounds[0])).total_seconds()/config['analysis']['seconds'])+2
                        if config['analysis']['kind'] == 'state' and windows > 100000:
                            raise ValueError('State analysis exceeds 100,000 intervals; increase the interval or shorten the range')
                        count = windows if config['analysis']['kind'] == 'state' else min(count, windows)
                stride = 1 if count <= limit else max(1, math.ceil(count/max(1, (limit-4)//2)))
                state, points, bucket = new_stats(), [], []
                first = last = None
                for i, row in enumerate(self.series_rows(db, config)):
                    if first is None:
                        first = row
                    last = row
                    stats_update(state, row['value'])
                    bucket.append(row)
                    if len(bucket) == stride:
                        points.extend(self.reduce_bucket(bucket))
                        bucket = []
                points.extend(self.reduce_bucket(bucket))
                if first:
                    points.extend((first, last))
                points = list({r['timestamp_utc']: r for r in points}.values())
                points.sort(key=lambda r: r['timestamp_utc'])
                result.append({'configuration': config, 'statistics': final_stats(state), 'points': points,
                               'rows': state['count']+state['missing'], 'decimated': len(points) < state['count']+state['missing']})
        return {'series': result, 'events': self.workbench_events(request) if request.get('events') else [],
                'statistics_scope': 'all source rows within each requested range', 'timezone': 'Asia/Taipei'}

    @staticmethod
    def reduce_bucket(bucket):
        if not bucket:
            return []
        valid = [r for r in bucket if r['value'] is not None]
        if len(valid) != len(bucket):
            # Mixed buckets retain extrema but use isolated markers: their
            # internal missing intervals cannot be reconstructed after reduction.
            selected = [next(r for r in bucket if r['value'] is None)]
            if valid:
                selected.extend((min(valid,key=lambda r:r['value']),max(valid,key=lambda r:r['value'])))
            return [{**r, 'plot_break': True} for r in selected]
        return [min(valid, key=lambda r: r['value']), max(valid, key=lambda r: r['value'])]

    def workbench_events(self, request):
        devices = {c.get('device') for c in request.get('series', [])}
        if not devices:
            return []
        starts = [utc(c['start']) for c in request['series'] if c.get('start')]
        ends = [utc(c['end']) for c in request['series'] if c.get('end')]
        where = ' WHERE device IN ('+','.join('?' for _ in devices)+')'
        args = list(devices)
        if len(starts) == len(request['series']):
            where += ' AND timestamp_utc>=?'
            args.append(min(starts))
        if len(ends) == len(request['series']):
            where += ' AND timestamp_utc<=?'
            args.append(max(ends))
        with self.connect() as db:
            return [{**dict(r), 'details': json.loads(r['details'])} for r in db.execute('SELECT * FROM events'+where+' ORDER BY timestamp_utc DESC LIMIT 2000', args)]

    @staticmethod
    def add_event(db, device, deployment, stamp, kind, details, origin='pi_observed'):
        body = json.dumps(details, ensure_ascii=False, sort_keys=True)
        identity = hashlib.sha256(json.dumps([device, deployment, stamp, kind, origin, body]).encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?,?)', (identity, device, deployment, stamp, kind, origin, body))

    def observe_snapshot(self, db, data):
        old = db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()
        if not old:
            return
        old = json.loads(old[0])
        if old['serial'] != data['serial']:
            return
        device, deployment, stamp = data['serial'], self.deployment(data['archive']), data['observed_utc']
        values = [('logging_state', old.get('logging'), data.get('logging')),
                  ('memory_state', old.get('archive', {}).get('memory_health', 'healthy'), data.get('archive', {}).get('memory_health', 'healthy')),
                  ('relay_state', old.get('alarms', {}).get('current'), data.get('alarms', {}).get('current')),
                  ('sensor_connection', sorted(c['key'] for c in old.get('sensors', [])), sorted(c['key'] for c in data.get('sensors', [])))]
        for kind, before, after in values:
            if before != after and before is not None and after is not None:
                self.add_event(db, device, deployment, stamp, kind,
                               {'before': before, 'after': after, 'previous_observation_utc': old['observed_utc'],
                                **({'error': data['archive'].get('metadata_error') or data['archive'].get('pointer_error')} if kind == 'memory_state' else {}),
                                'timing': 'observed at next successful poll; not a native event timestamp'})
        if data.get('battery_percent', 100) <= 20 < old.get('battery_percent', 100):
            self.add_event(db, device, deployment, stamp, 'low_battery_20_percent', {'battery_percent': data['battery_percent']})

    def observe_connection(self, connected):
        with self.connect() as db:
            old = db.execute("SELECT value FROM state WHERE key='read_connection'").fetchone()
            value = '1' if connected else '0'
            db.execute("INSERT OR REPLACE INTO state VALUES ('read_connection',?)", (value,))
            snapshot = db.execute("SELECT value FROM state WHERE key='snapshot'").fetchone()
            if old and old[0] != value and snapshot:
                s = json.loads(snapshot[0])
                self.add_event(db, s['serial'], self.deployment(s['archive']), datetime.now(timezone.utc).isoformat(timespec='seconds'),
                               'read_resumed' if connected else 'read_failed', {'last_success_utc': s['observed_utc']})

    def import_table(self, request):
        """All-or-nothing file import; only rows from this file are added, never device writes."""
        name = text(request.get('filename', ''), 'Filename')
        suffix = Path(name).suffix.lower()
        if suffix not in ('.csv', '.txt', '.xls'):
            raise ValueError('Import supports UTF-8 CSV, tab-delimited TXT and BIFF8 XLS')
        try:
            content = base64.b64decode(request.get('content_base64', ''), validate=True)
        except (ValueError, TypeError):
            raise ValueError('Invalid file encoding') from None
        if not content or len(content) > 12*1024*1024:
            raise ValueError('File must be between 1 byte and 12 MB; split larger files first')
        digest = hashlib.sha256(content).hexdigest()
        with self.connect() as db:
            previous = db.execute('SELECT metadata FROM imports WHERE id=?', (digest,)).fetchone()
            if previous:
                return {**json.loads(previous[0]), 'already_imported': True}
        if suffix == '.xls':
            import xlrd
            book = xlrd.open_workbook(file_contents=content, on_demand=True)
            def sheets():
                for sheet in book.sheets():
                    if not sheet.nrows:
                        continue
                    headers = [str(v).strip() for v in sheet.row_values(0)]
                    for i in range(1, sheet.nrows):
                        values = sheet.row_values(i)
                        for j in range(sheet.ncols):
                            if sheet.cell_type(i, j) == xlrd.XL_CELL_DATE:
                                values[j] = xlrd.xldate_as_datetime(values[j], book.datemode).replace(tzinfo=TAIPEI).isoformat()
                        yield dict(zip(headers, values))
            rows = sheets()
        else:
            try:
                decoded = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                raise ValueError('Please save CSV/TXT as UTF-8') from None
            rows = csv.DictReader(io.StringIO(decoded), delimiter='\t' if suffix == '.txt' else ',')
        fallback_device = text(request.get('device', 'Imported logger'), 'Device')
        fallback_deployment = text(request.get('deployment', 'Imported deployment'), 'Deployment')
        pulse = request.get('pulse_channels', [])
        if not isinstance(pulse, list) or any(not isinstance(x, str) for x in pulse):
            raise ValueError('Invalid pulse channel declarations')
        inserted = events = read = 0
        merge_mode = request.get('mode', 'independent')
        if merge_mode not in ('independent', 'strict_merge'):
            raise ValueError('Invalid import mode')
        (self.directory/'imports').mkdir(exist_ok=True)
        target = self.directory/'imports'/(digest+suffix)
        target.write_bytes(content)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            metadata = {}
            groups, bounds, baseline = set(), {}, {}
            for i, row in enumerate(rows, 2):
                if i > 250002:
                    raise ValueError('Import is limited to 250,000 table rows per file')
                if not any(v not in ('', None) for v in row.values()):
                    continue
                stamp_value = row.get('timestamp_gmt8', row.get('timestamp_utc', row.get('timestamp')))
                try:
                    stamp = utc(str(stamp_value))
                    device = text(str(row.get('device') or fallback_device), 'Device')
                    deployment = text(str(row.get('deployment') or fallback_deployment), 'Deployment')
                    if row.get('record_type') == 'event':
                        self.add_event(db, device, deployment, stamp, text(str(row.get('event_type', 'imported_event'))),
                                       {'file': name, 'quality': str(row.get('quality', ''))}, 'imported:'+str(row.get('event_origin') or 'unverified'))
                        events += 1
                        continue
                    group = (device, deployment)
                    groups.add(group)
                    if group not in baseline:
                        old = db.execute("SELECT MIN(timestamp_utc),MAX(timestamp_utc) FROM samples WHERE device=? AND deployment=? AND source='import'", group).fetchone()
                        channels = {r[0] for r in db.execute("SELECT DISTINCT channel FROM samples WHERE device=? AND deployment=? AND source='import'", group)}
                        baseline[group] = (old[0], old[1], channels)
                    old_bounds = bounds.setdefault(group, [stamp, stamp, set()])
                    old_bounds[0], old_bounds[1] = min(old_bounds[0], stamp), max(old_bounds[1], stamp)
                    if 'channel' in row and 'value' in row:
                        values = [(str(row['channel']), row.get('value'), str(row.get('units') or ''), str(row.get('label') or row['channel']))]
                    else:
                        excluded = {'timestamp', 'timestamp_utc', 'timestamp_gmt8', 'device', 'deployment'}
                        values = [(k, v, '', k) for k, v in row.items() if k not in excluded]
                    if not values:
                        raise ValueError('No measurement columns')
                    if read+len(values) > 250000:
                        raise ValueError('A file may contain at most 250,000 sensor readings')
                    batch = []
                    for channel, value, units, label in values:
                        text(channel, 'Channel')
                        # Import namespace prevents file metadata overwriting physical sensor calibration.
                        key = 'import:'+hashlib.sha256((device+'\0'+channel).encode()).hexdigest()[:24]
                        old_bounds[2].add(key)
                        units = text(units or str(request.get('units', '')), 'Units', 64)
                        m = {'key': key, 'label': text(label), 'measurement': text(str(row.get('measurement') or channel)),
                             'units': units, 'serial': device, 'kind': 'pulse' if channel in pulse else 'import', 'import_channel': channel}
                        if key in metadata and metadata[key] != m:
                            raise ValueError('A channel has inconsistent labels or units')
                        if key not in metadata:
                            if len(metadata) >= 1000:
                                raise ValueError('A file may contain at most 1,000 channels')
                            existing = db.execute('SELECT metadata FROM channels WHERE key=?', (key,)).fetchone()
                            if existing and json.loads(existing[0]) != m:
                                raise ValueError('Existing imported channel uses different metadata; choose another device name')
                        metadata[key] = m
                        value = None if value in ('', None) else number(value, 'Value')
                        raw = row.get('raw')
                        raw = None if raw in ('', None) else number(raw, 'Raw')
                        quality = text(str(row.get('quality') or ('ok' if value is not None else 'missing')))
                        batch.append((device, deployment, 'import', key, stamp, raw, value, quality))
                    before = db.total_changes
                    # Conflicting duplicates fail rather than silently retaining an arbitrary value.
                    for sample in batch:
                        found = db.execute('SELECT raw,value,quality FROM samples WHERE device=? AND deployment=? AND source=? AND channel=? AND timestamp_utc=?', sample[:5]).fetchone()
                        if found and tuple(found) != sample[5:]:
                            raise ValueError('Conflicting duplicate timestamp; original data was preserved')
                    self.insert_samples(db, batch)
                    inserted += db.total_changes-before
                    read += len(batch)
                except (ValueError, TypeError) as exc:
                    raise ValueError(f'Row {i}: {exc}') from None
            if merge_mode == 'strict_merge':
                if len(groups) != 1:
                    raise ValueError('Strict merge requires exactly one device and one deployment per file')
                group = next(iter(groups))
                first, last, channel_keys = bounds[group]
                old_first, old_last, old_channels = baseline[group]
                if old_channels and channel_keys != old_channels:
                    raise ValueError('Strict merge requires identical series types, names and channel sets')
                if old_first is not None and first <= old_last and last >= old_first:
                    raise ValueError('Strict merge refuses overlapping time ranges, including identical duplicate samples')
            for m in metadata.values():
                self.channel(db, m)
            result = {'id': digest, 'filename': name, 'readings': read, 'inserted': inserted, 'events': events,
                      'created_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'bytes': len(content), 'mode': merge_mode}
            db.execute('INSERT INTO imports VALUES (?,?)', (digest, json.dumps(result, ensure_ascii=False)))
        return result

    def workbench_export(self, request, format):
        if format not in ('csv', 'txt', 'xls'):
            raise ValueError('Export format must be CSV, TXT or XLS')
        if not isinstance(request.get('series'), list) or not 1 <= len(request['series']) <= 24:
            raise ValueError('Select 1–24 series')
        output = tempfile.TemporaryFile()
        try:
            if format == 'xls':
                import xlwt
                workbook = xlwt.Workbook(encoding='utf-8')
                sheet = None
                index = 65536
                header_style = xlwt.easyxf('font: bold on; pattern: pattern solid, fore_colour light_green;')
                def write(values):
                    nonlocal sheet, index
                    if index >= 65536:
                        sheet = workbook.add_sheet('Data '+str(len(workbook._Workbook__worksheets)+1))
                        sheet.set_panes_frozen(True)
                        sheet.set_horz_split_pos(1)
                        for j, title in enumerate(EXPORT_COLUMNS):
                            sheet.write(0, j, title, header_style)
                            sheet.col(j).width = 6000 if j in (1, 5, 6) else 4000
                        index = 1
                    for j, value in enumerate(values):
                        sheet.write(index, j, '' if value is None else value)
                    index += 1
                    if index % 1000 == 0:
                        sheet.flush_row_data()
            else:
                buffer = io.StringIO()
                writer = csv.writer(buffer, delimiter='\t' if format == 'txt' else ',')
                output.write(b'\xef\xbb\xbf')
                def write(values):
                    buffer.seek(0)
                    buffer.truncate()
                    # Spreadsheet formulas remain text, including user channel names and event descriptions.
                    writer.writerow(["'"+v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values])
                    output.write(buffer.getvalue().encode('utf-8'))
                write(EXPORT_COLUMNS)
            count = 0
            with self.connect() as db:
                db.execute('BEGIN')
                configs = [self.validate_series(c, db) for c in request['series']]
                for config in configs:
                    exported_channel = config['channel']
                    if config['assistant']['kind'] != 'none' or config.get('analysis'):
                        definition = json.dumps([config['channel'], config['assistant'], config.get('analysis'), config['name'], config['units']],sort_keys=True)
                        exported_channel = 'derived:'+hashlib.sha256(definition.encode()).hexdigest()[:32]
                    for row in self.series_rows(db, config):
                        count += 1
                        if format == 'xls' and count > 1000000:
                            raise ValueError('XLS export is limited to 1,000,000 rows; use CSV/TXT or a shorter range')
                        write(('sample', datetime.fromisoformat(row['timestamp_utc']).astimezone(TAIPEI).isoformat(), config['device'],
                               config['deployment'], config['source'], exported_channel, config['name'],
                               config['metadata'].get('measurement', ''), config['units'], row['raw'], row['value'], row['quality'], '', '',
                               config['channel'], config['assistant']['kind'], json.dumps(config['analysis'],sort_keys=True) if config.get('analysis') else ''))
            if request.get('events'):
                for event in self.workbench_events(request):
                    write(('event', datetime.fromisoformat(event['timestamp_utc']).astimezone(TAIPEI).isoformat(), event['device'], event['deployment'],
                           '', '', '', '', '', '', '', json.dumps(event['details'], ensure_ascii=False), event['event_type'], event['origin'], '', '', ''))
            if format == 'xls':
                if sheet is None:
                    sheet = workbook.add_sheet('Data 1')
                    for j, title in enumerate(EXPORT_COLUMNS):
                        sheet.write(0, j, title, header_style)
                workbook.save(output)
            output.seek(0)
            return output
        except BaseException:
            output.close()
            raise
