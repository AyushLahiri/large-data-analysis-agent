# Group comparisons and categorical association

Apply the main hypothesis_correlation guidance first. Python has `scipy.stats`, `statsmodels` (formula API `statsmodels.formula.api as smf`) and `pandas`.

## Choose the test

| Question | Default test | Use instead when | Effect to report |
|---|---|---|---|
| Two independent groups, continuous outcome | Welch t-test: `scipy.stats.ttest_ind(a, b, equal_var=False)` | The outcome is ordinal, or groups are small and heavily skewed: Mann-Whitney U, `mannwhitneyu` | Mean difference with CI from `.confidence_interval()`; for Mann-Whitney, the rank-biserial r = 1 − 2U/(n₁n₂) |
| Two paired measurements | Paired t-test: `ttest_rel` | The differences are ordinal or strongly skewed with small n: Wilcoxon signed-rank, `wilcoxon` | Mean of the paired differences with CI |
| Three or more independent groups, continuous outcome | Welch ANOVA: `statsmodels.stats.oneway.anova_oneway(values, groups, use_var='unequal')` | Ordinal outcome: Kruskal-Wallis, `kruskal` | Group means; η² = SS_between / SS_total |
| Post hoc pairs after a significant omnibus test | Pairwise Welch t-tests with Holm adjustment | Equal variances are plausible: `scipy.stats.tukey_hsd` | Each pairwise difference with CI |
| Two-way grouping (two factors), continuous outcome | `smf.ols('y ~ C(a) * C(b)', d).fit()` then `sm.stats.anova_lm(fit, typ=2, robust='hc3')` | There is a meaningful interaction and the cells are unbalanced: Type III with sum-to-zero contrasts, `C(a, Sum) * C(b, Sum)`, `typ=3` | Cell means; partial η² per term |
| Three-way grouping, continuous outcome | The same model with `C(a) * C(b) * C(c)` | Some cells are empty or tiny: drop the three-way term and report main effects and two-way interactions | Cell means for the interaction that matters |
| Two categorical variables | Pearson chi-square: `scipy.stats.chi2_contingency(table, correction=False)` | A 2×2 table with an expected count below 5: Fisher's exact test, `fisher_exact` | Row percentages; Cramér's V from `scipy.stats.contingency.association(table, method='cramer')`; for 2×2, the difference in proportions with CI |
| Two proportions | `statsmodels.stats.proportion.test_proportions_2indep` | — | Difference in percentage points with CI from `confint_proportions_2indep(..., method='newcomb')` |
| Paired binary (before/after on the same units) | McNemar: `statsmodels.stats.contingency_tables.mcnemar` | — | Difference in paired proportions |
| Two categorical variables within strata of a third (2×2×K) | Cochran-Mantel-Haenszel: `StratifiedTable(tables).test_null_odds()` | The odds ratios differ across strata (`test_equal_odds()`, Breslow-Day): report each stratum separately | Pooled Mantel-Haenszel odds ratio with CI |
| Observed distribution against expected shares | Goodness of fit: `scipy.stats.chisquare(observed, expected)` | — | Observed and expected percentages |
| Mixed types (binary × continuous) | Use the two-group comparison above | — | Mean difference with CI |

## Test-specific checks

- **Chi-square expected counts:** check `expected_freq` from `chi2_contingency`. At least 80% of cells need an expected count of 5 or more, and none may be below 1 (Cochran; McHugh, 2013). If this fails, merge substantively adjacent categories, or use Fisher's test for a 2×2 table. Otherwise run the test and flag that the p-value is approximate.
- **Chi-square input:** use observed unweighted counts, never percentages or weighted counts.
- **t-test and ANOVA:** Welch's tests handle unequal variances. With strong skew or outliers and fewer than about 30 per group, also run the rank-based test. Report both if their conclusions differ.
- **Factorial ANOVA:** every cell of the crossed factors needs observations. Report cell n. An interaction makes main effects conditional, so interpret the interaction through cell means first.
- **Paired tests:** confirm that the pairing key matches exactly one row per unit per occasion.

## Weighted versions

- **Means:** `smf.wls('y ~ C(group)', d, weights=w).fit(cov_type='HC1')`. Use `cov_type='cluster', cov_kwds={'groups': d.psu}` when a PSU is documented. The coefficient is the weighted mean difference. For three or more groups, use the joint test `fit.wald_test_terms()`.
- **Proportions or 2×K tables with a binary outcome:** `smf.glm('y ~ C(group)', d, family=sm.families.Binomial(), var_weights=w).fit(cov_type='HC0')`, then a Wald test on the group terms. Report weighted percentages, not odds ratios.
- **R×C tables where neither variable is binary:** run unweighted chi-square on unweighted counts, show weighted percentages, and flag that the p-value ignores the design. A Rao-Scott corrected test is not available in this sandbox.

## Multiple comparisons

- A few planned comparisons or post hoc pairs: Holm, `statsmodels.stats.multitest.multipletests(p, method='holm')`.
- Many exploratory tests, such as one test per category or variable: Benjamini-Hochberg false discovery rate, `scipy.stats.false_discovery_control(p)`.
- Report adjusted p-values and say which adjustment was used.

## Interpreting each result

| Result | Say it like this |
|---|---|
| Welch t / mean difference | "On average, renters spend 120 units less than owners (95% CI 80 to 160 less). This gap is statistically significant." |
| Mann-Whitney / rank-biserial | "Values in group A tend to be higher than in group B. Picking one person from each, A is higher about 64% of the time." Use (1 + r)/2 for that probability. |
| ANOVA omnibus | "Average spending differs across the four regions." Then name which pairs differ, from the adjusted post hoc tests. |
| Interaction | "The gap between owners and renters is larger in urban areas (X) than in rural areas (Y)." Do not report a main effect on its own when the interaction is significant. |
| Chi-square / Cramér's V | "Fuel type is associated with region." Then describe the largest differences in row percentages. Cramér's V runs from 0 (no association) to 1 (complete). It is a strength measure, not a percentage. |
| Difference in proportions | "42% of owners and 31% of renters reported saving energy, a gap of 11 percentage points (95% CI 6 to 16)." |
| Odds ratio (CMH) | "Within each region, the odds of saving energy were 1.5 times as high for owners." Do not say "1.5 times as likely" unless the outcome is rare, below about 10%. |
| Non-significant | "The data show no statistically significant difference. The interval (−20 to +90) is wide, so a moderate gap cannot be ruled out." |

## Table layout

| Comparison | Group A estimate | Group B estimate | Difference (95% CI) | Test statistic | p-value | n A | n B |
|---|---|---|---|---|---|---|---|

For omnibus tests, add one summary row, or a second table holding the statistic, df, p-value, effect size and total n. Keep post hoc pairs in their own table.

## Headline formulas for the methodology

- Welch t: `t = (x̄₁ − x̄₂) / √(s₁²/n₁ + s₂²/n₂)`, where x̄ is the group mean, s² the group variance and n the group size.
- Chi-square: `χ² = Σ (Oᵢⱼ − Eᵢⱼ)² / Eᵢⱼ`, df = (r − 1)(c − 1), where O is the observed count, E the expected count and r, c the numbers of rows and columns.
- One-way ANOVA: `F = MS_between / MS_within`.
- Cramér's V: `V = √(χ² / (n · (min(r, c) − 1)))`.
- Two-way model: `Yᵢⱼₖ = μ + αᵢ + βⱼ + (αβ)ᵢⱼ + εᵢⱼₖ`.

References: [SciPy stats](https://docs.scipy.org/doc/scipy-1.15.3/reference/stats.html) · [McHugh 2013, chi-square](https://pmc.ncbi.nlm.nih.gov/articles/PMC3900058/) · [Delacre et al. 2017](https://rips-irsp.com/articles/10.5334/irsp.82) · [statsmodels ANOVA](https://www.statsmodels.org/stable/anova.html) · [Lumley, svychisq](https://faculty.washington.edu/tlumley/old-survey/html/svychisq.html)
