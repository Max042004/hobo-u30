"""Readout-time derived series. Fixed intervals are aligned to GMT+8."""
import math
from datetime import datetime, timezone
from data_assistants import number

NUMERIC_FUNCTIONS = ('min', 'max', 'mean', 'sum', 'count', 'stddev')
STATE_FUNCTIONS = ('on_seconds', 'off_seconds', 'on_percent', 'switches', 'on_count', 'off_count')


def validate_analysis(config):
    if config is None or config == {}:
        return None
    if not isinstance(config, dict):
        raise ValueError('Invalid interval analysis')
    if config.get('kind') == 'none':
        return None
    kind = config.get('kind')
    function = config.get('function')
    if kind not in ('numeric', 'state') or function not in (NUMERIC_FUNCTIONS if kind == 'numeric' else STATE_FUNCTIONS):
        raise ValueError('Invalid interval analysis function')
    units = {'second':1, 'minute':60, 'hour':3600, 'day':86400}
    interval = number(config.get('interval'), 'Analysis interval')
    if config.get('unit') not in units or interval != int(interval) or interval < 1:
        raise ValueError('Analysis interval must be a positive integer in seconds, minutes, hours or days')
    seconds = int(interval)*units[config['unit']]
    if not 1 <= seconds <= 366*86400:
        raise ValueError('Analysis interval must not exceed 366 days')
    result = {'kind':kind, 'function':function, 'interval':int(interval), 'unit':config['unit'], 'seconds':seconds}
    if kind == 'state':
        on, off = number(config.get('on_value'), 'On value', 1), number(config.get('off_value'), 'Off value', 0)
        if on == off:
            raise ValueError('On and off values must be different')
        gap = number(config.get('max_gap_seconds'), 'Maximum state hold gap', 60)
        if not 1 <= gap <= 64800:
            raise ValueError('Maximum state hold gap must be 1–64800 seconds')
        result.update(on_value=on, off_value=off, max_gap_seconds=gap)
    return result


def bucket_at(stamp, seconds):
    return math.floor((stamp+28800)/seconds)*seconds-28800


def numeric_intervals(rows, config):
    seconds = config['seconds']
    current = None
    state = None
    def finish(start, s):
        n, mean, m2, low, high, total, missing = s
        fn = config['function']
        value = (dict(min=low, max=high, mean=mean, sum=total, count=n,
                      stddev=math.sqrt(max(0, m2/n)) if n else None)[fn]) if n or fn == 'count' else None
        if value is not None and not math.isfinite(value):
            raise ValueError('Interval analysis overflow')
        return {'timestamp_utc':datetime.fromtimestamp(start+seconds, timezone.utc).isoformat(), 'raw':None, 'value':value,
                'quality':f'analysis_{fn};input_samples={n};missing={missing};window_start='+datetime.fromtimestamp(start,timezone.utc).isoformat()}
    for row in rows:
        stamp = datetime.fromisoformat(row['timestamp_utc']).timestamp()
        bucket = bucket_at(stamp, seconds)
        if current != bucket:
            if current is not None:
                yield finish(current, state)
            current = bucket
            state = [0, 0, 0, None, None, 0, 0]
        value = row['value']
        if value is None:
            state[6] += 1
        else:
            state[0] += 1
            delta = value-state[1]
            state[1] += delta/state[0]
            state[2] += delta*(value-state[1])
            state[3] = value if state[3] is None else min(state[3], value)
            state[4] = value if state[4] is None else max(state[4], value)
            state[5] += value
    if current is not None:
        yield finish(current, state)


def state_intervals(rows, config):
    """Hold a known state only until the next sample within max_gap.

    No extrapolation beyond the final sample. Missing samples and long gaps are
    unknown time, not off time. A switch is counted at the new sample timestamp.
    """
    seconds = config['seconds']
    current = None
    counters = None
    previous = None
    def fresh():
        return dict(on_seconds=0., off_seconds=0., switches=0, on_count=0, off_count=0)
    def finish(start, counter):
        covered = counter['on_seconds']+counter['off_seconds']
        fn = config['function']
        known = covered or counter['switches']
        value = (100*counter['on_seconds']/covered if covered else None) if fn == 'on_percent' else counter[fn] if known else None
        return {'timestamp_utc':datetime.fromtimestamp(start+seconds,timezone.utc).isoformat(), 'raw':None, 'value':value,
                'quality':f'analysis_{fn};covered_seconds={covered:g};window_seconds={seconds};unknown_seconds={max(0,seconds-covered):g}'}
    for row in rows:
        stamp = datetime.fromisoformat(row['timestamp_utc']).timestamp()
        value = row['value']
        state = True if value == config['on_value'] else False if value == config['off_value'] else None
        if previous:
            left, last_state = previous
            gap = stamp-left
            if gap > 366*86400:
                raise ValueError('State analysis cannot span a gap longer than 366 days')
            if gap/seconds > 100000:
                raise ValueError('State analysis exceeds 100,000 interval outputs; increase the interval or shorten the range')
            valid = gap <= config['max_gap_seconds'] and last_state is not None and state is not None
            while left < stamp:
                bucket = bucket_at(left, seconds)
                right = min(stamp, bucket+seconds)
                if current != bucket:
                    if current is not None:
                        yield finish(current, counters)
                    current, counters = bucket, fresh()
                if valid:
                    counters['on_seconds' if last_state else 'off_seconds'] += right-left
                left = right
            if valid and last_state != state:
                bucket = bucket_at(stamp, seconds)
                if current != bucket:
                    if current is not None:
                        yield finish(current, counters)
                    current, counters = bucket, fresh()
                counters['switches'] += 1
                counters['on_count' if state else 'off_count'] += 1
        previous = stamp, state
    if current is not None:
        yield finish(current, counters)


def interval_rows(rows, config):
    yield from (numeric_intervals(rows, config) if config['kind'] == 'numeric' else state_intervals(rows, config))
