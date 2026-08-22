# Capability catalog

Generated from `udfs/kpi_engine/registries/`. Do not hand-edit.

This catalog covers column functions, measure functions, measure op kinds, and hooks.
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

## Hooks (`measures.hook`)

_None registered._
