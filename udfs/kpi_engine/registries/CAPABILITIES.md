# Capability catalog

Generated from `udfs/kpi_engine/registries/`. Do not hand-edit.

This catalog covers column functions, measure functions, measure op kinds, and hooks.
A new name is implemented under `capabilities/` and allowlisted here. Do not edit `core/`.
Filter operators, compose templates, time formats, and aggregations stay platform code.

## Measure ops (`measures.op`)

### `point`

Value at the selected period, optional calendar offset.  
`role: platform` · `enabled: on`

```yaml
previous_year_value:
  op: point
  of: sotif_value
  offset: { years: 1 }
```

### `window`

Trailing, leading, or cumulative aggregate of a base measure.  
`role: platform` · `enabled: on`

```yaml
value_3m:
  op: window
  of: sotif_value
  trailing: { months: 3 }
  inclusive: true
```

### `trend`

Fixed-length period array plus a shared axis for graphs.  
`role: platform` · `enabled: on`

```yaml
trend_12m:
  op: trend
  of: sotif_value
  trailing: { months: 12 }
```

### `arithmetic`

Combine other measures with a registered measure function (default divide).  
`role: platform` · `enabled: on`

```yaml
ratio:
  op: arithmetic
  of: [current_value, previous_year_value]
  fn: divide
```

### `fn`

Feed other measures' scalars into a named measure function.  
`role: platform` · `enabled: on`

```yaml
yoy:
  op: fn
  fn: growth_pct
  inputs: [current_value, previous_year_value]
```

### `expr`

Formula over other measures using + - * /.  
`role: platform` · `enabled: on`

```yaml
blended:
  op: expr
  expr: (current_value + previous_year_value) / 2
```

### `constant`

A literal number on every row.  
`role: platform` · `enabled: on`

```yaml
target:
  op: constant
  value: 95
```

### `dimension`

Echo a grouping field as a requestable measure key.  
`role: platform` · `enabled: on`

```yaml
region:
  op: dimension
```

### `hook`

Call an allowlisted Python hook for logic the catalog cannot express.  
`role: platform` · `enabled: on`

```yaml
custom:
  op: hook
  hook: seasonal_index
  of: sotif_value
```

### `rank`

Rank groups on a cut. Ties share a rank; the next rank skips.  
`role: platform` · `enabled: on`

```yaml
reason_rank:
  op: rank
  of: current_value
  order: desc
```

### `percent_of_total`

Share of all groups on this cut (source * 100 / partition sum).  
`role: platform` · `enabled: on`

```yaml
share:
  op: percent_of_total
  of: current_value
  partition_by: [region]
```

### `ntile`

Bucket groups into N tiles using RANK-style ties.  
`role: addon` · `enabled: on`

```yaml
value_quartile:
  op: ntile
  of: current_value
  tiles: 4
  order: desc
```

### `dense_rank`

Rank groups on a cut. Ties share a rank; the next rank does not skip.  
`role: addon` · `enabled: on`

```yaml
reason_dense_rank:
  op: dense_rank
  of: current_value
  order: desc
```

### `row_number`

Unique 1..n order on a cut. Nulls sort last.  
`role: addon` · `enabled: on`

```yaml
reason_row:
  op: row_number
  of: current_value
  order: desc
```

### `cumulative_share`

Running share of of (Pareto). Last row in desc order is 100.  
`role: addon` · `enabled: on`

```yaml
pareto:
  op: cumulative_share
  of: current_value
  order: desc
```

### `running_total`

Ordered running sum of of on the cut.  
`role: addon` · `enabled: on`

```yaml
running:
  op: running_total
  of: current_value
  order: desc
```

### `contribution`

Share of the cut's (of - vs) change. Who drove the movement.  
`role: addon` · `enabled: on`

```yaml
yoy_contrib:
  op: contribution
  of: current_value
  vs: previous_year_value
```

### `lag`

Value of a base, point, or window measure at offset before the anchor.  
`role: addon` · `enabled: on`

```yaml
value_3m_ly:
  op: lag
  of: value_3m
  offset: { years: 1 }
```

### `lead`

Value of a base, point, or window measure at offset after the anchor.  
`role: addon` · `enabled: on`

```yaml
next_month:
  op: lead
  of: current_value
  offset: { months: 1 }
```

### `index`

of / lagged of × 100. 100 means unchanged vs the offset period.  
`role: addon` · `enabled: on`

```yaml
volume_index:
  op: index
  of: current_value
  offset: { years: 1 }
```

### `vs_target`

Compare of to a target measure or literal (gap or percent).  
`role: addon` · `enabled: on`

```yaml
gap:
  op: vs_target
  of: current_value
  vs: target
  as: gap
```

### `threshold`

1 if of meets cmp vs a literal or measure, else 0.  
`role: addon` · `enabled: on`

```yaml
hit_sla:
  op: threshold
  of: current_value
  cmp: gte
  value: 95
```

### `percent_rank`

RANK-style percent rank on a cut, scaled 0-100. One group is 0.  
`role: addon` · `enabled: on`

```yaml
reason_pct_rank:
  op: percent_rank
  of: current_value
  order: desc
```

### `gap_to_leader`

of minus the partition max. The leader is 0.  
`role: addon` · `enabled: on`

```yaml
vs_best:
  op: gap_to_leader
  of: current_value
```

### `gap_to_avg`

of minus the partition mean.  
`role: addon` · `enabled: on`

```yaml
vs_typical:
  op: gap_to_avg
  of: current_value
```

### `zscore`

(of - mean) / sample stdev on the partition. Zero stdev is 0.  
`role: addon` · `enabled: on`

```yaml
reason_z:
  op: zscore
  of: current_value
```

### `running_avg`

Ordered running mean of of on the cut.  
`role: addon` · `enabled: on`

```yaml
running_mean:
  op: running_avg
  of: current_value
  order: desc
```

### `top_n`

1 if RANK() is <= n, else 0. Ties can produce more than n ones.  
`role: addon` · `enabled: on`

```yaml
top_reason:
  op: top_n
  of: current_value
  n: 3
  order: desc
```

### `diff`

of minus the same measure at offset before the anchor.  
`role: addon` · `enabled: on`

```yaml
vs_last_year:
  op: diff
  of: current_value
  offset: { years: 1 }
```

### `pct_change`

growth_pct(of, lagged of) at offset. Same scale as fn growth_pct.  
`role: addon` · `enabled: on`

```yaml
yoy:
  op: pct_change
  of: current_value
  offset: { years: 1 }
```

## Column functions (`base_measures.op`)

### `value` (aliases: identity)

Pass one retrieved column through unchanged.  
`role: platform` · `enabled: on`

```yaml
amount:
  columns: [amount]
  op: value
```

### `abs`

Absolute value of one numeric column.  
`role: platform` · `enabled: on`

```yaml
abs_delta:
  columns: [delta]
  op: abs
```

### `sum` (aliases: add)

Row-wise sum of two or more columns.  
`role: platform` · `enabled: on`

```yaml
total:
  columns: [a, b]
  op: sum
```

### `subtract` (aliases: sub)

Row-wise left-to-right subtraction.  
`role: platform` · `enabled: on`

```yaml
net:
  columns: [gross, returns]
  op: subtract
```

### `multiply` (aliases: mul, product)

Row-wise product of two or more columns.  
`role: platform` · `enabled: on`

```yaml
ontime_full:
  columns: [ontime, fullqty]
  op: multiply
```

### `divide` (aliases: div, ratio)

Row-wise numerator / denominator. Zero denominator is null.  
`role: platform` · `enabled: on`

```yaml
rate:
  columns: [shipped, ordered]
  op: divide
```

### `percent_of` (aliases: share)

Row-wise share of whole, scaled to 0-100.  
`role: platform` · `enabled: on`

```yaml
fill_pct:
  columns: [filled, ordered]
  op: percent_of
```

### `min`

Row-wise minimum of two or more columns.  
`role: platform` · `enabled: on`

```yaml
lower:
  columns: [a, b]
  op: min
```

### `max`

Row-wise maximum of two or more columns.  
`role: platform` · `enabled: on`

```yaml
upper:
  columns: [a, b]
  op: max
```

### `avg` (aliases: mean)

Row-wise mean of two or more columns.  
`role: platform` · `enabled: on`

```yaml
mid:
  columns: [a, b]
  op: avg
```

### `coalesce`

First non-null value on each row.  
`role: platform` · `enabled: on`

```yaml
picked:
  columns: [preferred, fallback]
  op: coalesce
```

## Measure functions (`measures.fn`)

### `growth_pct` (aliases: yoy, mom, percent_change)

Percent change between two scalars. Null or zero base yields null.  
`role: platform` · `enabled: on`

```yaml
yoy:
  op: fn
  fn: growth_pct
  inputs: [current_value, previous_year_value]
```

### `divide` (aliases: div, ratio)

Ratio of two scalars. Zero or null denominator is null.  
`role: platform` · `enabled: on`

```yaml
rate:
  op: fn
  fn: divide
  inputs: [shipped, ordered]
```

### `percent` (aliases: percent_of, share)

Share of whole, scaled to 0-100.  
`role: platform` · `enabled: on`

```yaml
share:
  op: fn
  fn: percent
  inputs: [part, whole]
```

### `sum` (aliases: add)

Sum of two or more measure scalars. Any null yields null.  
`role: platform` · `enabled: on`

```yaml
total:
  op: fn
  fn: sum
  inputs: [a, b]
```

### `subtract` (aliases: sub)

Left-to-right subtraction of measure scalars.  
`role: platform` · `enabled: on`

```yaml
net:
  op: fn
  fn: subtract
  inputs: [gross, returns]
```

### `multiply` (aliases: mul, product)

Product of two or more measure scalars.  
`role: platform` · `enabled: on`

```yaml
weighted:
  op: fn
  fn: multiply
  inputs: [qty, price]
```

### `min`

Minimum of the non-null measure scalars.  
`role: platform` · `enabled: on`

```yaml
lower:
  op: fn
  fn: min
  inputs: [a, b]
```

### `max`

Maximum of the non-null measure scalars.  
`role: platform` · `enabled: on`

```yaml
upper:
  op: fn
  fn: max
  inputs: [a, b]
```

### `avg` (aliases: mean)

Mean of the non-null measure scalars.  
`role: platform` · `enabled: on`

```yaml
mid:
  op: fn
  fn: avg
  inputs: [a, b]
```

### `abs`

Absolute value of one measure scalar.  
`role: addon` · `enabled: on`

```yaml
magnitude:
  op: fn
  fn: abs
  inputs: [gap]
```

### `clamp`

Clamp a scalar into [lo, hi].  
`role: addon` · `enabled: on`

```yaml
bounded:
  op: fn
  fn: clamp
  inputs: [current_value, floor, cap]
```

### `attainment`

actual / target × 100. Null or zero target is null.  
`role: addon` · `enabled: on`

```yaml
vs_goal:
  op: fn
  fn: attainment
  inputs: [current_value, target]
```

## Hooks (`measures.hook`)

### `seasonal_index`

Anchor vs the average of the same calendar month in prior years.  
`role: addon` · `enabled: on`

```yaml
seasonal:
  op: hook
  hook: seasonal_index
  of: sotif_value
  trailing: { months: 36 }
```

### `ewma`

Recency-weighted average of period values. Alpha = 2 / (N + 1).  
`role: addon` · `enabled: on`

```yaml
smoothed:
  op: hook
  hook: ewma
  of: sotif_value
  trailing: { months: 12 }
```

### `period_max`

Largest period value in the trailing window.  
`role: addon` · `enabled: on`

```yaml
best_month:
  op: hook
  hook: period_max
  of: sotif_value
  trailing: { months: 12 }
```

### `period_min`

Smallest period value in the trailing window.  
`role: addon` · `enabled: on`

```yaml
worst_month:
  op: hook
  hook: period_min
  of: sotif_value
  trailing: { months: 12 }
```

### `period_median`

Median of period values in the trailing window.  
`role: addon` · `enabled: on`

```yaml
typical_month:
  op: hook
  hook: period_median
  of: sotif_value
  trailing: { months: 12 }
```

### `period_avg`

Mean of period values in the trailing window.  
`role: addon` · `enabled: on`

```yaml
typical_level:
  op: hook
  hook: period_avg
  of: sotif_value
  trailing: { months: 12 }
```

### `period_sum`

Sum of period values in the trailing window.  
`role: addon` · `enabled: on`

```yaml
window_total:
  op: hook
  hook: period_sum
  of: sotif_value
  trailing: { months: 12 }
```

### `hit_rate`

Percent of observed periods whose value is >= value.  
`role: addon` · `enabled: on`

```yaml
months_on_sla:
  op: hook
  hook: hit_rate
  of: sotif_value
  trailing: { months: 12 }
  value: 95
```

### `streak`

Consecutive periods ending at the anchor whose value is >= value.  
`role: addon` · `enabled: on`

```yaml
sla_streak:
  op: hook
  hook: streak
  of: sotif_value
  trailing: { months: 12 }
  value: 95
```

### `period_stdev`

Sample standard deviation of period values in the trailing window.  
`role: addon` · `enabled: on`

```yaml
volatility:
  op: hook
  hook: period_stdev
  of: sotif_value
  trailing: { months: 12 }
```

### `period_var`

Sample variance of period values in the trailing window.  
`role: addon` · `enabled: on`

```yaml
variance:
  op: hook
  hook: period_var
  of: sotif_value
  trailing: { months: 12 }
```

### `period_cv`

Sample stdev / mean × 100 of period values in the trailing window.  
`role: addon` · `enabled: on`

```yaml
relative_vol:
  op: hook
  hook: period_cv
  of: sotif_value
  trailing: { months: 12 }
```

### `period_range`

Largest minus smallest period value in the trailing window.  
`role: addon` · `enabled: on`

```yaml
spread:
  op: hook
  hook: period_range
  of: sotif_value
  trailing: { months: 12 }
```

### `period_count`

Count of observed periods in the trailing window.  
`role: addon` · `enabled: on`

```yaml
months_seen:
  op: hook
  hook: period_count
  of: sotif_value
  trailing: { months: 12 }
```

### `miss_rate`

Percent of observed periods whose value is below value.  
`role: addon` · `enabled: on`

```yaml
months_off_sla:
  op: hook
  hook: miss_rate
  of: sotif_value
  trailing: { months: 12 }
  value: 95
```

### `miss_streak`

Consecutive periods ending at the anchor whose value is below value.  
`role: addon` · `enabled: on`

```yaml
off_sla_run:
  op: hook
  hook: miss_streak
  of: sotif_value
  trailing: { months: 12 }
  value: 95
```

### `longest_streak`

Longest run of periods >= value anywhere in the trailing window.  
`role: addon` · `enabled: on`

```yaml
best_run:
  op: hook
  hook: longest_streak
  of: sotif_value
  trailing: { months: 12 }
  value: 95
```

### `cagr`

Compound annual growth from first to last observed value (growth_pct scale).  
`role: addon` · `enabled: on`

```yaml
annualized:
  op: hook
  hook: cagr
  of: sotif_value
  trailing: { months: 36 }
```

### `slope`

Least-squares slope of value vs observed period index (0..n-1).  
`role: addon` · `enabled: on`

```yaml
trend_slope:
  op: hook
  hook: slope
  of: sotif_value
  trailing: { months: 12 }
```
