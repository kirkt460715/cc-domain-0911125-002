"""Versioned operating-condition inputs and grid stratification.

Condition tables are externally authored inputs, versioned per study and joined
to cells by validity interval.  Only fields available at ``cutoff_at`` are used
and only the declared stratification fields are ever attached to a cell.  The
condition rows never contain, and must never be populated from, temperatures of
missing cells or post-hoc maintenance conclusions.
"""
import json

from hangar_ingest.types import canonical, digest, fail, identity, instant, integer, micros, number
from hangar_ingest.runs import journal
from hangar_ingest.studies import registry

MAX_ROWS = 100000

# Condition tables describe operating context available at the cutoff.  Fields
# carrying the target measurement of a missing cell or post-hoc maintenance
# conclusions can never be predictors: they would leak the very thing being studied.
FORBIDDEN_FIELD_TOKENS = ('temperature', 'temp', 'repair', 'maintenance', 'fault', 'failure', 'outcome')


def _assert_field_allowed(field):
    lowered = field.lower()
    if any(token in lowered for token in FORBIDDEN_FIELD_TOKENS):
        fail('forbidden_condition_field',
             'Condition fields may not carry target temperatures or post-hoc maintenance conclusions',
             field=field)


def import_conditions(db, study_id, rows, *, source, version):
    study = registry.require_state(db, study_id, {'gridded', 'modeled', 'scenario'})
    identity(source, 'source')
    identity(version, 'version')
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        fail('conditions_size', 'Condition import needs a bounded nonempty row list')
    fields = study['strata_fields']
    if not fields:
        fail('conditions_fields', 'Study declares no stratification fields')
    for field in fields:
        _assert_field_allowed(field)
    known_devices = {row['device_id'] for row in registry.device_windows(db, study_id)}
    next_revision = (db.scalar('SELECT COALESCE(MAX(revision),0)+1 FROM study_conditions WHERE study_id=?', (study_id,)))
    cleaned = []
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict):
            fail('invalid_condition', 'Condition row must be an object', index=index)
        device_id = identity(raw['device_id'], 'device_id')
        if device_id not in known_devices:
            fail('condition_device', 'Condition row references a device outside the study', index=index)
        start = instant(raw['valid_from'])
        end = instant(raw['valid_until'])
        if start >= end:
            fail('condition_interval', 'Condition validity interval must be nonempty', index=index)
        unknown = set(raw) - {'device_id', 'valid_from', 'valid_until', *fields}
        if unknown:
            fail('condition_fields', 'Condition row contains undeclared fields', unknown=sorted(unknown), index=index)
        for field in fields:
            _assert_field_allowed(field)
        values = {}
        for field in fields:
            if field not in raw:
                fail('condition_fields', 'Condition row misses a declared stratification field', field=field, index=index)
            values[field] = _scalar(raw[field], field)
        cleaned.append((device_id, start, end, canonical(values)))
    payload_hash = digest({'rows': rows, 'source': source, 'version': version})
    existing = db.one(
        'SELECT 1 FROM study_conditions WHERE study_id=? AND (source=? OR version=?)',
        (study_id, source, version),
    )
    if existing:
        fail('condition_version', 'Condition source/version already imported for this study')
    with db.transaction():
        db.execute(
            'INSERT INTO study_conditions(study_id,revision,source,version,imported_at,payload_hash) '
            'VALUES(?,?,?,?,?,?)', (study_id, next_revision, source, version, db.clock(), payload_hash),
        )
        for row_number, (device_id, start, end, values) in enumerate(cleaned, 1):
            db.execute(
                'INSERT INTO study_condition_rows(study_id,revision,row_number,device_id,valid_from,valid_until,fields) '
                'VALUES(?,?,?,?,?,?,?)', (study_id, next_revision, row_number, device_id, start, end, values),
            )
        journal.record(db, 'study.conditions_imported', {
            'study_id': study_id, 'revision': next_revision, 'rows': len(cleaned),
            'source': source, 'version': version, 'payload_hash': payload_hash,
        })
    return annotate(db, study_id, next_revision)


def _scalar(value, field):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        fail('condition_value', 'Condition fields must be bounded scalar values', field=field)
    if isinstance(value, float):
        number(value, field)
    if isinstance(value, str) and not value.strip():
        fail('condition_value', 'Condition text must be nonempty', field=field)
    return value


def active_revision(db, study_id):
    row = db.one('SELECT MAX(revision) AS revision FROM study_conditions WHERE study_id=?', (study_id,))
    if row is None or row['revision'] is None:
        return None
    return row['revision']


def _lookup(db, study_id, revision):
    rows = db.all(
        'SELECT device_id,valid_from,valid_until,fields FROM study_condition_rows '
        'WHERE study_id=? AND revision=? ORDER BY device_id,valid_from,row_number', (study_id, revision),
    )
    table = {}
    for row in rows:
        table.setdefault(row['device_id'], []).append(
            (micros(row['valid_from']), micros(row['valid_until']), json.loads(row['fields'])),
        )
    return table


def annotate(db, study_id, revision=None):
    study = registry.require_state(db, study_id, {'gridded', 'modeled', 'scenario'})
    if revision is None:
        revision = active_revision(db, study_id)
        if revision is None:
            fail('conditions_missing', 'No condition table has been imported')
    integer(revision, 'revision', 1)
    if db.one('SELECT 1 FROM study_conditions WHERE study_id=? AND revision=?', (study_id, revision)) is None:
        fail('conditions_revision', 'Condition revision does not exist')
    table = _lookup(db, study_id, revision)
    missing = []
    annotated = 0
    with db.transaction():
        for cell in db.all('SELECT * FROM study_cells WHERE study_id=? ORDER BY cell_index', (study_id,)):
            start_us = micros(cell['cell_start'])
            match = None
            for lower, upper, values in table.get(cell['device_id'], ()):
                if lower <= start_us < upper:
                    if match is not None:
                        fail('condition_overlap', 'Condition rows overlap for a grid cell',
                             device_id=cell['device_id'], cell_start=cell['cell_start'])
                    match = values
            if match is None:
                missing.append(cell['cell_index'])
                continue
            db.execute('UPDATE study_cells SET strata=? WHERE study_id=? AND cell_index=? AND device_id=?',
                       (canonical(match), study_id, cell['cell_index'], cell['device_id']))
            annotated += 1
        journal.record(db, 'study.annotated', {'study_id': study_id, 'revision': revision, 'cells': annotated})
    return {
        'study_id': study_id,
        'condition_revision': revision,
        'annotated_cells': annotated,
        'unannotated_cells': len(missing),
        'unannotated_cell_indexes': missing[:1000],
        'complete': not missing,
    }


def strata_of(cell):
    return json.loads(cell['strata']) if isinstance(cell['strata'], str) else cell['strata']


def strata_key(cell, fields):
    values = strata_of(cell)
    return tuple(values.get(field) for field in fields)


def strata_report(db, study_id):
    """Coverage and gap lengths stratified by device, period and condition layer."""
    study = registry.require_state(db, study_id, {'gridded', 'modeled', 'scenario'})
    fields = study['strata_fields']
    interval = study['interval_seconds']
    period_us = study['period_seconds'] * 1000000
    grid_start = micros(study['grid_start'])
    groups = {}
    for cell in db.all('SELECT * FROM study_cells WHERE study_id=? ORDER BY cell_index', (study_id,)):
        period_index = (micros(cell['cell_start']) - grid_start) // period_us
        key = (cell['device_id'], period_index, *strata_key(cell, fields))
        group = groups.setdefault(key, {
            'device_id': cell['device_id'],
            'period_index': period_index,
            'strata': {field: value for field, value in zip(fields, key[2:])},
            'counts': {label: 0 for label in registry.DISPOSITIONS},
            'gap_runs': [],
        })
        group['counts'][cell['disposition']] += 1
        groups_store_run(group, cell)
    result = []
    for group in groups.values():
        total = sum(group['counts'].values())
        accountable = total - group['counts']['planned_outage']
        for run in group['gap_runs']:
            run['duration_seconds'] = run['length_cells'] * interval
        result.append({
            **{k: v for k, v in group.items() if k != 'gap_runs'},
            'cells': total,
            'accountable_cells': accountable,
            'valid_coverage_ratio': _ratio(group['counts']['valid'], accountable),
            'unexpected_missing_ratio': _ratio(group['counts']['unexpected_missing'], accountable),
            'gap_runs': group['gap_runs'],
        })
    result.sort(key=lambda row: (row['device_id'], row['period_index'], canonical(row['strata'])))
    return {'study_id': study_id, 'strata_fields': fields, 'layers': result}


def groups_store_run(group, cell):
    runs = group['gap_runs']
    if cell['disposition'] in {'unexpected_missing', 'planned_outage'} and runs \
            and runs[-1]['disposition'] == cell['disposition'] and runs[-1]['end'] == cell['cell_start']:
        runs[-1]['end'] = cell['cell_end']
        runs[-1]['length_cells'] += 1
        return
    if cell['disposition'] in {'unexpected_missing', 'planned_outage'}:
        runs.append({
            'disposition': cell['disposition'], 'start': cell['cell_start'],
            'end': cell['cell_end'], 'length_cells': 1, 'duration_seconds': None,
        })


def _ratio(numerator, denominator):
    from decimal import Decimal
    from hangar_ingest.types import decimal_string
    if not denominator:
        return decimal_string(Decimal(0))
    return decimal_string(Decimal(numerator) / Decimal(denominator))
