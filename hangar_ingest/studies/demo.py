"""Reproducible local demonstration of the missingness study.

The fixture is synthetic local data: three aircraft whose hottest, high-load
readings are the ones most often lost at the gateway.  The runner shows that a
mean computed only from successful uploads is optimistic, then demonstrates that
a calibration correction after the study does not alter the frozen evidence.
"""
from hangar_ingest.types import object_fields, fail
from hangar_ingest.quality import devices
from hangar_ingest.studies import artifacts


REQUIRED_FIXTURE = {'setup', 'batch', 'study', 'conditions', 'scenarios'}


def run(app, fixture):
    object_fields(fixture, REQUIRED_FIXTURE)
    app.batch(fixture['setup'])
    staged = app.call('batch.stage', fixture['batch'])
    drained = app.call('batch.drain', {'batch_id': staged['batch_id'], 'chunk': 50})

    study_input = dict(fixture['study'])
    study_id = study_input['study_id']
    # The analysis cutoff is explicitly pre-registered just after the grid ends;
    # it must never default to a wall clock that predates the collected data.
    study_input.setdefault('frozen_at', study_input['end'])
    study_input.setdefault('cutoff_at', study_input['end'])
    windows = []
    for device in devices.list_devices(app.db):
        windows.append({
            'device_id': device['device_id'],
            'operational_start': study_input['start'],
            'operational_end': study_input['end'],
            'outages': [],
        })
    app.call('study.create', {**study_input, 'device_windows': windows})
    coverage = app.call('study.grid', {'study_id': study_id})
    app.call('study.conditions.import', {'study_id': study_id, **fixture['conditions']})
    strata = app.call('study.strata_report', {'study_id': study_id})
    model = app.call('study.model.fit', {'study_id': study_id, 'model_id': 'logistic-v1'})

    scenario_results = []
    for scenario in fixture['scenarios']:
        params = {'study_id': study_id, 'model_id': 'logistic-v1', **scenario}
        scenario_results.append(app.call('study.scenario', params))

    evidence_before = artifacts.evidence(app.db, study_id)
    # Simulate a later calibration revision; the study snapshot must keep revision one.
    first_device = windows[0]['device_id']
    calibration_id = app.db.one(
        'SELECT calibration_id FROM calibrations WHERE device_id=? ORDER BY valid_from LIMIT 1',
        (first_device,),
    )['calibration_id']
    app.call('calibration.correct', {'calibration_id': calibration_id, 'revision': 1,
                                     'scale': '1.02', 'offset': '0.5', 'clock_offset_us': 0})
    evidence_after = artifacts.evidence(app.db, study_id)
    if evidence_after['digest'] != evidence_before['digest']:
        fail('demo_evidence', 'Frozen study evidence changed after a calibration revision')
    replay = app.call('study.replay', {'study_id': study_id})
    verification = app.call('study.evidence.verify', {'study_id': study_id})

    visible_means = {result['scenario_id']: result['data_identifiable']['visible_mean']
                     for result in scenario_results}
    full_means = {result['scenario_id']: result['assumption_driven']['full_grid_mean']
                  for result in scenario_results}
    hot = next(result for result in scenario_results if result['scenario_id'] == 'hot-missing')
    return {
        'ok': True,
        'ingested_rows': drained['batch']['accepted'],
        'quarantined_rows': drained['batch']['rejected'],
        'grid': {
            'total_cells': coverage['total_cells'],
            'counts': coverage['counts'],
            'longest_unexpected_gap_cells': coverage['longest_unexpected_gap_cells'],
            'cells_with_duplicate_uploads': coverage['cells_with_duplicate_uploads'],
            'duplicate_uploads_never_add_coverage': coverage['duplicate_uploads_never_add_coverage'],
            'missing_cells_never_interpolated': coverage['missing_cells_never_interpolated'],
            'long_gaps_remain_distinct_cells': coverage['long_gaps_remain_distinct_cells'],
        },
        'strata_coverage': [
            {'device_id': layer['device_id'], 'strata': layer['strata'],
             'counts': layer['counts'], 'valid_coverage_ratio': layer['valid_coverage_ratio']}
            for layer in strata['layers']
        ],
        'probability_model': {
            'support_ok': model['support_ok'],
            'effective_sample_size': model['weights']['effective_sample_size'],
            'extreme_weight_ratio': model['weights']['extreme_weight_ratio'],
            'held_out_brier': model['pooled_held_out_brier'],
            'folds': len(model['aircraft_held_out_folds']),
        },
        'instability_demonstration': {
            'visible_only_mean': visible_means['no-offset'],
            'full_grid_mean_no_offset_assumption': full_means['no-offset'],
            'full_grid_mean_hot_missing_assumption': full_means['hot-missing'],
            'visible_overtemperature_proportion': hot['data_identifiable']['visible_overtemperature_proportion'],
            'overtemperature_feasible_range': hot['assumption_driven']['overtemperature_proportion_range'],
            'business_flip_point': hot['flip_point'],
            'bootstrap_interval': hot['uncertainty']['full_grid_mean'],
            'bootstrap_method': hot['uncertainty']['method'],
            'bootstrap_clusters': hot['uncertainty']['clusters'],
            'explanation': 'Hot high-load cells are missing more often, so the visible mean is biased low.',
        },
        'evidence_after_calibration_revision': {
            'digest_unchanged': evidence_after['digest'] == evidence_before['digest'],
            'frozen_calibration_revisions': sorted({c['revision'] for c in replay['calibrations']}),
            'live_calibration_revision': app.db.one(
                'SELECT revision FROM calibrations WHERE calibration_id=?', (calibration_id,))['revision'],
            'verification': verification,
        },
    }
