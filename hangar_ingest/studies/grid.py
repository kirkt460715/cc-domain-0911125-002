"""Expected reporting grid and per-cell disposition classification.

Each cell is exactly one of:

* ``valid`` — an accepted observation lands in the cell, its alias resolved under
  the frozen alias table, a calibration in its validity interval covers the
  reading and the frozen quality rules do not reject it;
* ``quality_excluded`` — data reached the gateway for the cell but the row was
  quarantined, or the accepted row has no valid alias/calibration coverage under
  the frozen reference data;
* ``planned_outage`` — the cell falls inside a registered downtime window;
* ``unexpected_missing`` — the device should have reported but nothing usable
  arrived.

Repeated uploads into one cell never improve coverage: only the first accepted
observation counts and the duplicate tally is retained as evidence.  Long gaps
are kept as runs of individual missing cells; nothing interpolates across them.
"""
import json

from hangar_ingest.types import canonical, decimal_string, digest, fail, from_micros, micros
from hangar_ingest.quality import rules as quality_rules
from hangar_ingest.runs import journal
from hangar_ingest.studies import registry


def _frozen_calibration(db, study_id, device_id, observed_us):
    return db.one(
        'SELECT * FROM study_calibration_snapshots WHERE study_id=? AND device_id=? '
        'AND valid_from<=? AND valid_until>? ORDER BY revision',
        (study_id, device_id, from_micros(observed_us), from_micros(observed_us)),
    )


def _frozen_rule(db, study_id, device_id):
    row = db.one('SELECT * FROM study_rule_snapshots WHERE study_id=? AND device_id=?', (study_id, device_id))
    if row is None:
        fail('study_snapshot', 'Frozen quality rule snapshot is missing', device_id=device_id)
    return json.loads(row['payload'])


def _outage_lookup(db, study_id):
    table = {}
    for outage in db.all('SELECT * FROM study_outages WHERE study_id=?', (study_id,)):
        table.setdefault(outage['device_id'], []).append(
            (micros(outage['start']), micros(outage['end']), outage['outage_id']),
        )
    for intervals in table.values():
        intervals.sort()
    return table


def _outage_at(table, device_id, cell_start, cell_end):
    for start, end, outage_id in table.get(device_id, ()):
        if start >= cell_end:
            break
        if start <= cell_start and end >= cell_end:
            return outage_id
    return None


def _quarantine_cells(db, study_id, device_id, cell_of, cutoff_us):
    """Map grid cells to quarantine rows aimed at the device before the cutoff,
    resolving the external identifier through the frozen alias snapshots."""
    result = {}
    for row in db.all('SELECT * FROM quarantine'):
        payload = json.loads(row['payload'])
        if not isinstance(payload, dict) or 'observed_at' not in payload or 'device' not in payload:
            continue
        source = db.one('SELECT source FROM batches WHERE batch_id=?', (row['batch_id'],))
        if source is None:
            continue
        try:
            observed_us = micros(payload['observed_at'])
        except Exception:
            continue
        if observed_us > cutoff_us:
            continue
        resolved = db.one(
            'SELECT device_id FROM study_alias_snapshots WHERE study_id=? AND source=? AND external_id=? '
            'AND valid_from<=? AND valid_until>?',
            (study_id, source['source'], payload['device'],
             from_micros(observed_us), from_micros(observed_us)),
        )
        if resolved is None or resolved['device_id'] != device_id:
            continue
        index = cell_of(observed_us)
        if index is None:
            continue
        previous = result.get(index)
        if previous is None or (row['batch_id'], row['row_number']) < (previous['batch_id'], previous['row_number']):
            result[index] = row
    return result


def build_grid(db, study_id):
    study = registry.require_state(db, study_id, {'registered'})
    interval_us = study['interval_seconds'] * 1000000
    cutoff_us = micros(study['cutoff_at'])
    outages = _outage_lookup(db, study_id)
    global_index = 0
    inserted = 0
    with db.transaction():
        for window in registry.device_windows(db, study_id):
            device_id = window['device_id']
            op_start = micros(window['operational_start'])
            op_end = micros(window['operational_end'])
            frozen_rule = _frozen_rule(db, study_id, device_id)

            def cell_of(observed_us, _start=op_start, _end=op_end, _step=interval_us):
                if observed_us < _start or observed_us >= _end:
                    return None
                return (observed_us - _start) // _step

            quarantined = _quarantine_cells(db, study_id, device_id, cell_of, cutoff_us)
            accepted = {}
            duplicates = {}
            for obs in db.all(
                'SELECT * FROM observations WHERE device_id=? AND event_time>=? AND event_time<? '
                'AND raw_time<=? ORDER BY event_time,observation_id',
                (device_id, window['operational_start'], window['operational_end'], study['cutoff_at']),
            ):
                index = cell_of(micros(obs['event_time']))
                if index is None:
                    continue
                if index in accepted:
                    # Dense re-uploads are recorded as evidence but can never add coverage.
                    duplicates[index] = duplicates.get(index, 0) + 1
                    continue
                raw_us = micros(obs['raw_time'])
                alias_ok = _alias_covered(db, study_id, obs, raw_us)
                calibration = _frozen_calibration(db, study_id, device_id, raw_us)
                calibration_ok = calibration is not None or not frozen_rule['required_calibration']
                rule_errors = quality_rules.bounds(obs['value'], frozen_rule)
                quality_ok = not any(reason['code'] in {'below_minimum', 'above_maximum'} for reason in rule_errors)
                accepted[index] = {
                    'observation': obs, 'calibration': calibration,
                    'alias_ok': alias_ok, 'calibration_ok': calibration_ok, 'quality_ok': quality_ok,
                    'rule_errors': [reason['code'] for reason in rule_errors],
                }
            device_cells = (op_end - op_start) // interval_us
            for index in range(device_cells):
                cell_start = op_start + index * interval_us
                cell_end = cell_start + interval_us
                outage_id = _outage_at(outages, device_id, cell_start, cell_end)
                disposition = 'planned_outage' if outage_id else 'unexpected_missing'
                reason = 'registered_outage' if outage_id else 'no_usable_report'
                observation_id = None
                calibration_id = None
                calibration_revision = None
                quarantine_id = None
                value = None
                alias_resolved = 0
                calibration_covered = 0
                duplicate_count = duplicates.get(index, 0)
                if index in quarantined and outage_id is None:
                    disposition = 'quality_excluded'
                    reason = 'quarantined_at_ingestion'
                    quarantine_id = quarantined[index]['quarantine_id']
                accepted_row = accepted.get(index)
                if accepted_row is not None and outage_id is None:
                    obs = accepted_row['observation']
                    calibration = accepted_row['calibration']
                    observation_id = obs['observation_id']
                    alias_resolved = int(accepted_row['alias_ok'])
                    calibration_covered = int(accepted_row['calibration_ok'])
                    if calibration is not None:
                        calibration_id = calibration['calibration_id']
                        calibration_revision = calibration['revision']
                    if accepted_row['alias_ok'] and accepted_row['calibration_ok'] and accepted_row['quality_ok']:
                        disposition = 'valid'
                        reason = 'accepted_with_valid_reference_data'
                        value = obs['value']
                    else:
                        disposition = 'quality_excluded'
                        failures = []
                        if not accepted_row['alias_ok']:
                            failures.append('alias_unresolved_under_frozen_snapshot')
                        if not accepted_row['calibration_ok']:
                            failures.append('no_calibration_coverage')
                        if not accepted_row['quality_ok']:
                            failures.extend(accepted_row['rule_errors'])
                        reason = '|'.join(failures)
                db.execute(
                    'INSERT INTO study_cells(study_id,cell_index,device_id,aircraft,cell_start,cell_end,'
                    'disposition,disposition_reason,observation_id,calibration_id,calibration_revision,'
                    'alias_resolved,calibration_covered,rule_revision,duplicate_count,quarantine_id,outage_id,'
                    'value,strata) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (study_id, global_index, device_id, window['aircraft'], from_micros(cell_start),
                     from_micros(cell_end), disposition, reason, observation_id, calibration_id,
                     calibration_revision, alias_resolved, calibration_covered, frozen_rule['revision'],
                     duplicate_count, quarantine_id, outage_id, value, canonical({})),
                )
                inserted += 1
                global_index += 1
        if inserted != global_index:
            fail('study_grid', 'Grid cell accounting failed')
        registry.advance_state(db, study_id, 'gridded')
        journal.record(db, 'study.gridded', {
            'study_id': study_id, 'cells': global_index,
            'digest': digest({'study_id': study_id, 'state': 'gridded'}),
        })
    return grid_report(db, study_id)


def _alias_covered(db, study_id, obs, raw_us):
    """An accepted observation is alias-covered when its source/external id still
    resolves uniquely to the same device under the frozen alias table."""
    external = db.one(
        'SELECT 1 FROM study_alias_snapshots WHERE study_id=? AND source=? AND device_id=? '
        'AND valid_from<=? AND valid_until>?',
        (study_id, obs['source'], obs['device_id'], from_micros(raw_us), from_micros(raw_us)),
    )
    return external is not None


def cells(db, study_id, *, disposition=None, device_id=None):
    registry.get_study(db, study_id)
    clauses = ['study_id=?']
    args = [study_id]
    if disposition is not None:
        if disposition not in registry.DISPOSITIONS:
            fail('disposition', 'Unknown grid disposition')
        clauses.append('disposition=?')
        args.append(disposition)
    if device_id is not None:
        clauses.append('device_id=?')
        args.append(device_id)
    rows = db.all(
        f"SELECT * FROM study_cells WHERE {' AND '.join(clauses)} ORDER BY cell_index,device_id", args,
    )
    for row in rows:
        row['strata'] = json.loads(row['strata'])
    return rows


def _runs(disposition_cells):
    """Consecutive same-disposition cells become gap runs with their lengths."""
    runs = []
    current = None
    for row in disposition_cells:
        key = (row['device_id'], row['disposition'])
        if current is None or key != (current['device_id'], current['disposition']):
            if current is not None:
                runs.append(current)
            current = {
                'device_id': row['device_id'], 'disposition': row['disposition'],
                'start': row['cell_start'], 'end': row['cell_end'],
                'length_cells': 1, 'duration_seconds': None,
            }
        else:
            current['end'] = row['cell_end']
            current['length_cells'] += 1
    if current is not None:
        runs.append(current)
    return runs


def grid_report(db, study_id):
    study = registry.require_state(db, study_id, {'gridded', 'modeled', 'scenario'})
    interval = study['interval_seconds']
    all_cells = cells(db, study_id)
    counts = {label: 0 for label in registry.DISPOSITIONS}
    duplicate_cells = 0
    for row in all_cells:
        counts[row['disposition']] += 1
        if row['duplicate_count']:
            duplicate_cells += 1
    total = len(all_cells)
    report_rows = []
    for window in registry.device_windows(db, study_id):
        device_cells = [row for row in all_cells if row['device_id'] == window['device_id']]
        device_counts = {label: 0 for label in registry.DISPOSITIONS}
        for row in device_cells:
            device_counts[row['disposition']] += 1
        operational = len(device_cells)
        accountable = operational - device_counts['planned_outage']
        report_rows.append({
            'device_id': window['device_id'],
            'aircraft': window['aircraft'],
            'operational_start': window['operational_start'],
            'operational_end': window['operational_end'],
            'cells': operational,
            **device_counts,
            'valid_coverage_ratio': _ratio(device_counts['valid'], accountable),
            'unexpected_missing_ratio': _ratio(device_counts['unexpected_missing'], accountable),
        })
    missing_runs = _runs([row for row in all_cells if row['disposition'] in {'unexpected_missing', 'planned_outage'}])
    for run in missing_runs:
        run['duration_seconds'] = run['length_cells'] * interval
    length_histogram = {}
    for run in missing_runs:
        if run['disposition'] == 'unexpected_missing':
            length_histogram[run['length_cells']] = length_histogram.get(run['length_cells'], 0) + 1
    return {
        'study_id': study_id,
        'interval_seconds': interval,
        'period_seconds': study['period_seconds'],
        'total_cells': total,
        'counts': counts,
        'ratios': {label: _ratio(count, total) for label, count in counts.items()},
        'cells_with_duplicate_uploads': duplicate_cells,
        'duplicate_uploads_never_add_coverage': True,
        'missing_cells_never_interpolated': True,
        'long_gaps_remain_distinct_cells': True,
        'by_device': report_rows,
        'gap_runs': missing_runs,
        'unexpected_gap_length_histogram': dict(sorted(length_histogram.items())),
        'longest_unexpected_gap_cells': max(
            (run['length_cells'] for run in missing_runs if run['disposition'] == 'unexpected_missing'), default=0,
        ),
    }


def _ratio(numerator, denominator):
    from decimal import Decimal
    from hangar_ingest.types import decimal_string
    if not denominator:
        return decimal_string(Decimal(0))
    return decimal_string(Decimal(numerator) / Decimal(denominator))
