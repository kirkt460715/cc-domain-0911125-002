"""Study registration: expected reporting grids, operational windows and outages.

A study freezes the point-in-time reference data (aliases, calibrations, quality
rules) at ``frozen_at`` so historical evidence can be reconstructed after later
calibration corrections.  Nothing written by a study ever modifies the official
observations table.
"""
from hangar_ingest.types import (
    canonical, decimal_string, digest, fail, from_micros, identity, instant, integer, micros, number,
)
from hangar_ingest.quality import devices, rules
from hangar_ingest.runs import journal

MAX_CELLS = 200000
DISPOSITIONS = ('valid', 'quality_excluded', 'planned_outage', 'unexpected_missing')


def _bounded_text(value, field, maximum=512):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        fail('invalid_text', 'Expected bounded nonempty text', field=field)
    return value.strip()


def _aligned(value, interval_us, field):
    if value % interval_us:
        fail('grid_alignment', 'Time must align to the reporting grid', field=field)
    return value


def _validate_outages(outages, interval_us, operational, seen_ids):
    clean = []
    for index, raw in enumerate(outages):
        if not isinstance(raw, dict):
            fail('invalid_outage', 'Outage must be an object', index=index)
        outage_id = identity(raw.get('outage_id', f'outage{index + 1}'), 'outage_id')
        if outage_id in seen_ids:
            fail('outage_id_duplicate', 'Outage identifiers must be unique within a study', outage_id=outage_id)
        seen_ids.add(outage_id)
        start = _aligned(micros(instant(raw['start'])), interval_us, 'outage.start')
        end = _aligned(micros(instant(raw['end'])), interval_us, 'outage.end')
        if start >= end:
            fail('outage_interval', 'Outage window must be nonempty', outage_id=outage_id)
        reason = _bounded_text(raw.get('reason', 'planned_outage'), 'reason')
        lower, upper = operational
        if start < lower or end > upper:
            fail('outage_scope', 'Outage must lie inside the device operational window', outage_id=outage_id)
        clean.append((outage_id, start, end, reason))
    for left, right in zip(clean, clean[1:]):
        if left[1] < right[2] and right[1] < left[2]:
            fail('outage_overlap', 'Outage windows for one device may not overlap', outage_id=left[0])
    return [(outage_id, from_micros(start), from_micros(end), reason)
            for outage_id, start, end, reason in sorted(clean, key=lambda row: (row[1], row[0]))]


def _snapshot_reference(db, study_id, device_rows):
    for device_id, _aircraft, operational_start, operational_end, _outages, _rule_revision in device_rows:
        aliases = db.all(
            'SELECT source,external_id,device_id,valid_from,valid_until FROM aliases '
            'WHERE device_id=? AND valid_from<? AND valid_until>? ORDER BY source,external_id,valid_from',
            (device_id, operational_end, operational_start),
        )
        for alias in aliases:
            db.execute(
                'INSERT INTO study_alias_snapshots(study_id,source,external_id,device_id,valid_from,valid_until) '
                'VALUES(?,?,?,?,?,?)',
                (study_id, alias['source'], alias['external_id'], alias['device_id'], alias['valid_from'], alias['valid_until']),
            )
        calibrations = db.all(
            'SELECT * FROM calibrations WHERE device_id=? AND valid_from<? AND valid_until>? ORDER BY valid_from',
            (device_id, operational_end, operational_start),
        )
        for calibration in calibrations:
            db.execute(
                'INSERT INTO study_calibration_snapshots(study_id,calibration_id,device_id,valid_from,valid_until,'
                'scale,offset,clock_offset_us,revision) VALUES(?,?,?,?,?,?,?,?,?)',
                (study_id, calibration['calibration_id'], device_id, calibration['valid_from'], calibration['valid_until'],
                 calibration['scale'], calibration['offset'], calibration['clock_offset_us'], calibration['revision']),
            )
        rule = rules.get(db, device_id)
        db.execute(
            'INSERT INTO study_rule_snapshots(study_id,device_id,revision,minimum,maximum,max_delta,'
            'max_gap_seconds,duplicate_policy,required_calibration,payload) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (study_id, device_id, rule['revision'], rule['minimum'], rule['maximum'], rule['max_delta'],
             rule['max_gap_seconds'], rule['duplicate_policy'], int(rule['required_calibration']), canonical(rule)),
        )


def create_study(db, study_id, *, device_windows, start, end, interval_seconds,
                 period_seconds=None, metric=None, frozen_at=None, cutoff_at=None,
                 strata_fields=None, physical_minimum, physical_maximum, alarm_threshold,
                 bootstrap_samples=500, seed=0):
    identity(study_id, 'study_id')
    interval = integer(interval_seconds, 'interval_seconds', 1, 86400)
    period = integer(period_seconds or interval, 'period_seconds', interval, 86400 * 7)
    if period % interval:
        fail('period_alignment', 'Period must be a multiple of the reporting interval')
    interval_us = interval * 1000000
    grid_start = _aligned(micros(instant(start)), interval_us, 'start')
    grid_end = _aligned(micros(instant(end)), interval_us, 'end')
    if grid_start >= grid_end:
        fail('study_interval', 'Study window must be nonempty')
    cell_count = (grid_end - grid_start) // interval_us
    integer(bootstrap_samples, 'bootstrap_samples', 10, 10000)
    integer(seed, 'seed', 0, 2**31 - 1)
    low = number(physical_minimum, 'physical_minimum')
    high = number(physical_maximum, 'physical_maximum')
    alarm = number(alarm_threshold, 'alarm_threshold')
    if low >= high:
        fail('physical_bounds', 'Physical minimum must be below maximum')
    if not low <= alarm <= high:
        fail('alarm_bounds', 'Alarm threshold must lie inside the physical bounds')
    if not isinstance(device_windows, list) or not device_windows or len(device_windows) > 1000:
        fail('device_windows', 'Study needs a bounded nonempty device window list')
    fields = strata_fields or []
    if not isinstance(fields, list) or len(fields) > 20 or any(not isinstance(item, str) or not item for item in fields):
        fail('strata_fields', 'Stratification fields must be a bounded list of names')
    if len(fields) != len(set(fields)):
        fail('strata_fields', 'Stratification fields must be unique')
    frozen = instant(frozen_at) if frozen_at else db.clock()
    cutoff = instant(cutoff_at) if cutoff_at else frozen
    if cutoff > frozen:
        fail('cutoff_order', 'Analysis cutoff cannot be later than the study freeze time')
    parsed_devices = []
    total_cells = 0
    shared_metric = metric
    for entry in device_windows:
        if not isinstance(entry, dict):
            fail('invalid_device_window', 'Device window must be an object')
        device_id = identity(entry['device_id'], 'device_id')
        config = devices.device(db, device_id)
        if shared_metric is None:
            shared_metric = config['metric']
        elif shared_metric != config['metric']:
            fail('study_metric', 'All study devices must measure the same metric', device_id=device_id)
        operational_start = _aligned(micros(instant(entry['operational_start'])), interval_us, 'operational_start')
        operational_end = _aligned(micros(instant(entry['operational_end'])), interval_us, 'operational_end')
        if not grid_start <= operational_start < operational_end <= grid_end:
            fail('operational_scope', 'Operational window must lie inside the study grid', device_id=device_id)
        device_cells = (operational_end - operational_start) // interval_us
        total_cells += device_cells
        if total_cells > MAX_CELLS:
            fail('study_size', 'Study would create too many grid cells', maximum=MAX_CELLS)
        outages = _validate_outages(entry.get('outages', []), interval_us,
                                    (operational_start, operational_end), set())
        parsed_devices.append((device_id, config['aircraft'],
                               from_micros(operational_start), from_micros(operational_end),
                               outages, rules.get(db, device_id)['revision']))
    if db.one('SELECT 1 FROM studies WHERE study_id=?', (study_id,)):
        fail('study_conflict', 'Study identifier already exists', study_id=study_id)
    definition = {
        'study_id': study_id,
        'device_windows': device_windows,
        'start': from_micros(grid_start),
        'end': from_micros(grid_end),
        'interval_seconds': interval,
        'period_seconds': period,
        'metric': shared_metric,
        'frozen_at': frozen,
        'cutoff_at': cutoff,
        'strata_fields': fields,
        'physical_minimum': decimal_string(low),
        'physical_maximum': decimal_string(high),
        'alarm_threshold': decimal_string(alarm),
        'bootstrap_samples': bootstrap_samples,
        'seed': seed,
    }
    definition_hash = digest(definition)
    with db.transaction():
        db.execute(
            'INSERT INTO studies(study_id,state,revision,metric,interval_seconds,period_seconds,grid_start,grid_end,'
            'frozen_at,cutoff_at,strata_fields,physical_minimum,physical_maximum,alarm_threshold,bootstrap_samples,'
            'seed,definition_hash,created_at,updated_at) VALUES(?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (study_id, 'registered', shared_metric, interval, period, from_micros(grid_start), from_micros(grid_end),
             frozen, cutoff, canonical(fields), decimal_string(low), decimal_string(high), decimal_string(alarm),
             bootstrap_samples, seed, definition_hash, db.clock(), db.clock()),
        )
        for device_id, aircraft, op_start, op_end, outages, rule_revision in parsed_devices:
            db.execute(
                'INSERT INTO study_devices(study_id,device_id,aircraft,operational_start,operational_end,rule_revision) '
                'VALUES(?,?,?,?,?,?)', (study_id, device_id, aircraft, op_start, op_end, rule_revision),
            )
            for outage_id, outage_start, outage_end, reason in outages:
                db.execute(
                    'INSERT INTO study_outages(study_id,outage_id,device_id,start,end,reason) VALUES(?,?,?,?,?,?)',
                    (study_id, outage_id, device_id, outage_start, outage_end, reason),
                )
        _snapshot_reference(db, study_id, parsed_devices)
        journal.record(db, 'study.registered', {
            'study_id': study_id, 'devices': len(parsed_devices), 'cells': total_cells,
            'definition_hash': definition_hash, 'frozen_at': frozen,
        })
    return get_study(db, study_id)


def get_study(db, study_id):
    identity(study_id, 'study_id')
    row = db.one('SELECT * FROM studies WHERE study_id=?', (study_id,))
    if row is None:
        fail('unknown_study', 'Study does not exist', study_id=study_id)
    row['strata_fields'] = json_loads(row['strata_fields'])
    return row


def json_loads(value):
    import json
    return json.loads(value)


def list_studies(db, state=None):
    if state is None:
        rows = db.all('SELECT * FROM studies ORDER BY study_id')
    else:
        if state not in {'registered', 'gridded', 'modeled', 'scenario'}:
            fail('study_state', 'Unknown study state')
        rows = db.all('SELECT * FROM studies WHERE state=? ORDER BY study_id', (state,))
    for row in rows:
        row['strata_fields'] = json_loads(row['strata_fields'])
    return rows


def require_state(db, study_id, allowed):
    study = get_study(db, study_id)
    if study['state'] not in allowed:
        fail('study_state', 'Study is not in the required state', study_id=study_id,
             current=study['state'], required=sorted(allowed))
    return study


def advance_state(db, study_id, state):
    db.execute('UPDATE studies SET state=?,revision=revision+1,updated_at=? WHERE study_id=?',
               (state, db.clock(), study_id))


def device_windows(db, study_id):
    return db.all('SELECT * FROM study_devices WHERE study_id=? ORDER BY device_id', (study_id,))


def outages(db, study_id, device_id):
    return db.all(
        'SELECT * FROM study_outages WHERE study_id=? AND device_id=? ORDER BY start,outage_id',
        (study_id, device_id),
    )


def definition_replay(db, study_id):
    """Return the frozen evidence bundle used when the study was registered."""
    study = get_study(db, study_id)
    return {
        'study': study,
        'devices': device_windows(db, study_id),
        'outages': db.all('SELECT * FROM study_outages WHERE study_id=? ORDER BY device_id,start,outage_id', (study_id,)),
        'aliases': db.all('SELECT * FROM study_alias_snapshots WHERE study_id=? ORDER BY device_id,source,external_id', (study_id,)),
        'calibrations': db.all('SELECT * FROM study_calibration_snapshots WHERE study_id=? ORDER BY device_id,valid_from', (study_id,)),
        'rules': [dict(row, payload=json_loads(row['payload']))
                  for row in db.all('SELECT * FROM study_rule_snapshots WHERE study_id=? ORDER BY device_id', (study_id,))],
    }
