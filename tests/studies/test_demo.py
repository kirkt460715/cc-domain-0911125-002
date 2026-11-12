import json
import tempfile
import unittest
from pathlib import Path

from hangar_ingest.runs.database import Database
from tests.support import Clock

FIXTURE = Path(__file__).resolve().parents[2] / 'fixtures' / 'missingness_demo.json'


class MissingnessDemoTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'demo.db'
        self.db = Database(self.path, clock=Clock())

    def tearDown(self):
        self.db.close()
        self.directory.cleanup()

    def test_demo_shows_hot_segment_instability_and_keeps_frozen_evidence(self):
        from hangar_ingest.studies.demo import run
        from hangar_ingest.api import Application
        fixture = json.loads(FIXTURE.read_text())
        result = run(Application(self.db), fixture)
        self.assertTrue(result['ok'])
        grid = result['grid']
        self.assertEqual(grid['total_cells'], 90)
        self.assertEqual(grid['counts']['valid'], 79)
        self.assertEqual(grid['counts']['unexpected_missing'], 11)
        # Every low-load layer is complete; missingness concentrates in high load.
        for layer in result['strata_coverage']:
            if layer['strata']['load'] == 'low':
                self.assertEqual(layer['valid_coverage_ratio'], '1')
            else:
                self.assertLess(float(layer['valid_coverage_ratio']), 0.85)
        demonstration = result['instability_demonstration']
        visible = float(demonstration['visible_only_mean'])
        hot = float(demonstration['full_grid_mean_hot_missing_assumption'])
        self.assertGreater(hot, visible)
        feasible = demonstration['overtemperature_feasible_range']
        self.assertLess(float(feasible['minimum']), float(feasible['maximum']))
        # The model refuses to fabricate a corrected mean when support is incomplete.
        self.assertFalse(result['probability_model']['support_ok'])
        # Calibration revision two must not overwrite the frozen revision-one evidence.
        evidence = result['evidence_after_calibration_revision']
        self.assertTrue(evidence['digest_unchanged'])
        self.assertEqual(evidence['frozen_calibration_revisions'], [1])
        self.assertEqual(evidence['live_calibration_revision'], 2)
        self.assertTrue(evidence['verification']['estimates_never_written_to_observations'])
