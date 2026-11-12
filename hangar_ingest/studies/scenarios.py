"""Pre-registered offset scenarios and assumption-bounded temperature conclusions.

Engineers supply, per condition layer, an explicit offset assumption stating how
the mean of missing cells relates to the mean of visible cells in that layer,
bounded by the pre-registered physical range.  Two quantities are reported:

* quantities identifiable from data (visible means, observed over-temperature
  proportions, coverage) are reported as such;
* assumption-driven quantities (full-grid means, feasible over-temperature
  ranges) are labelled with the scenario that produced them.

A mean-offset assumption is never treated as a known full distribution.  The
over-temperature proportion is therefore returned as a feasibility interval
derived from the physical bounds and the assumed layer means rather than a
single fabricated number.  Nothing here corrects non-random missingness.
"""
import json
import random
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from hangar_ingest.types import canonical, decimal_string, digest, fail, identity, number
from hangar_ingest.runs import journal
from hangar_ingest.studies import conditions, registry


def _dec(value):
    return Decimal(str(value))


def _assumption_for(layer_assumptions, strata_key, fields):
    target = dict(zip(fields, strata_key))
    for assumption in layer_assumptions:
        if assumption.get('strata') == target:
            return assumption
    for assumption in layer_assumptions:
        if assumption.get('strata') == {} or 'strata' not in assumption:
            return assumption
    fail('assumption_missing', 'No offset assumption supplied for a condition layer', strata=target)


def _validate_assumptions(assumptions, fields, physical_low, physical_high):
    if not isinstance(assumptions, list) or not assumptions:
        fail('assumptions', 'Scenarios need a nonempty assumption list')
    seen = set()
    cleaned = []
    for assumption in assumptions:
        if not isinstance(assumption, dict) or 'offset' not in assumption:
            fail('assumption_shape', 'Each assumption needs strata and offset')
        strata = assumption.get('strata', {})
        if not isinstance(strata, dict) or set(strata) - set(fields):
            fail('assumption_strata', 'Assumption strata may only use declared fields')
        key = canonical(strata)
        if key in seen:
            fail('assumption_duplicate', 'Duplicate assumption for one condition layer')
        seen.add(key)
        offset = number(assumption['offset'], 'offset')
        low = _dec(assumption['minimum']) if 'minimum' in assumption else physical_low
        high = _dec(assumption['maximum']) if 'maximum' in assumption else physical_high
        if not physical_low <= low <= high <= physical_high:
            fail('assumption_bounds', 'Assumption range must stay inside the physical bounds')
        cleaned.append({'strata': strata, 'offset': decimal_string(offset),
                        'minimum': decimal_string(low), 'maximum': decimal_string(high)})
    return cleaned


def _layer_cells(db, study_id):
    study = registry.get_study(db, study_id)
    fields = study['strata_fields']
    layers = {}
    for cell in db.all('SELECT * FROM study_cells WHERE study_id=? ORDER BY cell_index', (study_id,)):
        if cell['disposition'] == 'planned_outage':
            continue
        key = conditions.strata_key(cell, fields)
        layers.setdefault(key, []).append(cell)
    return study, fields, layers


def _layer_statistics(study, cells, assumption):
    """Data-identifiable visible statistics plus assumption-constrained missing mean."""
    visible = [cell for cell in cells if cell['disposition'] == 'valid']
    missing = [cell for cell in cells if cell['disposition'] in {'unexpected_missing', 'quality_excluded'}]
    visible_values = [_dec(cell['value']) for cell in visible]
    visible_mean = sum(visible_values) / len(visible_values) if visible_values else None
    offset = _dec(assumption['offset'])
    bound_low = _dec(assumption['minimum'])
    bound_high = _dec(assumption['maximum'])
    missing_mean = None
    if missing:
        if visible_mean is None:
            # No visible anchor: the physical/assumption range is all the evidence available.
            missing_mean = (bound_low + bound_high) / 2
        else:
            candidate = visible_mean + offset
            missing_mean = min(max(candidate, bound_low), bound_high)
    return {
        'cells': len(cells),
        'valid_cells': len(visible),
        'missing_cells': len(missing),
        'visible_mean': decimal_string(visible_mean) if visible_mean is not None else None,
        'visible_minimum': decimal_string(min(visible_values)) if visible_values else None,
        'visible_maximum': decimal_string(max(visible_values)) if visible_values else None,
        'assumed_missing_mean': decimal_string(missing_mean) if missing_mean is not None else None,
        'assumption': {
            'offset': decimal_string(offset),
            'minimum': decimal_string(bound_low),
            'maximum': decimal_string(bound_high),
        },
        'identifiable': {
            'visible_mean': visible_mean is not None,
            'missing_mean': False,
        },
    }


def _full_mean(layer_stats):
    """Assumption-driven full-grid mean over all operational, non-outage cells."""
    total_cells = 0
    total = Decimal(0)
    for stats in layer_stats.values():
        n = stats['cells']
        total_cells += n
        if not n:
            continue
        if stats['assumed_missing_mean'] is None:
            total += _dec(stats['visible_mean']) * n
        elif stats['visible_mean'] is None:
            # Entire layer is missing: only the assumption-constrained mean is available.
            total += _dec(stats['assumed_missing_mean']) * n
        else:
            total += _dec(stats['visible_mean']) * stats['valid_cells'] \
                + _dec(stats['assumed_missing_mean']) * stats['missing_cells']
    return total / total_cells if total_cells else None


def _overtemperature_feasible(layer_stats, alarm, physical_low, physical_high):
    """Feasible range of the over-temperature proportion.

    Visible cells fix their above/below counts exactly.  For missing cells we do
    not invent a distribution; each layer contributes the tightest bounds
    compatible with its physical range and assumed missing mean via mean-feasibility
    (Markov-style) constraints rather than the loosest all-above/all-below envelope.
    """
    total = sum(stats['cells'] for stats in layer_stats.values())
    fixed_above = 0
    lower_missing_above = 0
    upper_missing_above = 0
    for stats in layer_stats.values():
        fixed_above += stats.get('_visible_above', 0)
        missing = stats['missing_cells']
        if not missing or stats['assumed_missing_mean'] is None:
            continue
        mean = _dec(stats['assumed_missing_mean'])
        if mean > alarm:
            lower_fraction = (mean - alarm) / (physical_high - alarm)
            lower_missing_above += _ceil_fraction(lower_fraction, missing)
        if alarm > physical_low:
            upper_fraction = min(Decimal(1), max(Decimal(0), (mean - physical_low) / (alarm - physical_low)))
        else:
            upper_fraction = Decimal(1)
        upper_missing_above += min(missing, _floor_fraction(upper_fraction, missing))
    lower = (fixed_above + lower_missing_above) / total if total else Decimal(0)
    upper = (fixed_above + upper_missing_above) / total if total else Decimal(0)
    return min(lower, upper), max(lower, upper), total


def _ceil_fraction(fraction, count):
    whole = (fraction * count).to_integral_value(rounding=ROUND_CEILING)
    return max(0, min(count, int(whole)))


def _floor_fraction(fraction, count):
    whole = (fraction * count).to_integral_value(rounding=ROUND_FLOOR)
    return max(0, min(count, int(whole)))


def evaluate(db, study_id, scenario_id, assumptions, *, model_id=None):
    study = registry.require_state(db, study_id, {'gridded', 'modeled', 'scenario'})
    identity(scenario_id, 'scenario_id')
    if db.one('SELECT 1 FROM study_scenarios WHERE study_id=? AND scenario_id=?', (study_id, scenario_id)):
        fail('scenario_conflict', 'Scenario identifier already exists for this study')
    physical_low = _dec(study['physical_minimum'])
    physical_high = _dec(study['physical_maximum'])
    alarm = _dec(study['alarm_threshold'])
    cleaned = _validate_assumptions(assumptions, study['strata_fields'], physical_low, physical_high)
    _, fields, layers = _layer_cells(db, study_id)
    layer_stats = {}
    for key, cells in sorted(layers.items()):
        assumption = _assumption_for(cleaned, key, fields)
        stats = _layer_statistics(study, cells, assumption)
        visible_values = [_dec(cell['value']) for cell in cells if cell['disposition'] == 'valid']
        stats['_visible_above'] = sum(1 for value in visible_values if value > alarm)
        stats['visible_overtemperature'] = decimal_string(
            (Decimal(stats['_visible_above']) / len(visible_values)) if visible_values else Decimal(0),
        )
        stats['strata'] = dict(zip(fields, key))
        layer_stats[key] = stats
    full_mean = _full_mean(layer_stats)
    lower, upper, total = _overtemperature_feasible(layer_stats, alarm, physical_low, physical_high)
    naive = _naive_visible_mean(layer_stats)
    result = {
        'study_id': study_id,
        'scenario_id': scenario_id,
        'alarm_threshold': decimal_string(alarm),
        'physical_bounds': {'minimum': decimal_string(physical_low), 'maximum': decimal_string(physical_high)},
        'accountable_cells': total,
        'data_identifiable': {
            'visible_mean': decimal_string(naive),
            'visible_overtemperature_proportion': decimal_string(
                sum(s['_visible_above'] for s in layer_stats.values())
                / sum(s['valid_cells'] for s in layer_stats.values())
                if sum(s['valid_cells'] for s in layer_stats.values()) else Decimal(0)),
            'valid_cells': sum(s['valid_cells'] for s in layer_stats.values()),
            'missing_cells': sum(s['missing_cells'] for s in layer_stats.values()),
        },
        'assumption_driven': {
            'full_grid_mean': decimal_string(full_mean),
            'overtemperature_proportion_range': {
                'minimum': decimal_string(lower),
                'maximum': decimal_string(upper),
                'derivation': 'physical_bounds_plus_assumed_layer_means',
            },
            'mean_shift_against_visible': decimal_string(full_mean - naive),
        },
        'layers': [{k: v for k, v in stats.items() if not k.startswith('_')} for stats in layer_stats.values()],
        'claims': {
            'nonrandom_missingness_corrected': False,
            'missing_distribution_known': False,
            'note': 'Ranges are feasible envelopes under registered assumptions, not corrected estimates.',
        },
        'flip_point': _flip_point(study, layer_stats, alarm),
    }
    if model_id is not None:
        result['inverse_probability'] = _ipw_section(db, study_id, model_id)
    assumptions_hash = digest(cleaned)
    with db.transaction():
        db.execute(
            'INSERT INTO study_scenarios(study_id,scenario_id,assumptions_hash,assumptions,results,created_at) '
            'VALUES(?,?,?,?,?,?)',
            (study_id, scenario_id, assumptions_hash, canonical(cleaned), canonical(result), db.clock()),
        )
        registry.advance_state(db, study_id, 'scenario')
        journal.record(db, 'study.scenario_evaluated', {
            'study_id': study_id, 'scenario_id': scenario_id,
            'assumptions_hash': assumptions_hash,
            'full_grid_mean': result['assumption_driven']['full_grid_mean'],
        })
    result['uncertainty'] = bootstrap(db, study_id, cleaned)
    db.execute('UPDATE study_scenarios SET results=? WHERE study_id=? AND scenario_id=?',
               (canonical(result), study_id, scenario_id))
    return result


def _naive_visible_mean(layer_stats):
    total = sum(stats['valid_cells'] for stats in layer_stats.values())
    weighted = sum(_dec(stats['visible_mean']) * stats['valid_cells']
                   for stats in layer_stats.values() if stats['visible_mean'] is not None)
    return weighted / total if total else Decimal(0)


def _flip_point(study, layer_stats, alarm):
    """Business judgement flip: the smallest mean offset (shared across layers,
    in the direction of the data gap) that would push the assumption-driven full
    mean to the alarm threshold."""
    total = sum(stats['cells'] for stats in layer_stats.values())
    missing = sum(stats['missing_cells'] for stats in layer_stats.values())
    visible_sum = sum(_dec(stats['visible_mean']) * stats['valid_cells']
                      for stats in layer_stats.values() if stats['visible_mean'] is not None)
    if not missing or not total:
        return {'estimable': False, 'reason': 'no_missing_cells'}
    required_missing_mean = (alarm * total - visible_sum) / missing
    current_visible_mean = visible_sum / sum(stats['valid_cells'] for stats in layer_stats.values()) \
        if sum(stats['valid_cells'] for stats in layer_stats.values()) else None
    if current_visible_mean is None:
        return {'estimable': False, 'reason': 'no_visible_anchor'}
    offset_needed = required_missing_mean - current_visible_mean
    physical_low = _dec(study['physical_minimum'])
    physical_high = _dec(study['physical_maximum'])
    clipped = min(max(required_missing_mean, physical_low), physical_high)
    reachable = clipped == required_missing_mean
    return {
        'estimable': True,
        'required_shared_offset': decimal_string(offset_needed),
        'required_missing_mean': decimal_string(required_missing_mean),
        'physically_reachable': reachable,
        'clipped_missing_mean': decimal_string(clipped),
        'interpretation': 'shared per-layer mean offset at which the full-grid mean reaches the alarm',
    }


def _ipw_section(db, study_id, model_id):
    from hangar_ingest.studies.missingness import get_model, weighted_mean
    model = get_model(db, study_id, model_id)
    metrics = model['metrics']
    if not metrics['support_ok']:
        return {
            'estimable': False,
            'reason': 'missing_overlap_support',
            'weight_summary': metrics['weights'],
            'note': 'No seemingly precise corrected mean is emitted when strata lack overlapping support.',
        }
    estimate = weighted_mean(db, study_id, model_id)
    return {
        'estimable': True,
        'model_id': model_id,
        'weighted_mean': estimate['weighted_mean'],
        'assumption': estimate['assumption'],
        'weight_summary': metrics['weights'],
    }


def bootstrap(db, study_id, cleaned, confidence=0.95):
    """Resample whole aircraft (clusters), never individual cells."""
    study = registry.get_study(db, study_id)
    fields = study['strata_fields']
    alarm = _dec(study['alarm_threshold'])
    cells = db.all('SELECT * FROM study_cells WHERE study_id=?', (study_id,))
    aircraft_of_device = {row['device_id']: row['aircraft'] for row in registry.device_windows(db, study_id)}
    fleet = sorted(set(aircraft_of_device.values()))
    by_aircraft = {aircraft: [cell for cell in cells if aircraft_of_device[cell['device_id']] == aircraft]
                   for aircraft in fleet}
    rng = random.Random(study['seed'])
    samples = study['bootstrap_samples']
    means = []
    visible_means = []
    for _ in range(samples):
        resampled = []
        for _aircraft in fleet:
            resampled.extend(by_aircraft[rng.choice(fleet)])
        mean, visible = _resample_mean(resampled, fields, cleaned, alarm)
        if mean is not None:
            means.append(mean)
        if visible is not None:
            visible_means.append(visible)
    return {
        'method': 'aircraft_cluster_bootstrap',
        'resamples': samples,
        'clusters': len(fleet),
        'seed': study['seed'],
        'full_grid_mean': _interval(means, confidence),
        'visible_mean': _interval(visible_means, confidence),
    }


def _resample_mean(cells, fields, cleaned, alarm):
    layers = {}
    for cell in cells:
        if cell['disposition'] == 'planned_outage':
            continue
        key = conditions.strata_key(cell, fields)
        layers.setdefault(key, []).append(cell)
    total = Decimal(0)
    count = 0
    visible_total = Decimal(0)
    visible_count = 0
    for key, members in layers.items():
        assumption = _assumption_for(cleaned, key, fields)
        visible = [_dec(cell['value']) for cell in members if cell['disposition'] == 'valid']
        missing_count = sum(1 for cell in members if cell['disposition'] in {'unexpected_missing', 'quality_excluded'})
        bound_low = _dec(assumption['minimum'])
        bound_high = _dec(assumption['maximum'])
        visible_mean = sum(visible) / len(visible) if visible else (bound_low + bound_high) / 2
        if visible:
            missing_mean = min(max(visible_mean + _dec(assumption['offset']), bound_low), bound_high)
        else:
            missing_mean = visible_mean
        total += visible_mean * len(visible) + missing_mean * missing_count
        visible_total += sum(visible)
        count += len(members)
        visible_count += len(visible)
    full = total / count if count else None
    naive = visible_total / visible_count if visible_count else None
    return full, naive


def _interval(values, confidence):
    if not values:
        return {'estimable': False}
    ordered = sorted(values)
    tail = (1 - confidence) / 2
    lower_index = int(tail * len(ordered))
    upper_index = min(len(ordered) - 1, int((1 - tail) * len(ordered)))
    return {
        'estimable': True,
        'point': decimal_string(sum(ordered) / len(ordered)),
        'lower': decimal_string(ordered[lower_index]),
        'upper': decimal_string(ordered[upper_index]),
        'confidence_level': decimal_string(confidence),
    }


def list_scenarios(db, study_id):
    registry.get_study(db, study_id)
    rows = db.all('SELECT * FROM study_scenarios WHERE study_id=? ORDER BY scenario_id', (study_id,))
    for row in rows:
        for field in ('assumptions', 'results'):
            row[field] = json.loads(row[field])
    return rows
