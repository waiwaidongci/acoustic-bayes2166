"""整件反演：在折外混淆矩阵的允许偏差带内，求指定真类件数的最少/最多构成。

每个版本冻结 k×k 折外混淆矩阵、新批“判读件数”向量 counts 与允许偏差（百分点）。
对真类 i 的判读去向比例 p_ij 以矩阵行归一值为基准，允许在 ±tol 内浮动，
且同一真类各去向比例之和仍为 100%。

整件构成是整数矩阵 A（真类×判读去向）与真类件数 n：
  Σ_j A[i][j] = n[i]；Σ_i A[i][j] = counts[j]；n[i]>0 时 lower[i][j] ≤ A[i][j]/n[i] ≤ upper[i][j]。
目标为最小/最大化 n[target]。求解后以精确有理数逐项复核，保证构成能复原判读计数。
"""
import math
from decimal import Decimal
from fractions import Fraction

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

_MAX_TOTAL = 1_000_000
_MAX_CELL = 1_000_000


class SparesSolveError(RuntimeError):
    """求解器未能给出可复核结论（超时/数值异常），不是业务无解。"""


def parse_tolerance(value):
    try:
        dec = Decimal(str(value))
    except Exception:
        raise ValueError('允许偏差必须为数值')
    if not dec.is_finite():
        raise ValueError('允许偏差必须为有限数值')
    if dec < 0 or dec > 100:
        raise ValueError('允许偏差需在0至100个百分点之间')
    return dec


def canonical_tolerance(dec):
    return format(dec.normalize(), 'f')


def prepare(labels, matrix, counts, tolerance):
    """校验冻结输入并构造精确比例带。tolerance 为 Decimal 或字符串。"""
    if not labels or len(labels) > 12:
        raise ValueError('故障类型数需在1至12之间')
    k = len(labels)
    if len(set(labels)) != k:
        raise ValueError('故障类型不能重复')
    if not isinstance(matrix, list) or len(matrix) != k:
        raise ValueError('混淆矩阵行数与故障类型不一致')
    rows = []
    for i, raw in enumerate(matrix):
        if not isinstance(raw, list) or len(raw) != k:
            raise ValueError('混淆矩阵列数与故障类型不一致')
        vals = []
        for v in raw:
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError('混淆矩阵必须是非负整数')
            vals.append(v)
        if sum(vals) <= 0:
            raise ValueError('真类“%s”在混淆矩阵中没有判读记录' % labels[i])
        rows.append(vals)
    if not isinstance(counts, list) or len(counts) != k:
        raise ValueError('判读件数需按故障类型逐一给出')
    clean = []
    for v in counts:
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise ValueError('判读件数必须是非负整数')
        if v > _MAX_CELL:
            raise ValueError('单个判读件数不能超过%d' % _MAX_CELL)
        clean.append(v)
    total = sum(clean)
    if total < 1:
        raise ValueError('判读件数合计至少为1件')
    if total > _MAX_TOTAL:
        raise ValueError('判读件数合计不能超过%d' % _MAX_TOTAL)
    if isinstance(tolerance, Decimal):
        dec = tolerance
    else:
        dec = parse_tolerance(tolerance)
    band = Fraction(dec) / 100
    lower, upper, baseline = [], [], []
    for vals in rows:
        row_total = sum(vals)
        base = [Fraction(v, row_total) for v in vals]
        baseline.append(base)
        lower.append([max(Fraction(0), x - band) for x in base])
        upper.append([min(Fraction(1), x + band) for x in base])
    return {'labels': list(labels), 'matrix': [list(r) for r in rows], 'counts': clean,
            'tolerance': dec, 'baseline': baseline, 'lower': lower, 'upper': upper}


def _build(model):
    lower, upper, counts = model['lower'], model['upper'], model['counts']
    k = len(counts)
    total = sum(counts)
    nv = k * k + k
    rows, lb, ub = [], [], []

    def add(row, lo, hi):
        rows.append(row); lb.append(lo); ub.append(hi)

    for i in range(k):
        row = np.zeros(nv)
        for j in range(k):
            row[i * k + j] = 1
        row[k * k + i] = -1
        add(row, 0.0, 0.0)
    for j in range(k):
        row = np.zeros(nv)
        for i in range(k):
            row[i * k + j] = 1
        add(row, float(counts[j]), float(counts[j]))
    for i in range(k):
        for j in range(k):
            lo, hi = lower[i][j], upper[i][j]
            row = np.zeros(nv)
            row[i * k + j] = float(lo.denominator)
            row[k * k + i] = -float(lo.numerator)
            add(row, 0.0, np.inf)
            row = np.zeros(nv)
            row[i * k + j] = float(hi.denominator)
            row[k * k + i] = -float(hi.numerator)
            add(row, -np.inf, 0.0)
    A = np.array(rows, float)
    upper_bounds = np.array([float(min(counts[j], total)) for i in range(k) for j in range(k)] + [float(total)] * k)
    return A, np.array(lb, float), np.array(ub, float), upper_bounds, nv, total


def _verify(model, allocation, totals, target_index):
    k = len(model['labels'])
    if len(allocation) != k or len(totals) != k:
        return '构成维度不正确'
    for i in range(k):
        if sum(allocation[i]) != totals[i]:
            return '第%d个真类的行合计与件数不一致' % (i + 1)
        if totals[i] < 0:
            return '真类件数不可为负'
        if totals[i] == 0 and any(allocation[i]):
            return '零件真类仍分配了判读去向'
        if totals[i] > 0:
            for j in range(k):
                rate = Fraction(allocation[i][j], totals[i])
                if rate < model['lower'][i][j] or rate > model['upper'][i][j]:
                    return '第%d个真类到第%d个去向的比例越出允许偏差' % (i + 1, j + 1)
    for j in range(k):
        if sum(allocation[i][j] for i in range(k)) != model['counts'][j]:
            return '第%d个判读去向的列合计与判读件数不一致' % (j + 1)
    if sum(totals) != sum(model['counts']):
        return '真类件数合计与判读件数合计不一致'
    return ''


def _format_result(model, sense, allocation, totals, target_index):
    k = len(model['labels'])
    total = sum(model['counts'])
    rates = [[float(Fraction(allocation[i][j], totals[i])) if totals[i] else 0.0 for j in range(k)] for i in range(k)]
    return {
        'end': sense,
        'feasible': True,
        'target_index': target_index,
        'target_label': model['labels'][target_index],
        'target_count': int(totals[target_index]),
        'total': total,
        'share': float(Fraction(int(totals[target_index]), total)),
        'labels': model['labels'],
        'counts': model['counts'],
        'true_totals': [int(x) for x in totals],
        'allocation': [[int(x) for x in row] for row in allocation],
        'predicted_totals': [int(sum(allocation[i][j] for i in range(k))) for j in range(k)],
        'rates': rates,
        'lower': [[float(x) for x in row] for row in model['lower']],
        'upper': [[float(x) for x in row] for row in model['upper']],
        'reproduces_counts': True,
        'explanation': '每行合计等于该真类件数，每列合计等于给定判读件数，所有行内比例均落在允许偏差带内；以上由精确有理数复核，可复原判读计数。',
    }


def _pct(value):
    return format(float(value * 100), '.4f').rstrip('0').rstrip('.')


def _lp(model):
    A, lb, ub, upper_bounds, nv, total = _build(model)
    bounds = Bounds(np.zeros(nv), upper_bounds)
    k = len(model['counts'])
    relax = np.zeros(nv)

    def run(c):
        return milp(c, integrality=relax, bounds=bounds, constraints=LinearConstraint(A, lb, ub),
                    options={'time_limit': 30})

    base = run(np.zeros(nv))
    if base.status == 2:
        return {'feasible': False}
    if base.status != 0 or base.x is None:
        return {'feasible': True, 'intervals': [], 'sample': None}
    intervals = []
    for i in range(k):
        c = np.zeros(nv); c[k * k + i] = 1.0
        lo = run(c); hi = run(-c)
        intervals.append((lo.fun if lo.status == 0 else None,
                          -hi.fun if hi.status == 0 else None))
    return {'feasible': True, 'intervals': intervals, 'sample': base.x[k * k:]}


def diagnose(model, target_index):
    """给出无解的可读原因；用于两端的无解结果。"""
    labels, lower, upper, counts = model['labels'], model['lower'], model['upper'], model['counts']
    k = len(labels)
    reasons = []
    for i in range(k):
        low_sum = sum(lower[i]); high_sum = sum(upper[i])
        if low_sum > 1:
            reasons.append('真类“%s”在允许偏差内各判读去向的比例下限之和为%s%%，超过100%%，仅靠浮动无法构成该真类的完整去向。' % (labels[i], _pct(low_sum)))
        if high_sum < 1:
            reasons.append('真类“%s”在允许偏差内各判读去向的比例上限之和为%s%%，不足100%%，无法把该真类的判读去向补满。' % (labels[i], _pct(high_sum)))
    if reasons:
        return reasons
    for j in range(k):
        forced = [i for i in range(k) if lower[i][j] > 0]
        if counts[j] == 0 and len(forced) == k:
            reasons.append('判读为“%s”的件数是0，而每一种真类在该去向的比例下限都大于0；任何真类只要存在就必然判读到“%s”，与0件冲突。' % (labels[j], labels[j]))
    if reasons:
        return reasons
    lp = _lp(model)
    if not lp['feasible']:
        return ['在允许偏差内连分数件数的构成也不存在：给定判读件数%s（合计%d件）与折外比例带相互冲突。' % ('/'.join(str(c) for c in counts), sum(counts))]
    gaps = []
    for i, (lo, hi) in enumerate(lp['intervals']):
        if lo is None or hi is None:
            continue
        left = math.ceil(lo - 1e-9); right = math.floor(hi + 1e-9)
        if left > right:
            gaps.append('真类“%s”的分数可行件数区间为[%.4f, %.4f]，区间内没有整数，而整件构成要求件数为整数。' % (labels[i], lo, hi))
    if gaps:
        return gaps
    if lp['sample'] is None:
        return ['按整件取整后无法同时满足行合计、列合计与允许偏差；可放宽允许偏差或核对判读件数后另开一版。']
    sample = '、'.join('%s≈%.3f' % (labels[i], x) for i, x in enumerate(lp['sample']))
    return ['比例带允许分数件数的构成（真类件数%s），但按整件取整后无法同时满足行合计、列合计与允许偏差。可放宽允许偏差或核对判读件数后另开一版。' % sample]


def solve_end(model, target_index, sense, time_limit=60):
    """sense: 'min' 或 'max'。返回可复原判读计数的构成，或无解原因；求解异常抛 SparesSolveError。"""
    if sense not in ('min', 'max'):
        raise ValueError('未知的极值方向')
    A, lb, ub, upper_bounds, nv, total = _build(model)
    k = len(model['counts'])
    c = np.zeros(nv)
    c[k * k + target_index] = 1.0 if sense == 'min' else -1.0
    result = milp(c, integrality=np.ones(nv), bounds=Bounds(np.zeros(nv), upper_bounds),
                  constraints=LinearConstraint(A, lb, ub),
                  options={'time_limit': time_limit, 'mip_rel_gap': 0.0})
    if result.status == 2:
        return {'end': sense, 'feasible': False, 'target_index': target_index,
                'target_label': model['labels'][target_index], 'labels': list(model['labels']),
                'counts': list(model['counts']), 'reason': '；'.join(diagnose(model, target_index))}
    if result.status != 0 or result.x is None:
        raise SparesSolveError('求解未能在%d秒内证明该端的整件极值（%s）' % (time_limit, result.message))
    allocation = [[int(round(result.x[i * k + j])) for j in range(k)] for i in range(k)]
    totals = [int(round(result.x[k * k + i])) for i in range(k)]
    problem = _verify(model, allocation, totals, target_index)
    if problem:
        raise SparesSolveError('求解结果未通过精确整件复核（%s）' % problem)
    return _format_result(model, sense, allocation, totals, target_index)


def solve_both(model, target_index, time_limit=60):
    return {'min': solve_end(model, target_index, 'min', time_limit),
            'max': solve_end(model, target_index, 'max', time_limit)}
