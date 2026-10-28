"""Non-destructive, explicitly named data conversions. All timestamps are UTC."""
import math
from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))
GDD_METHODS = ('average', 'triangle', 'triangle_horizontal', 'sine', 'sine_horizontal',
               'actual', 'actual_horizontal', 'actual_vertical')


def number(value, name, default=None):
    if value is None and default is not None:
        value = default
    if isinstance(value, bool):
        raise ValueError(name + ' must be a finite number')
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(name + ' must be a finite number') from None
    if not math.isfinite(result):
        raise ValueError(name + ' must be a finite number')
    return result


def utc(value):
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError('Invalid timestamp')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TAIPEI)
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds' if dt.microsecond else 'seconds')


def celsius(value, units):
    if units in ('*C', '°C', 'C', 'degC'):
        return value
    if units in ('*F', '°F', 'F', 'degF'):
        return (value-32)*5/9
    raise ValueError('Temperature assistant requires °C or °F')


def grains_per_pound(temp_c, rh, pressure):
    """ASHRAE 2017 SI equations 5, 6 and 20; no HOBOware altitude/dewpoint correction."""
    if not (-100 <= temp_c <= 200 and 0 <= rh <= 100 and pressure > 0):
        raise ValueError('Temperature, humidity or pressure is out of range')
    t = temp_c+273.15
    if temp_c <= .01:
        log_p = (-5674.5359/t+6.3925247-.009677843*t+6.2215701e-7*t*t+
                 2.0747825e-9*t**3-9.484024e-13*t**4+4.1635019*math.log(t))
    else:
        log_p = (-5800.2206/t+1.3914993-.048640239*t+4.1764768e-5*t*t-
                 1.4452093e-8*t**3+6.5459673*math.log(t))
    pv = math.exp(log_p)*rh/100
    if pressure <= pv:
        raise ValueError('Station pressure must exceed vapor pressure')
    return 7000*.621945*pv/(pressure-pv)


def validate_assistant(config, metadata):
    if not isinstance(config, dict):
        raise ValueError('Invalid assistant')
    kind = config.get('kind', 'none')
    if kind not in ('none', 'linear', 'pulse', 'gpp', 'gdd'):
        raise ValueError('Unknown data assistant')
    result = {'kind': kind}
    if kind == 'linear':
        result.update(slope=number(config.get('slope'), 'Slope', 1), intercept=number(config.get('intercept'), 'Intercept', 0))
    elif kind == 'pulse':
        if metadata.get('kind') != 'pulse' and metadata.get('measurement', '').lower() not in ('pulse', 'count', 'rainfall'):
            raise ValueError('Pulse scaling requires a declared pulse/count channel')
        result.update(raw_units=number(config.get('raw_units'), 'Raw units'), scaled_units=number(config.get('scaled_units'), 'Scaled units'))
        if result['raw_units'] == 0:
            raise ValueError('Raw units cannot be zero')
    elif kind in ('gpp', 'gdd'):
        celsius(0, metadata.get('units'))
        if kind == 'gpp':
            humidity = config.get('humidity_channel')
            if not isinstance(humidity, str) or not humidity or len(humidity) > 256:
                raise ValueError('Select a relative humidity channel')
            altitude = number(config.get('altitude_m'), 'Altitude', 0)
            if not -500 <= altitude <= 10000:
                raise ValueError('Altitude must be between -500 and 10000 m')
            pressure = number(config.get('pressure_pa'), 'Pressure', 101325*(1-2.25577e-5*altitude)**5.2559)
            if not 10000 <= pressure <= 120000:
                raise ValueError('Station pressure must be between 10000 and 120000 Pa')
            result.update(humidity_channel=humidity, pressure_pa=pressure)
        else:
            method = config.get('method', 'actual')
            if method not in GDD_METHODS:
                raise ValueError('Invalid GDD method')
            low = number(config.get('lower'), 'Lower threshold', 10)
            high = number(config.get('upper'), 'Upper threshold', 30)
            if not -100 <= low < high <= 200:
                raise ValueError('GDD requires -100 ≤ lower < upper ≤ 200 °C')
            gap = number(config.get('max_gap_seconds'), 'Maximum gap', 3600)
            if not 1 <= gap <= 64800:
                raise ValueError('Maximum gap must be between 1 and 64800 seconds')
            result.update(method=method, lower=low, upper=high, max_gap_seconds=gap,
                          cumulative=config.get('cumulative') is True, biofix=utc(config.get('biofix', '2000-01-01')))
    if kind in ('linear', 'pulse'):
        units = config.get('units', metadata.get('units', ''))
        if not isinstance(units, str) or len(units) > 64:
            raise ValueError('Invalid output unit')
        result['units'] = units
    return result


def gdd(rows, config, units):
    """Complete Taipei days only. Actual methods use right endpoint rectangles.

    Gaps/missing samples invalidate a day. A cumulative series stays missing after
    an incomplete day rather than hiding lost heat units. Average reproduces the
    installed HOBOware endpoint clipping rule, including its high-end asymmetry.
    """
    low, high, method = config['lower'], config['upper'], config['method']
    def f(t):
        if method.endswith('_vertical'):
            return t-low if low < t <= high else 0
        return max(0, (min(t, high) if method.endswith('_horizontal') else t)-low)
    def finish(day, duration, area, temperatures, complete):
        if not complete or duration < 86400-1e-6 or not temperatures:
            return None
        lo, hi = min(temperatures), max(temperatures)
        if method == 'average':
            return 0 if hi < low else (max(lo, low)+min(hi, high))/2-low
        if method.startswith(('triangle', 'sine')):
            temps = [lo+(hi-lo)*(math.sin(math.pi*k/96) if method.startswith('sine') else 1-abs(k-48)/48) for k in range(96)]
            return sum(f(t) for t in temps)/96
        return area/86400
    previous = None
    day = None
    duration = area = 0
    temps = []
    complete = True
    total = 0
    cumulative_ok = True
    biofix = datetime.fromisoformat(config['biofix']).astimezone(TAIPEI).date()
    def output(d, value):
        nonlocal total, cumulative_ok
        if value is None:
            cumulative_ok = False
        else:
            total += value
        val = (total if cumulative_ok else None) if config['cumulative'] else value
        return {'timestamp_utc': datetime.combine(d, datetime.min.time(), TAIPEI).replace(hour=12).astimezone(timezone.utc).isoformat(),
                'value': val, 'raw': None, 'quality': 'gdd_complete' if val is not None else 'gdd_incomplete_day'}
    for row in rows:
        stamp = datetime.fromisoformat(row['timestamp_utc'])
        value = celsius(row['value'], units) if row['value'] is not None else None
        if previous is not None:
            left, previous_value = previous
            gap = (stamp-left).total_seconds()
            if gap > 3660*86400:
                raise ValueError('GDD cannot span a gap longer than 10 years')
            while left < stamp:
                d = left.astimezone(TAIPEI).date()
                midnight = datetime.combine(d+timedelta(days=1), datetime.min.time(), TAIPEI).astimezone(timezone.utc)
                right = min(stamp, midnight)
                if d >= biofix:
                    if day is not None and day != d:
                        yield output(day, finish(day, duration, area, temps, complete))
                        duration = area = 0
                        temps = []
                        complete = True
                    day = d
                    seconds = (right-left).total_seconds()
                    valid = gap <= config['max_gap_seconds'] and value is not None and previous_value is not None
                    duration += seconds
                    complete = complete and valid
                    if valid:
                        temps.extend((previous_value, value))
                        area += f(value)*seconds
                left = right
        previous = stamp, value
    if day is not None:
        yield output(day, finish(day, duration, area, temps, complete))
