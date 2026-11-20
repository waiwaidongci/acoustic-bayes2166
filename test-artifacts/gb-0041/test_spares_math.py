import math
import sys
from fractions import Fraction
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from src.services import spares_math as sm  # noqa: E402


def compositions(total, parts):
    if parts == 1:
        yield (total,)
        return
    for first in range(total + 1):
        for rest in compositions(total - first, parts - 1):
            yield (first,) + rest


def brute(model, target):
    """独立整件枚举：穷举真类件数与每行去向，确认求解器结论。"""
    k = len(model['labels'])
    counts = model['counts']
    found = []
    for n in compositions(sum(counts), k):
        def walk(i, remaining):
            if i == k:
                return all(v == 0 for v in remaining)
            low = [math.ceil(model['lower'][i][j] * n[i]) for j in range(k)]
            high = [math.floor(model['upper'][i][j] * n[i] + 1e-12) for j in range(k)]
            for row in compositions(n[i], k):
                if any(row[j] < low[j] or row[j] > high[j] for j in range(k)):
                    continue
                if any(row[j] > remaining[j] for j in range(k)):
                    continue
                if walk(i + 1, [remaining[j] - row[j] for j in range(k)]):
                    return True
            return False
        if walk(0, counts[:]):
            found.append(n[target])
    return (min(found), max(found)) if found else None


def test_endpoints_match_bruteforce():
    matrix = [[7, 5, 3], [2, 9, 2], [6, 1, 2]]
    model = sm.prepare(['正常', '轴承磨损', '转子偏心'], matrix, [4, 6, 2], '10')
    expect = brute(model, 1)
    assert expect is not None and expect[0] != expect[1]
    assert sm.solve_end(model, 1, 'min')['target_count'] == expect[0]
    assert sm.solve_end(model, 1, 'max')['target_count'] == expect[1]


def test_tolerance_zero_gives_point_estimate_when_integral():
    model = sm.prepare(['A', 'B'], [[90, 10], [20, 80]], [91, 21], '0')
    assert brute(model, 0) is None
    assert not sm.solve_end(model, 0, 'min')['feasible']
    model = sm.prepare(['A', 'B'], [[90, 10], [20, 80]], [110, 20], '0')
    assert sm.solve_end(model, 0, 'min')['target_count'] == 120
    assert sm.solve_end(model, 0, 'max')['target_count'] == 120


def test_constituents_reproduce_counts_and_rates():
    model = sm.prepare(['A', 'B', 'C'], [[70, 20, 10], [5, 90, 5], [10, 10, 80]], [15, 19, 13], '4')
    for end in ('min', 'max'):
        result = sm.solve_end(model, 2, end)
        assert result['feasible']
        rows = result['allocation']
        assert [sum(r) for r in rows] == result['true_totals']
        assert [sum(r[j] for r in rows) for j in range(3)] == [15, 19, 13]
        for i in range(3):
            for j in range(3):
                rate = Fraction(rows[i][j], result['true_totals'][i])
                assert model['lower'][i][j] <= rate <= model['upper'][i][j]
        assert result['predicted_totals'] == result['counts']


def test_impossible_zero_count_explains_itself():
    model = sm.prepare(['A', 'B'], [[90, 10], [20, 80]], [100, 0], '5')
    result = sm.solve_end(model, 0, 'min')
    assert not result['feasible']
    assert '件数是0' in result['reason']


def test_integer_gap_explains_itself():
    model = sm.prepare(['A', 'B'], [[90, 10], [20, 80]], [90, 20], '0')
    result = sm.solve_end(model, 0, 'max')
    assert not result['feasible']
    assert '没有整数' in result['reason']
    model = sm.prepare(['A', 'B'], [[90, 10], [20, 80]], [91, 21], '0')
    result = sm.solve_end(model, 0, 'min')
    assert not result['feasible']
    assert '整件取整' in result['reason']


def test_full_tolerance_bounds_are_wide():
    model = sm.prepare(['A', 'B'], [[90, 10], [20, 80]], [5, 5], '100')
    assert sm.solve_end(model, 0, 'min')['target_count'] == 0
    assert sm.solve_end(model, 0, 'max')['target_count'] == 10


def test_input_validation():
    with pytest.raises(ValueError):
        sm.prepare(['A'], [[0]], [1], '5')
    with pytest.raises(ValueError):
        sm.prepare(['A', 'A'], [[1, 0], [0, 1]], [1, 1], '5')
    with pytest.raises(ValueError):
        sm.prepare(['A'], [[5]], [0], '5')
    with pytest.raises(ValueError):
        sm.prepare(['A'], [[5]], [-1], '5')
    with pytest.raises(ValueError):
        sm.prepare(['A'], [[5]], [1], '-1')
    with pytest.raises(ValueError):
        sm.prepare(['A'], [[5.5]], [1], '5')


@pytest.mark.parametrize('seed', range(12))
def test_random_small_cases_match_bruteforce(seed):
    import random
    rng = random.Random(seed)
    k = rng.choice([2, 3])
    matrix = [[rng.randint(0, 6) for _ in range(k)] for _ in range(k)]
    for i in range(k):
        if sum(matrix[i]) == 0:
            matrix[i][rng.randrange(k)] = 2
    total = rng.randint(1, 10)
    counts = [0] * k
    for _ in range(total):
        counts[rng.randrange(k)] += 1
    tolerance = rng.choice(['0', '1', '2.5', '5', '10'])
    model = sm.prepare([f'L{i}' for i in range(k)], matrix, counts, tolerance)
    expect = brute(model, 0)
    low = sm.solve_end(model, 0, 'min')
    high = sm.solve_end(model, 0, 'max')
    if expect is None:
        assert not low['feasible'] and not high['feasible']
        assert low['reason']
    else:
        assert (low['target_count'], high['target_count']) == expect
