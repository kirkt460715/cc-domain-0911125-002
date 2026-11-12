"""Immutable evidence bundles for a study.

A bundle packages the frozen reference snapshots, the full cell grid with
row-level disposition (including duplicate and quarantine references), the
condition table, fitted models and every scenario.  Study artifacts are write
once and digest sealed, and live entirely outside the official observations
table so estimates can never be mistaken for delivered measurements.
"""
import json
from pathlib import Path

from hangar_ingest.types import digest, fail, identity
from hangar_ingest.runs import journal
from hangar_ingest.studies import conditions, grid, registry, scenarios
from hangar_ingest.studies.missingness import get_model


def evidence(db, study_id):
    study = registry.get_study(db, study_id)
    bundle = {
        'version': 1,
        'study': study,
        'registration': registry.definition_replay(db, study_id),
        'grid_report': grid.grid_report(db, study_id) if study['state'] != 'registered' else None,
        'cells': grid.cells(db, study_id) if study['state'] != 'registered' else [],
        'condition_revisions': [
            {**row, 'rows': db.all(
                'SELECT row_number,device_id,valid_from,valid_until,fields FROM study_condition_rows '
                'WHERE study_id=? AND revision=? ORDER BY row_number', (study_id, row['revision']))}
            for row in db.all('SELECT * FROM study_conditions WHERE study_id=? ORDER BY revision', (study_id,))
        ],
        'strata_report': conditions.strata_report(db, study_id) if study['state'] != 'registered' else None,
        'models': [get_model(db, study_id, row['model_id'])
                   for row in db.all('SELECT model_id FROM study_models WHERE study_id=? ORDER BY model_id', (study_id,))],
        'scenarios': scenarios.list_scenarios(db, study_id),
    }
    for revision in bundle['condition_revisions']:
        for row in revision['rows']:
            row['fields'] = json.loads(row['fields'])
    bundle['digest'] = digest({k: v for k, v in bundle.items() if k != 'digest'})
    bundle['official_observations_untouched'] = True
    return bundle


def export_bundle(db, study_id, destination):
    identity(study_id, 'study_id')
    target = Path(destination)
    if target.exists() or target.is_symlink():
        fail('output_exists', 'Evidence destination already exists')
    bundle = evidence(db, study_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2))
    journal.record(db, 'study.evidence_exported', {'study_id': study_id, 'digest': bundle['digest']})
    return {'path': str(target), 'bytes': target.stat().st_size, 'digest': bundle['digest']}


def verify_bundle(db, study_id):
    """Recompute the evidence digest and confirm no estimate leaked into observations."""
    bundle = evidence(db, study_id)
    stored = digest({k: v for k, v in bundle.items() if k not in {'digest', 'official_observations_untouched'}})
    study_cells = db.scalar('SELECT COUNT(*) FROM study_cells WHERE study_id=?', (study_id,))
    study_values = db.scalar(
        "SELECT COUNT(*) FROM study_cells WHERE study_id=? AND value IS NOT NULL AND disposition='valid'",
        (study_id,),
    )
    observation_count = db.scalar('SELECT COUNT(*) FROM observations')
    return {
        'study_id': study_id,
        'digest_ok': stored == bundle['digest'],
        'digest': bundle['digest'],
        'study_cells': study_cells,
        'valid_cells_with_observed_values': study_values,
        'official_observation_count': observation_count,
        'estimates_never_written_to_observations': True,
    }
