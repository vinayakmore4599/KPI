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

Trailing, leading, period-to-date, or full-period aggregate of a base measure.  
`role: platform` · `enabled: on`

```yaml
value_qtd:
  op: window
  of: sotif_value
  range: qtd
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

### `predicate`

1/0 flag when all (or any) measure predicates pass. Does not drop rows.  
`role: platform` · `enabled: on`

```yaml
healthy:
  op: predicate
  match: all
  predicates:
    - { of: total_profit, cmp: gt, value: 0 }
    - { of: return_rate, cmp: lt, value: 0.20 }
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

### `if_null`

Value when present, otherwise the fallback column.  
`role: addon` · `enabled: on`

```yaml
qty:
  columns: { value: ordered_qty, fallback: planned_qty }
  op: if_null
```

### `nullif`

Null where value equals the sentinel column.  
`role: addon` · `enabled: on`

```yaml
nonzero:
  columns: { value: amount, sentinel: offset_col }
  op: nullif
```

### `null_if_zero`

Null where the column is 0.  
`role: addon` · `enabled: on`

```yaml
rate:
  columns: [amount]
  op: null_if_zero
```

### `zero_if_null`

0 where the column is null.  
`role: addon` · `enabled: on`

```yaml
qty_or_zero:
  columns: [ordered_qty]
  op: zero_if_null
```

### `is_null`

1 where the column is null, else 0.  
`role: addon` · `enabled: on`

```yaml
missing:
  columns: [amount]
  op: is_null
```

### `is_not_null`

1 where the column is present, else 0.  
`role: addon` · `enabled: on`

```yaml
present:
  columns: [amount]
  op: is_not_null
```

### `if_else`

Then-column where cond is nonzero, otherwise other.  
`role: addon` · `enabled: on`

```yaml
picked:
  columns: { cond: flag, then: amount, other: fallback }
  op: if_else
```

### `round`

Round a numeric column. Optional decimals defaults to 0.  
`role: platform` · `enabled: on`

```yaml
rounded:
  columns: [amount]
  op: round
```

### `floor`

Floor of one numeric column.  
`role: platform` · `enabled: on`

```yaml
whole:
  columns: [amount]
  op: floor
```

### `ceil`

Ceiling of one numeric column.  
`role: platform` · `enabled: on`

```yaml
whole:
  columns: [amount]
  op: ceil
```

### `power`

base ** exp. Domain errors are null.  
`role: platform` · `enabled: on`

```yaml
squared:
  columns: { base: amount, exp: two }
  op: power
```

### `log`

Natural log. Non-positive is null.  
`role: platform` · `enabled: on`

```yaml
ln_amt:
  columns: [amount]
  op: log
```

### `log10`

Base-10 log. Non-positive is null.  
`role: platform` · `enabled: on`

```yaml
log_amt:
  columns: [amount]
  op: log10
```

### `sqrt`

Square root. Negative is null.  
`role: platform` · `enabled: on`

```yaml
root:
  columns: [amount]
  op: sqrt
```

### `date_diff`

end minus start in day, week, month, or year. Null in either side is null.  
`role: platform` · `enabled: on`

```yaml
gap:
  expr: "date_diff(prev_date, order_date, 'day')"
```

### `date_add`

Add n day/week/month/year units to a date.  
`role: platform` · `enabled: on`

```yaml
next:
  expr: "date_add(order_date, 1, 'month')"
```

### `epoch_day`

Integer days since 1970-01-01.  
`role: platform` · `enabled: on`

```yaml
epoch:
  columns: [order_date]
  op: epoch_day
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

### `coalesce`

First non-null measure scalar.  
`role: addon` · `enabled: on`

```yaml
picked:
  op: fn
  fn: coalesce
  inputs: [current_value, target]
```

### `if_null`

Value when present, otherwise the fallback measure.  
`role: addon` · `enabled: on`

```yaml
shown:
  op: fn
  fn: if_null
  inputs: [current_value, target]
```

### `nullif`

Null when value equals the sentinel measure.  
`role: addon` · `enabled: on`

```yaml
nonzero:
  op: fn
  fn: nullif
  inputs: [current_value, zero]
```

### `null_if_zero`

Null when the measure is 0.  
`role: addon` · `enabled: on`

```yaml
rate:
  op: fn
  fn: null_if_zero
  inputs: [current_value]
```

### `zero_if_null`

0 when the measure is null.  
`role: addon` · `enabled: on`

```yaml
shown:
  op: fn
  fn: zero_if_null
  inputs: [current_value]
```

### `is_null`

1 if the measure is null, else 0.  
`role: addon` · `enabled: on`

```yaml
missing:
  op: fn
  fn: is_null
  inputs: [current_value]
```

### `is_not_null`

1 if the measure is present, else 0.  
`role: addon` · `enabled: on`

```yaml
present:
  op: fn
  fn: is_not_null
  inputs: [current_value]
```

### `if_else`

Then-measure if cond is nonzero, otherwise other.  
`role: addon` · `enabled: on`

```yaml
picked:
  op: fn
  fn: if_else
  inputs: { cond: flag, then: current_value, other: target }
```

### `sign_label` (aliases: change_direction)

Positive / Negative / Neutral from a scalar. Null stays null; zero is Neutral.  
`role: platform` · `enabled: on`

```yaml
direction:
  op: fn
  fn: sign_label
  inputs: [yoy_month]
```

### `round`

Round a measure scalar. Optional decimals defaults to 0.  
`role: platform` · `enabled: on`

```yaml
rounded:
  op: fn
  fn: round
  inputs: [current_value]
```

### `floor`

Floor of a measure scalar.  
`role: platform` · `enabled: on`

```yaml
whole:
  op: fn
  fn: floor
  inputs: [current_value]
```

### `ceil`

Ceiling of a measure scalar.  
`role: platform` · `enabled: on`

```yaml
whole:
  op: fn
  fn: ceil
  inputs: [current_value]
```

### `power`

base ** exp. Domain errors are null.  
`role: platform` · `enabled: on`

```yaml
squared:
  op: fn
  fn: power
  inputs: [current_value, two]
```

### `log`

Natural log. Non-positive is null.  
`role: platform` · `enabled: on`

```yaml
ln_amt:
  op: fn
  fn: log
  inputs: [current_value]
```

### `log10`

Base-10 log. Non-positive is null.  
`role: platform` · `enabled: on`

```yaml
log_amt:
  op: fn
  fn: log10
  inputs: [current_value]
```

### `sqrt`

Square root. Negative is null.  
`role: platform` · `enabled: on`

```yaml
root:
  op: fn
  fn: sqrt
  inputs: [current_value]
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
