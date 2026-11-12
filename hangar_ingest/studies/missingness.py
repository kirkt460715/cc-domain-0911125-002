"""Observation-probability model for missingness under known operating conditions.

The estimand is the probability that an accountable grid cell (not a planned
outage) actually carries a valid observation, conditional on operating-condition
fields available at the study cutoff.  Estimation uses L2-regularized logistic
regression implemented with the standard library only.

Validation leaves whole aircraft out at a time, so an aircraft whose conditions
the model never saw cannot leak into its own evaluation.  Overlap (positivity)
is checked explicitly: when condition strata contain only observed or only
missing cells, no inverse-probability-weighted mean is produced.
"""
import json
import math
from decimal import Decimal

from hangar_ingest.types import canonical, decimal_string, fail, identity, number
from hangar_ingest.runs import journal
from hangar_ingest.studies import conditions, registry

MAX_ITERATIONS = 500
TOLERANCE = 1e-9


def _feature_values(cell, fields):
    strata = conditions.strata_of(cell)
    values = []
    for field in fields:
        value = strata.get(field)
        if value is None:
            fail('model_features', 'Cell is missing a declared condition field', cell_index=cell['cell_index'])
        values.append(value)
    return values


def _encode(cells, fields):
    """Deterministic one-hot/standardized encoding built only from training cells."""
    columns = [[] for _ in fields]
    numeric = [True] * len(fields)
    for cell in cells:
        for index, value in enumerate(_feature_values(cell, fields)):
            if isinstance(value, str):
                numeric[index] = False
            columns[index].append(value)
    categories = []
    means = []
    scales = []
    for index, field in enumerate(fields):
        if numeric[index]:
            nums = [float(value) for value in columns[index]]
            mean = sum(nums) / len(nums)
            spread = max(max(nums) - min(nums), 1e-12)
            means.append(mean)
            scales.append(spread)
            categories.append(None)
        else:
            levels = sorted(set(columns[index]))
            categories.append(levels)
            means.append(None)
            scales.append(None)

    def row(cell):
        vector = [1.0]
        values = _feature_values(cell, fields)
        for index, value in enumerate(values):
            if numeric[index]:
                vector.append((float(value) - means[index]) / scales[index])
            else:
                for level in categories[index][1:]:  # first level is the reference
                    vector.append(1.0 if value == level else 0.0)
        return vector

    width = len(row(cells[0])) if cells else 1
    return row, width, {'numeric': numeric, 'means': means, 'scales': scales, 'categories': categories}


def _sigmoid(z):
    if z >= 0:
        exponential = math.exp(-z)
        return 1.0 / (1.0 + exponential)
    exponential = math.exp(z)
    return exponential / (1.0 + exponential)


def fit_logistic(rows, targets, *, width, penalty=1.0):
    """L2-regularized maximum likelihood via Newton-Raphson (IRLS)."""
    number(penalty, 'penalty')
    if penalty < 0:
        fail('model_penalty', 'Regularization strength must be nonnegative')
    beta = [0.0] * width
    count = len(rows)
    for _ in range(MAX_ITERATIONS):
        scores = [sum(beta[j] * row[j] for j in range(width)) for row in rows]
        probabilities = [_sigmoid(z) for z in scores]
        gradient = [0.0] * width
        matrix = [[0.0] * width for _ in range(width)]
        for row, target, probability in zip(rows, targets, probabilities):
            residual = probability - target
            weight = max(probability * (1.0 - probability), 1e-12)
            for j in range(width):
                gradient[j] += residual * row[j]
                for k in range(width):
                    matrix[j][k] += weight * row[j] * row[k]
        for j in range(1, width):  # intercept is not penalized
            gradient[j] += penalty * beta[j]
            matrix[j][j] += penalty
        try:
            delta = _solve(matrix, [-g for g in gradient])
        except ValueError:
            fail('model_fit', 'Design matrix is singular; reduce condition fields or strengthen the penalty')
        beta = [b + d for b, d in zip(beta, delta)]
        if max(abs(d) for d in delta) < TOLERANCE:
            break
    return beta


def _solve(matrix, vector):
    n = len(vector)
    copy = [row[:] for row in matrix]
    rhs = vector[:]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(copy[r][col]))
        if abs(copy[pivot][col]) < 1e-14:
            raise ValueError('singular matrix')
        copy[col], copy[pivot] = copy[pivot], copy[col]
        rhs[col], rhs[pivot] = rhs[pivot], rhs[col]
        pivot_value = copy[col][col]
        for j in range(col, n):
            copy[col][j] /= pivot_value
        rhs[col] /= pivot_value
        for r in range(n):
            if r == col:
                continue
            factor = copy[r][col]
            if factor == 0.0:
                continue
            for j in range(col, n):
                copy[r][j] -= factor * copy[col][j]
            rhs[r] -= factor * rhs[col]
    return rhs


def _brier(pairs):
    if not pairs:
        return None
    return sum((probability - target) ** 2 for probability, target in pairs) / len(pairs)


def _calibration_bins(pairs, bins=5):
    boundaries = [index / bins for index in range(bins + 1)]
    result = []
    for index in range(bins):
        members = [(p, y) for p, y in pairs if boundaries[index] <= p < boundaries[index + 1]
                   or (index == bins - 1 and p == boundaries[index + 1])]
        if not members:
            result.append({'bin': index + 1, 'range': [boundaries[index], boundaries[index + 1]], 'count': 0,
                           'mean_predicted': None, 'observed_rate': None})
            continue
        mean_predicted = sum(p for p, _ in members) / len(members)
        observed = sum(y for _, y in members) / len(members)
        result.append({'bin': index + 1, 'range': [boundaries[index], boundaries[index + 1]], 'count': len(members),
                       'mean_predicted': _rounded(mean_predicted), 'observed_rate': _rounded(observed)})
    return result


def _rounded(value):
    if value is None:
        return None
    return float(Decimal(number(value, 'value')).quantize(Decimal('0.000001')))


def _accountable_cells(db, study_id):
    return [cell for cell in db.all(
        'SELECT * FROM study_cells WHERE study_id=? ORDER BY cell_index', (study_id,))
        if cell['disposition'] != 'planned_outage']


def fit_model(db, study_id, *, model_id, penalty='1.0'):
    study = registry.require_state(db, study_id, {'gridded', 'modeled', 'scenario'})
    identity(model_id, 'model_id')
    if db.one('SELECT 1 FROM study_models WHERE study_id=? AND model_id=?', (study_id, model_id)):
        fail('model_conflict', 'Model identifier already exists for this study')
    fields = study['strata_fields']
    if not fields:
        fail('model_features', 'A probability model needs declared stratification fields')
    cells = _accountable_cells(db, study_id)
    if len(cells) < 4:
        fail('model_samples', 'Too few accountable cells to fit a probability model')
    targets = {cell['cell_index']: 1 if cell['disposition'] == 'valid' else 0 for cell in cells}
    aircraft = {cell['device_id']: cell['aircraft'] for cell in cells}
    fleet = sorted(set(aircraft.values()))
    if len(fleet) < 2:
        fail('model_validation', 'Aircraft-held-out validation needs at least two aircraft')

    # Overlap/positivity diagnostics on the full cohort.  Support is evaluated per
    # device-stratum combination, which is the unit at which observation probability
    # must stay strictly between zero and one for inverse-probability weighting.
    strata_groups = {}
    for cell in cells:
        key = (cell['device_id'], *_feature_values(cell, fields))
        strata_groups.setdefault(key, []).append(targets[cell['cell_index']])
    positivity = []
    support_ok = True
    for key, group in sorted(strata_groups.items(), key=lambda item: canonical(list(item[0]))):
        observed = sum(group)
        missing = len(group) - observed
        ok = observed > 0 and missing > 0
        support_ok = support_ok and ok
        positivity.append({'device_id': key[0], 'strata': dict(zip(fields, key[1:])),
                           'cells': len(group), 'observed': observed,
                           'missing': missing, 'overlap': ok})

    fold_metrics = []
    held_out_pairs = []
    fold_coefficients = []
    for held_aircraft in fleet:
        training = [cell for cell in cells if aircraft[cell['device_id']] != held_aircraft]
        evaluation = [cell for cell in cells if aircraft[cell['device_id']] == held_aircraft]
        encoder, width, encoding = _encode(training, fields)
        train_rows = [encoder(cell) for cell in training]
        train_targets = [targets[cell['cell_index']] for cell in training]
        beta = fit_logistic(train_rows, train_targets, width=width, penalty=float(number(penalty, 'penalty')))
        fold_coefficients.append(beta)
        pairs = []
        for cell in evaluation:
            probability = _sigmoid(sum(b * x for b, x in zip(beta, encoder(cell))))
            pairs.append((probability, targets[cell['cell_index']]))
        held_out_pairs.extend(pairs)
        fold_metrics.append({
            'held_out_aircraft': held_aircraft,
            'training_cells': len(training),
            'held_out_cells': len(evaluation),
            'brier': _rounded(_brier(pairs)),
            'log_loss': _rounded(_log_loss(pairs)),
        })

    full_encoder, width, encoding = _encode(cells, fields)
    full_rows = [full_encoder(cell) for cell in cells]
    full_beta = fit_logistic(full_rows, [targets[cell['cell_index']] for cell in cells],
                             width=width, penalty=float(number(penalty, 'penalty')))
    probabilities = {
        cell['cell_index']: _sigmoid(sum(b * x for b, x in zip(full_beta, full_encoder(cell))))
        for cell in cells
    }
    weights, weight_summary = _weights(cells, targets, probabilities, support_ok)
    metrics = {
        'method': 'l2_logistic_regression',
        'penalty': decimal_string(number(penalty, 'penalty')),
        'features': ['intercept', *fields],
        'encoding': encoding,
        'aircraft_held_out_folds': fold_metrics,
        'pooled_held_out_brier': _rounded(_brier(held_out_pairs)),
        'pooled_held_out_log_loss': _rounded(_log_loss(held_out_pairs)),
        'calibration_bins_held_out': _calibration_bins(held_out_pairs),
        'overlap_support': positivity,
        'support_ok': support_ok,
        'weights': weight_summary,
        'training_cells': len(cells),
        'observed_cells': sum(targets.values()),
        'missing_cells': len(cells) - sum(targets.values()),
    }
    with db.transaction():
        db.execute(
            'INSERT INTO study_models(study_id,model_id,method,regularization,features,folds,coefficients,metrics,'
            'support_ok,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (study_id, model_id, 'l2_logistic_regression', decimal_string(number(penalty, 'penalty')),
             canonical(metrics['features']), len(fleet), canonical(full_beta), canonical(metrics),
             int(support_ok), db.clock()),
        )
        registry.advance_state(db, study_id, 'modeled')
        journal.record(db, 'study.model_fitted', {
            'study_id': study_id, 'model_id': model_id, 'support_ok': support_ok,
            'folds': len(fleet), 'effective_sample_size': weight_summary['effective_sample_size'],
        })
    return {'model_id': model_id, **metrics, 'coefficients': [_rounded(value) for value in full_beta],
            'fold_coefficients': [[_rounded(value) for value in beta] for beta in fold_coefficients],
            'probabilities': {str(index): _rounded(probability) for index, probability in sorted(probabilities.items())}}


def _log_loss(pairs):
    if not pairs:
        return None
    total = 0.0
    for probability, target in pairs:
        clipped = min(max(probability, 1e-12), 1 - 1e-12)
        total += -(target * math.log(clipped) + (1 - target) * math.log(1 - clipped))
    return total / len(pairs)


def _weights(cells, targets, probabilities, support_ok):
    """Inverse-probability weights for observed cells; IPW only valid with support."""
    observed_weights = []
    per_cell = {}
    for cell in cells:
        if targets[cell['cell_index']] != 1:
            continue
        probability = min(max(probabilities[cell['cell_index']], 1e-6), 1.0)
        weight = 1.0 / probability
        observed_weights.append(weight)
        per_cell[cell['cell_index']] = _rounded(weight)
    if not observed_weights:
        return per_cell, {'count': 0, 'maximum': None, 'minimum': None, 'mean': None,
                          'effective_sample_size': 0, 'design_effect': None, 'extreme_weight_ratio': None,
                          'ipw_estimable': False}
    mean = sum(observed_weights) / len(observed_weights)
    ess = (sum(observed_weights) ** 2) / sum(w * w for w in observed_weights)
    ordered = sorted(observed_weights)
    summary = {
        'count': len(observed_weights),
        'maximum': _rounded(ordered[-1]),
        'minimum': _rounded(ordered[0]),
        'mean': _rounded(mean),
        'effective_sample_size': _rounded(ess),
        'design_effect': _rounded(len(observed_weights) / ess),
        'extreme_weight_ratio': _rounded(ordered[-1] / ordered[0]),
        'ipw_estimable': support_ok,
        'reason': None if support_ok else 'strata_without_overlapping_support',
    }
    return per_cell, summary


def get_model(db, study_id, model_id):
    identity(model_id, 'model_id')
    row = db.one('SELECT * FROM study_models WHERE study_id=? AND model_id=?', (study_id, model_id))
    if row is None:
        fail('unknown_model', 'Study model does not exist')
    for field in ('features', 'coefficients', 'metrics'):
        row[field] = json.loads(row[field])
    return row


def weighted_mean(db, study_id, model_id):
    """Inverse-probability-weighted visible mean under complete-case MAR assumptions.

    Emitted only when every device-stratum has overlapping support; otherwise the
    caller receives an explicit non-estimable result instead of a precise number.
    """
    model = get_model(db, study_id, model_id)
    metrics = model['metrics']
    if not metrics['support_ok']:
        return {
            'study_id': study_id, 'model_id': model_id, 'estimable': False,
            'reason': 'missing_overlap_support',
            'unsupported_strata': [row for row in metrics['overlap_support'] if not row['overlap']],
            'note': 'No corrected mean is produced when some strata lack overlapping support.',
        }
    fields = registry.get_study(db, study_id)['strata_fields']
    cells = [cell for cell in db.all('SELECT * FROM study_cells WHERE study_id=? ORDER BY cell_index', (study_id,))
             if cell['disposition'] == 'valid']
    encoder, _width, _encoding = _encoder_from_metrics(metrics, cells, fields)
    beta = model['coefficients']
    weighted_sum = 0.0
    weight_sum = 0.0
    per_cell = []
    for cell in cells:
        probability = _sigmoid(sum(b * x for b, x in zip(beta, encoder(cell))))
        probability = min(max(probability, 1e-6), 1.0)
        weight = 1.0 / probability
        value = float(cell['value'])
        weighted_sum += weight * value
        weight_sum += weight
        per_cell.append({'cell_index': cell['cell_index'], 'device_id': cell['device_id'],
                         'observation_probability': _rounded(probability), 'weight': _rounded(weight)})
    return {
        'study_id': study_id, 'model_id': model_id, 'estimable': True,
        'method': 'inverse_probability_weighted_complete_case',
        'weighted_mean': _rounded(weighted_sum / weight_sum),
        'weight_summary': metrics['weights'],
        'cells': per_cell,
        'assumption': 'missing_at_random_conditional_on_observed_condition_fields',
    }


def _encoder_from_metrics(metrics, cells, fields):
    """Rebuild the deterministic encoder using the stored training encoding."""
    encoding = metrics['encoding']
    means = encoding['means']
    scales = encoding['scales']
    categories = encoding['categories']

    def row(cell):
        values = _feature_values(cell, fields)
        vector = [1.0]
        for index, value in enumerate(values):
            if encoding['numeric'][index]:
                vector.append((float(value) - means[index]) / scales[index])
            else:
                for level in categories[index][1:]:
                    vector.append(1.0 if value == level else 0.0)
        return vector

    return row, len(row(cells[0])), encoding
