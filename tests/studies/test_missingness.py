import json
from decimal import Decimal

from hangar_ingest.types import PipelineError
from hangar_ingest.api import Application
from hangar_ingest.studies import registry, grid, conditions, missingness, scenarios, artifacts
from tests.support import PipelineCase, sample

START = '2026-11-11T10:00:00Z'
END = '2026-11-11T10:10:00Z'


def temp_row(event_id, minute, value, device='sensor1', second=0):
    return {'event_id': event_id, 'device': device,
            'observed_at': f'2026-11-11T10:{minute:02d}:{second:02d}Z', 'value': str(value), 'unit': 'C'}


class StudyCase(PipelineCase):
    def setUp(self):
        super().setUp()
        self.app = Application(self.db)
        self.app.call('device.register', {'device_id': 'dev2', 'aircraft': 'plane2', 'component': 'engine1',
                                          'metric': 'temperature', 'canonical_unit': 'C'})
        self.app.call('alias.add', {'source': 'feed1', 'external_id': 'sensor2', 'device_id': 'dev2',
                                    'valid_from': '2026-01-01T00:00:00Z', 'valid_until': '2027-01-01T00:00:00Z'})
        self.app.call('calibration.add', {'calibration_id': 'cal2', 'device_id': 'dev2',
                                          'valid_from': '2026-01-01T00:00:00Z',
                                          'valid_until': '2027-01-01T00:00:00Z'})
        self.app.call('quality.configure', {'device_id': 'dev2', 'minimum': '-50', 'maximum': '150'})

    def study(self, study_id='study1', outages=None, strata_fields=None, bootstrap_samples=50):
        windows = [
            {'device_id': 'dev1', 'operational_start': START, 'operational_end': END, 'outages': []},
            {'device_id': 'dev2', 'operational_start': START, 'operational_end': END,
             'outages': outages or []},
        ]
        return self.app.call('study.create', {
            'study_id': study_id, 'device_windows': windows, 'start': START, 'end': END,
            'interval_seconds': 60, 'period_seconds': 300,
            'strata_fields': strata_fields or ['load'],
            'physical_minimum': '-50', 'physical_maximum': '150', 'alarm_threshold': '90',
            'bootstrap_samples': bootstrap_samples, 'seed': 7,
        })

    def ingest_rows(self, rows, batch_id='study-batch'):
        content = '\n'.join(json.dumps(row) for row in rows) + '\n'
        self.app.call('batch.stage', {'batch_id': batch_id, 'source': 'feed1', 'content': content})
        return self.app.call('batch.drain', {'batch_id': batch_id})

    def conditions_v1(self):
        return self.app.call('study.conditions.import',
                             {'study_id': 'study1', 'rows': self._condition_rows(),
                              'source': 'ops', 'version': 'v1'})

    def _condition_rows(self):
        rows = []
        for device in ('dev1', 'dev2'):
            rows.append({'device_id': device, 'valid_from': START,
                         'valid_until': '2026-11-11T10:05:00Z', 'load': 'low'})
            rows.append({'device_id': device, 'valid_from': '2026-11-11T10:05:00Z',
                         'valid_until': END, 'load': 'high'})
        return rows


class GridTests(StudyCase):
    def test_four_way_disposition_and_duplicates_never_add_coverage(self):
        rows = [
            temp_row('e1', 0, 60), temp_row('e2', 1, 70),
            # dense re-upload inside the same 10:01 cell
            temp_row('e2b', 1, 71, second=30),
            # minutes 2,3 form a two-cell unexpected gap; minutes 4-9 observed
            temp_row('e3', 4, 80), temp_row('e4', 5, 95), temp_row('e5', 6, 96),
            temp_row('e6', 7, 97), temp_row('e7', 8, 98), temp_row('e8', 9, 99),
            # quality rejection for dev2 at minute 0
            temp_row('b1', 0, 999, device='sensor2'),
            temp_row('f1', 1, 62, device='sensor2'),
            temp_row('f2', 2, 64, device='sensor2'),
        ]
        self.ingest_rows(rows)
        self.study(outages=[{'outage_id': 'o1', 'start': '2026-11-11T10:03:00Z',
                             'end': '2026-11-11T10:05:00Z', 'reason': 'scheduled'}])
        report = self.app.call('study.grid', {'study_id': 'study1'})
        self.assertEqual(report['counts']['planned_outage'], 2)
        self.assertEqual(report['counts']['quality_excluded'], 1)
        self.assertEqual(report['counts']['unexpected_missing'], 7)
        self.assertEqual(report['cells_with_duplicate_uploads'], 1)
        cells = {(c['device_id'], c['cell_start'][11:16]): c for c in
                 self.app.call('study.cells', {'study_id': 'study1'})}
        duplicate_cell = cells[('dev1', '10:01')]
        self.assertEqual(duplicate_cell['disposition'], 'valid')
        self.assertEqual(duplicate_cell['duplicate_count'], 1)
        outage_cell = cells[('dev2', '10:03')]
        self.assertEqual(outage_cell['disposition'], 'planned_outage')
        self.assertEqual(outage_cell['outage_id'], 'o1')
        missing_cell = cells[('dev1', '10:02')]
        self.assertEqual(missing_cell['disposition'], 'unexpected_missing')
        self.assertEqual(missing_cell['disposition_reason'], 'no_usable_report')
        excluded = cells[('dev2', '10:00')]
        self.assertEqual(excluded['disposition'], 'quality_excluded')
        self.assertIsNotNone(excluded['quarantine_id'])
        # Long gap stays a run of missing cells instead of an interpolated value.
        run = next(r for r in report['gap_runs']
                   if r['device_id'] == 'dev1' and r['disposition'] == 'unexpected_missing')
        self.assertEqual(run['length_cells'], 2)
        self.assertEqual(run['duration_seconds'], 120)

    def test_grid_requires_registered_state_and_is_immutable(self):
        self.study()
        self.app.call('study.grid', {'study_id': 'study1'})
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.grid', {'study_id': 'study1'})
        self.assertEqual(error.exception.code, 'study_state')

    def test_estimates_never_touch_official_observations(self):
        self.ingest_rows([temp_row('e1', 0, 60)])
        self.study()
        self.app.call('study.grid', {'study_id': 'study1'})
        before = self.db.scalar('SELECT COUNT(*) FROM observations')
        verification = self.app.call('study.evidence.verify', {'study_id': 'study1'})
        self.assertTrue(verification['digest_ok'])
        self.assertTrue(verification['estimates_never_written_to_observations'])
        self.assertEqual(self.db.scalar('SELECT COUNT(*) FROM observations'), before)

    def test_evidence_bundle_is_write_once_and_round_trips(self):
        import json
        import pathlib
        self.ingest_rows([temp_row('e1', 0, 60)])
        self.study()
        self.app.call('study.grid', {'study_id': 'study1'})
        target = pathlib.Path(self.directory.name) / 'bundle.json'
        exported = self.app.call('study.evidence.export',
                                 {'study_id': 'study1', 'destination': str(target)})
        bundle = json.loads(target.read_text())
        self.assertEqual(bundle['digest'], exported['digest'])
        self.assertTrue(bundle['official_observations_untouched'])
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.evidence.export',
                          {'study_id': 'study1', 'destination': str(target)})
        self.assertEqual(error.exception.code, 'output_exists')

    def test_misaligned_grid_and_out_of_scope_outage_are_rejected(self):
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.create', {
                'study_id': 'bad1',
                'device_windows': [{'device_id': 'dev1',
                                    'operational_start': '2026-11-11T10:00:30Z',
                                    'operational_end': END, 'outages': []}],
                'start': START, 'end': END, 'interval_seconds': 60,
                'physical_minimum': '-50', 'physical_maximum': '150', 'alarm_threshold': '90'})
        self.assertEqual(error.exception.code, 'grid_alignment')
        with self.assertRaises(PipelineError) as error:
            self.study('bad2', outages=[{'outage_id': 'o1', 'start': '2026-11-11T09:59:00Z',
                                         'end': '2026-11-11T10:01:00Z'}])
        self.assertEqual(error.exception.code, 'outage_scope')

    def test_data_arriving_after_cutoff_is_not_counted(self):
        self.ingest_rows([temp_row('e1', 0, 60)])
        # A late row lands inside the window but after the pre-registered cutoff.
        self.ingest_rows([temp_row('late', 5, 95)], batch_id='late-batch')
        self.app.call('study.create', {
            'study_id': 'cutoff1',
            'device_windows': [
                {'device_id': 'dev1', 'operational_start': START, 'operational_end': END, 'outages': []},
                {'device_id': 'dev2', 'operational_start': START, 'operational_end': END, 'outages': []}],
            'start': START, 'end': END, 'interval_seconds': 60,
            'frozen_at': END, 'cutoff_at': '2026-11-11T10:01:00Z',
            'physical_minimum': '-50', 'physical_maximum': '150', 'alarm_threshold': '90'})
        report = self.app.call('study.grid', {'study_id': 'cutoff1'})
        dev1 = next(row for row in report['by_device'] if row['device_id'] == 'dev1')
        self.assertEqual(dev1['valid'], 1)  # the 10:05 row exists but is past the cutoff
        late_cell = next(cell for cell in self.app.call('study.cells', {'study_id': 'cutoff1'})
                         if cell['device_id'] == 'dev1' and cell['cell_start'][11:16] == '10:05')
        self.assertEqual(late_cell['disposition'], 'unexpected_missing')


class StratificationTests(StudyCase):
    def test_strata_report_partitions_coverage_by_load_and_period(self):
        # High-load minutes 5-9 lose more cells than low-load minutes 0-4.
        rows = [temp_row('e1', 0, 60), temp_row('e2', 1, 70), temp_row('e3', 2, 80),
                temp_row('e4', 5, 95), temp_row('e5', 6, 96)]
        self.ingest_rows(rows)
        self.study()
        self.app.call('study.grid', {'study_id': 'study1'})
        annotation = self.conditions_v1()
        self.assertTrue(annotation['complete'])
        report = self.app.call('study.strata_report', {'study_id': 'study1'})
        high = next(layer for layer in report['layers']
                    if layer['device_id'] == 'dev1' and layer['strata'] == {'load': 'high'})
        low = next(layer for layer in report['layers']
                   if layer['device_id'] == 'dev1' and layer['strata'] == {'load': 'low'})
        self.assertEqual(high['counts']['valid'], 2)
        self.assertEqual(high['counts']['unexpected_missing'], 3)
        self.assertEqual(low['counts']['unexpected_missing'], 2)
        self.assertEqual(Decimal(high['valid_coverage_ratio']), Decimal('0.4'))

    def test_temperature_and_maintenance_fields_are_forbidden_predictors(self):
        self.ingest_rows([temp_row('e1', 0, 60)])
        # A study declaring a target-temperature or maintenance field must be rejected
        # at import time, so those outcomes can never train the observation model.
        self.study('forbidden1', strata_fields=['egt_temperature'])
        self.app.call('study.grid', {'study_id': 'forbidden1'})
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.conditions.import', {'study_id': 'forbidden1', 'rows': [
                {'device_id': 'dev1', 'valid_from': START, 'valid_until': END,
                 'egt_temperature': 99}], 'source': 'ops', 'version': 'v1'})
        self.assertEqual(error.exception.code, 'forbidden_condition_field')
        self.study('forbidden2', strata_fields=['post_repair_finding'])
        self.app.call('study.grid', {'study_id': 'forbidden2'})
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.conditions.import', {'study_id': 'forbidden2', 'rows': [
                {'device_id': 'dev1', 'valid_from': START, 'valid_until': END,
                 'post_repair_finding': 'yes'}], 'source': 'ops2', 'version': 'v1'})
        self.assertEqual(error.exception.code, 'forbidden_condition_field')

    def test_unknown_condition_field_and_overlap_are_rejected(self):
        self.ingest_rows([temp_row('e1', 0, 60)])
        self.study()
        self.app.call('study.grid', {'study_id': 'study1'})
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.conditions.import', {'study_id': 'study1', 'rows': [
                {'device_id': 'dev1', 'valid_from': START, 'valid_until': END,
                 'load': 'low', 'undeclared': 99}], 'source': 'ops', 'version': 'v1'})
        self.assertEqual(error.exception.code, 'condition_fields')
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.conditions.import', {'study_id': 'study1', 'rows': [
                {'device_id': 'dev1', 'valid_from': START, 'valid_until': '2026-11-11T10:02:00Z', 'load': 'low'},
                {'device_id': 'dev1', 'valid_from': '2026-11-11T10:01:00Z', 'valid_until': END, 'load': 'high'}],
                'source': 'ops2', 'version': 'v1'})
        self.assertEqual(error.exception.code, 'condition_overlap')


class ModelTests(StudyCase):
    def _data_with_layer_without_overlap(self, batch_id='study-batch', study_id='study1'):
        rows = []
        # dev1: low layer fully observed, high layer fully missing -> no overlap there
        for minute in range(0, 5):
            rows.append(temp_row(f'a{minute}', minute, 60 + minute))
        # dev2 mixes both layers so held-out folds still have both outcomes
        rows.append(temp_row('b0', 0, 61, device='sensor2'))
        rows.append(temp_row('b5', 5, 95, device='sensor2'))
        rows.append(temp_row('b7', 7, 97, device='sensor2'))
        self.ingest_rows(rows, batch_id=batch_id)
        self.study(study_id=study_id)
        self.app.call('study.grid', {'study_id': study_id})
        source = 'ops' if study_id == 'study1' else 'ops2'
        self.app.call('study.conditions.import',
                      {'study_id': study_id, 'rows': self._condition_rows(),
                       'source': source, 'version': 'v1'})

    def test_aircraft_held_out_validation_reports_calibration_weights_and_support(self):
        self._data_with_layer_without_overlap()
        model = self.app.call('study.model.fit', {'study_id': 'study1', 'model_id': 'm1'})
        self.assertEqual(len(model['aircraft_held_out_folds']), 2)
        held_aircraft = {fold['held_out_aircraft'] for fold in model['aircraft_held_out_folds']}
        self.assertEqual(held_aircraft, {'plane1', 'plane2'})
        self.assertTrue(all(fold['held_out_cells'] == 10 for fold in model['aircraft_held_out_folds']))
        self.assertEqual(len(model['calibration_bins_held_out']), 5)
        self.assertGreater(model['weights']['effective_sample_size'], 0)
        self.assertIn('extreme_weight_ratio', model['weights'])
        # The high layer for dev1 contains only missing cells -> positivity failure.
        self.assertFalse(model['support_ok'])
        failed = [row for row in model['overlap_support'] if not row['overlap']]
        self.assertTrue(failed)

    def test_missing_support_blocks_seemingly_precise_corrected_mean(self):
        self._data_with_layer_without_overlap()
        self.app.call('study.model.fit', {'study_id': 'study1', 'model_id': 'm1'})
        result = self.app.call('study.scenario', {'study_id': 'study1', 'model_id': 'm1', 'scenario_id': 's1',
                                                  'assumptions': [{'strata': {}, 'offset': '5'}]})
        self.assertFalse(result['inverse_probability']['estimable'])
        self.assertEqual(result['inverse_probability']['reason'], 'missing_overlap_support')

    def test_weighted_mean_requires_overlap_and_tracks_cell_weights(self):
        # Every device-stratum cell group keeps both observed and missing cells.
        rows = []
        for minute, value in [(0, 60), (2, 70), (5, 95), (7, 97)]:
            rows.append(temp_row(f'a{minute}', minute, value))
        for minute, value in [(1, 62), (3, 72), (6, 96), (8, 98)]:
            rows.append(temp_row(f'b{minute}', minute, value, device='sensor2'))
        self.ingest_rows(rows)
        self.study(bootstrap_samples=40)
        self.app.call('study.grid', {'study_id': 'study1'})
        self.conditions_v1()
        model = self.app.call('study.model.fit', {'study_id': 'study1', 'model_id': 'm1'})
        self.assertTrue(model['support_ok'])
        weighted = self.app.call('study.model.weighted_mean',
                                 {'study_id': 'study1', 'model_id': 'm1'})
        self.assertTrue(weighted['estimable'])
        self.assertEqual(len(weighted['cells']), model['training_cells'] - model['missing_cells'])
        self.assertTrue(all(cell['weight'] >= 1 for cell in weighted['cells']))
        # The unsupported-support dataset must refuse the weighted mean.
        self._data_with_layer_without_overlap(batch_id='support-batch', study_id='study2')
        self.app.call('study.model.fit', {'study_id': 'study2', 'model_id': 'm2'})
        blocked = self.app.call('study.model.weighted_mean',
                                {'study_id': 'study2', 'model_id': 'm2'})
        self.assertFalse(blocked['estimable'])
        self.assertEqual(blocked['reason'], 'missing_overlap_support')

    def test_model_needs_two_aircraft_for_held_out_validation(self):
        single = Application(self.db)
        single.call('study.create', {
            'study_id': 'solo', 'device_windows': [
                {'device_id': 'dev1', 'operational_start': START, 'operational_end': END, 'outages': []}],
            'start': START, 'end': END, 'interval_seconds': 60,
            'physical_minimum': '-50', 'physical_maximum': '150', 'alarm_threshold': '90',
            'strata_fields': ['load']})
        single.call('study.grid', {'study_id': 'solo'})
        rows = [{'device_id': 'dev1', 'valid_from': START, 'valid_until': END, 'load': 'low'}]
        single.call('study.conditions.import',
                    {'study_id': 'solo', 'rows': rows, 'source': 'ops', 'version': 'v1'})
        with self.assertRaises(PipelineError) as error:
            single.call('study.model.fit', {'study_id': 'solo', 'model_id': 'm1'})
        self.assertEqual(error.exception.code, 'model_validation')


class ScenarioTests(StudyCase):
    def _grid(self):
        # Missing cells concentrate in the hot/high-load segment.
        rows = [temp_row('e1', 0, 60), temp_row('e2', 1, 65), temp_row('e3', 2, 70),
                temp_row('e4', 3, 75), temp_row('e5', 4, 80),
                temp_row('e6', 5, 95), temp_row('e7', 8, 98)]
        self.ingest_rows(rows)
        self.study()
        self.app.call('study.grid', {'study_id': 'study1'})
        self.conditions_v1()

    def test_assumption_bounded_mean_and_feasible_overtemperature_range(self):
        self._grid()
        result = self.app.call('study.scenario', {
            'study_id': 'study1', 'scenario_id': 'hot-missing',
            'assumptions': [
                {'strata': {'load': 'low'}, 'offset': '0'},
                {'strata': {'load': 'high'}, 'offset': '8', 'minimum': '85', 'maximum': '120'},
            ]})
        identifiable = result['data_identifiable']
        driven = result['assumption_driven']
        # Hot missing cells push the assumption-driven full mean above the visible-only mean.
        self.assertGreater(Decimal(driven['full_grid_mean']), Decimal(identifiable['visible_mean']))
        feasible = driven['overtemperature_proportion_range']
        self.assertLess(Decimal(feasible['minimum']), Decimal(feasible['maximum']))
        self.assertEqual(feasible['derivation'], 'physical_bounds_plus_assumed_layer_means')
        self.assertFalse(result['claims']['nonrandom_missingness_corrected'])
        self.assertFalse(result['claims']['missing_distribution_known'])
        self.assertTrue(result['flip_point']['estimable'])
        uncertainty = result['uncertainty']
        self.assertEqual(uncertainty['method'], 'aircraft_cluster_bootstrap')
        self.assertEqual(uncertainty['clusters'], 2)
        self.assertTrue(uncertainty['full_grid_mean']['estimable'])

    def test_physical_bounds_clip_assumption_and_invalid_assumptions_fail(self):
        self._grid()
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.scenario', {
                'study_id': 'study1', 'scenario_id': 'bad',
                'assumptions': [{'strata': {'load': 'high'}, 'offset': '1000',
                                 'minimum': '0', 'maximum': '200'}]})
        self.assertEqual(error.exception.code, 'assumption_bounds')
        with self.assertRaises(PipelineError) as error:
            self.app.call('study.scenario', {
                'study_id': 'study1', 'scenario_id': 'bad2',
                'assumptions': [{'strata': {'load': 'unknown'}, 'offset': '1'}]})
        self.app.call('study.scenario', {
            'study_id': 'study1', 'scenario_id': 'clipped',
            'assumptions': [{'strata': {'load': 'low'}, 'offset': '0'},
                            {'strata': {'load': 'high'}, 'offset': '1000',
                             'minimum': '85', 'maximum': '120'}]})
        # Offset beyond the physical range is clipped, never producing an out-of-range mean.
        stored = self.app.call('study.scenarios', {'study_id': 'study1'})[0]
        for layer in stored['results']['layers']:
            if layer['strata'] == {'load': 'high'} and layer['assumed_missing_mean'] is not None:
                self.assertLessEqual(Decimal(layer['assumed_missing_mean']), Decimal('120'))

    def test_naive_average_is_unstable_against_hot_segment_missingness(self):
        self._grid()
        neutral = self.app.call('study.scenario', {
            'study_id': 'study1', 'scenario_id': 'neutral',
            'assumptions': [{'strata': {}, 'offset': '0'}]})
        hot = self.app.call('study.scenario', {
            'study_id': 'study1', 'scenario_id': 'hot',
            'assumptions': [{'strata': {'load': 'low'}, 'offset': '0'},
                            {'strata': {'load': 'high'}, 'offset': '10',
                             'minimum': '85', 'maximum': '120'}]})
        visible = Decimal(neutral['data_identifiable']['visible_mean'])
        full_neutral = Decimal(neutral['assumption_driven']['full_grid_mean'])
        full_hot = Decimal(hot['assumption_driven']['full_grid_mean'])
        # Even the zero-offset full-grid mean already differs from the visible-only
        # average because the missing cells sit disproportionately in one layer.
        self.assertNotEqual(full_neutral, visible)
        self.assertGreater(full_hot, full_neutral)
        self.assertGreater(full_hot - visible, Decimal('1'))  # the conclusion moves materially


class EvidenceTests(StudyCase):
    def test_frozen_snapshots_survive_calibration_revision(self):
        self.ingest_rows([temp_row('e1', 0, 20), temp_row('e2', 5, 95)])
        self.study()
        self.app.call('study.grid', {'study_id': 'study1'})
        evidence_before = self.app.call('study.evidence', {'study_id': 'study1'})
        frozen_calibration = evidence_before['registration']['calibrations'][0]
        self.assertEqual(frozen_calibration['revision'], 1)
        # A later calibration correction changes live data but not the frozen study evidence.
        self.app.call('calibration.correct', {'calibration_id': 'cal1', 'revision': 1,
                                              'scale': '2', 'offset': '0', 'clock_offset_us': 0})
        replay = self.app.call('study.replay', {'study_id': 'study1'})
        self.assertEqual(replay['calibrations'][0]['revision'], 1)
        evidence_after = self.app.call('study.evidence', {'study_id': 'study1'})
        self.assertEqual(evidence_after['digest'], evidence_before['digest'])
        cells = evidence_after['cells']
        self.assertTrue(all(c['calibration_revision'] == 1 for c in cells if c['calibration_revision']))
