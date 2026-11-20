# gb-0041 测试说明

## 备件反演：数学

```bash
python -m pytest test-artifacts/gb-0041/test_spares_math.py -q
```

- `test_endpoints_match_bruteforce`：独立枚举整件构成，与整数规划结论对照。
- `test_random_small_cases_match_bruteforce`：12 个固定种子随机小例，可行/无解与极值均须与穷举一致。
- `test_constituents_reproduce_counts_and_rates`：发布的构成必须行合计=真类件数、列合计=给定判读件数、逐格比例在带内。
- `test_impossible_zero_count_explains_itself` / `test_integer_gap_explains_itself`：无解必须给出可读原因。
- `test_input_validation`：非整数矩阵、重复故障、负件数、越界偏差等被拒绝。

## 备件反演：版本与并发

```bash
python -m pytest test-artifacts/gb-0041/test_spares_flow.py -q
```

覆盖：仅有已完成研究可冻结；同版两端齐备前不可见；更正判读件数另开新版且旧版晚到结果标 `superseded`；同端重跑不重复发布（审计计数不变）；中断恢复使用该版冻结资料；6 个并发更正得到 1–6 唯一版本号；非法输入 422、已发布版本重跑 409；无解版本两端均带原因。

## 口径说明

整数规划求解后由 `spares_math._verify` 用 `Fraction` 精确复核；测试中的穷举 oracle 与业务代码没有共享实现。
