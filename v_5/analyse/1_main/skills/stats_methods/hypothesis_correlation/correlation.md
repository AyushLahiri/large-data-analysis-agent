# Correlation

Apply the main hypothesis_correlation guidance first. Python has `scipy.stats`, `numpy` and `pandas`.

## Choose the coefficient

| Variables | Coefficient | Function | Use when |
|---|---|---|---|
| Two continuous variables with a linear relationship | Pearson r | `scipy.stats.pearsonr(x, y)`; CI from `.confidence_interval(0.95)` | The relationship is roughly straight-line and no extreme points dominate |
| Continuous with outliers or skew, ordinal, or monotonic but curved | Spearman ρ | `scipy.stats.spearmanr(x, y)` | Ranks capture the relationship. This is the safer default for survey scales |
| Ordinal with many ties, or small n | Kendall τ-b | `scipy.stats.kendalltau(x, y)` | Many tied values, such as 5-point scales |
| Binary × continuous | Point-biserial r | `scipy.stats.pointbiserialr(b, x)` | It is equivalent to Pearson with the binary coded 0/1. Also report the mean difference |
| Nominal × continuous | Correlation ratio η | √(SS_between / SS_total) from the group means | Use it only as strength; it has no direction. Test it with the group comparison in tests |
| Nominal × nominal | Cramér's V | See tests | — |
| Two variables adjusting for others | Partial correlation | Regress x and y each on the covariates, then correlate the residuals | The user asks for an association "controlling for" something; regression is often clearer |

## Checks

- **Shape:** compute both Pearson and Spearman. If they differ materially, or Spearman is clearly larger, the relationship is non-linear or driven by outliers. Report Spearman and say so.
- **Range restriction:** a filter that narrows one variable's range weakens r. Mention it when the population was filtered on either variable.
- **Aggregation:** correlations between group averages, such as regions, are usually much stronger than individual-level correlations and cannot be read as individual relationships. This is the ecological fallacy.
- **Size:** Fisher intervals for Pearson need about n ≥ 25 (Bonett & Wright, 2000). Below that, still report r and flag the interval as rough.
- **Many correlations (a matrix):** adjust the p-values with Benjamini-Hochberg and report only the pairs the question needs.

## Intervals

- Pearson: use the `pearsonr` result's `confidence_interval`, which is the Fisher z interval.
- Spearman: use the Fisher z interval with Bonett-Wright standard error: `z = atanh(ρ)`, `SE = √((1 + ρ²/2) / (n − 3))`, bounds `tanh(z ± 1.96·SE)`.
- Kendall: `SE = √(0.437 / (n − 4))` on the same atanh scale, valid for |τ| < 0.8.

## Weighted correlation

- Weighted Pearson: `numpy.cov(x, y, aweights=w)` scaled by the weighted standard deviations. For its p-value and CI, fit `smf.wls('zy ~ zx', d, weights=w).fit(cov_type='HC1')` on standardised variables, clustering on the PSU if documented. The slope is the weighted correlation, approximately.
- Weighted Spearman or Kendall: no design-aware version is available. Report the unweighted coefficient and flag it.

## Interpret

- Sign: positive means the two rise together; negative means one rises as the other falls.
- Size: as a rough guide for social data, about 0.1 is small, 0.3 moderate and 0.5 large (Cohen, 1988). Say the guide is rough, and judge size in context.
- r² is the share of variation in one variable that moves linearly with the other. Use it only for Pearson, and only if it helps the user.
- Never call a correlation "proof" of an effect. Name an obvious third factor if one plausibly drives both variables.

Example wording: "Households with higher incomes tend to report higher energy spending. The correlation is moderate (Spearman ρ = 0.34, 95% CI 0.29 to 0.39). This shows the two move together, not that income causes higher spending."

## Table layout

| Variable 1 | Variable 2 | Coefficient | Estimate | 95% CI | p-value | n |
|---|---|---|---|---|---|---|

For several pairs, use one row per pair, sorted by the question's priority, not by size.

## Headline formulas for the methodology

- Pearson: `r = Σ(xᵢ − x̄)(yᵢ − ȳ) / √(Σ(xᵢ − x̄)² · Σ(yᵢ − ȳ)²)`.
- Spearman: Pearson r computed on the ranks R(xᵢ), R(yᵢ).
- Fisher interval: `tanh(atanh(r) ± z₀.₉₇₅ · SE)`, where SE = 1/√(n − 3) for Pearson.

References: [Bonett & Wright 2000](https://link.springer.com/article/10.1007/BF02294183) · [SciPy pearsonr](https://docs.scipy.org/doc/scipy-1.15.3/reference/generated/scipy.stats.pearsonr.html) · [SciPy spearmanr](https://docs.scipy.org/doc/scipy-1.15.3/reference/generated/scipy.stats.spearmanr.html) · [SciPy false discovery control](https://docs.scipy.org/doc/scipy-1.15.3/reference/generated/scipy.stats.false_discovery_control.html)
