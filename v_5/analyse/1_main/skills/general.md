# General analysis and cross-tabs

## For any analysis

- Identify the observation unit, coverage, eligible filters, and which columns answer the question. For a simple count or grouped measure, prefer SQL. Use Python for distribution work that is awkward in SQL.
- If weights are appropriate and available, use them for totals, shares, and means. Report the unweighted count and document the weight. Check null, nonpositive, and invalid weights separately. Do not guess how a weight is scaled.
- Exclude nulls and documented missing codes on the variables needed for that estimate unless asked otherwise. Record the base, number dropped, and missing percentage per analyzed column before exclusion. Avoid deleting rows because unrelated variables are missing.
- For dates, amounts, and changes, check units, period, levels of aggregation, and whether groups or files are comparable. Make any recode explicit.
- If a user asks for specific records from their project, keep the preview focused on the requested fields and scope. Use human labels for coded values and say if the answer is a preview.
- Return the smallest useful table with readable headers and a short interpretation. Include substantial missingness in the methodology when it affects the answer.

## When comparing categories

- Identify the eligible population, grouping variable, outcome, and denominator. If the question asks for a share within each group, divide by that group's eligible total. Use the overall eligible total for a share of the whole sample.
- For a weighted cross-tab, sum weights in each cell and divide by the sum of weights in the relevant denominator. Show unweighted counts on the same analytic base. Keep null weights separate from zero, negative, or invalid weights when checking exclusions.
- Join parts on the full primary key, preserving the intended observation unit. Read the tests skill if the user asks whether a difference is statistically significant.

## Margin of error for cross-tabs

- Show one margin of error for each estimated percentage, mean, or population total. Match the weighting and analysis base to the estimate, and show the unweighted sample size for context. Use 95% confidence unless the user requests another level. Express percentage margins in percentage points.
- For a symmetric interval, calculate `MOE = critical value × standard error`. Use the critical value for the sampling design and confidence level. For example, use `t.ppf(0.975, df)` for a 95% t interval. For an asymmetric interval, report the distance from the estimate to each bound in one cell, such as `−3.2 / +4.1 pp`.
- For independent unweighted proportions, use a Wilson interval from `scipy.stats.binomtest(x, n).proportion_ci(confidence_level=0.95, method="wilson")`, where `x` is the number with the outcome and `n` is the eligible base. For an unweighted mean under simple sampling, use `SE = sample SD / sqrt(n)` with `n − 1` degrees of freedom. [SciPy proportion intervals](https://docs.scipy.org/doc/scipy-1.15.3/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html) · [NIST mean intervals](https://www.itl.nist.gov/div898/handbook/eda/section3/eda352.htm)
- For survey-weighted estimates, use the documented replicate-weight method or compute design-based standard errors by Taylor linearization. For a proportion or mean under a stratified cluster design, estimate `p = Σ(w × y) / Σw` within the eligible group. For each sampled cluster, compute `z = Σ(w × y) − p × Σw` over its eligible cases, including zero contributions for clusters with no eligible cases. Under the with-replacement approximation, compute `SE² = Σ_h [m_h / (m_h − 1) × Σ_j (z_hj − mean_h(z))²] / (Σw)²`, where `h` indexes strata and `m_h` is the number of sampled clusters in that stratum. Follow the survey documentation for finite population corrections, single-cluster strata, degrees of freedom, and interval choice. [DHS sampling methods](https://dhsprogram.com/pubs/pdf/FR382/FR382.pdf) · [CDC subgroup variance guidance](https://wwwn.cdc.gov/nchs/nhanes/tutorials/varianceestimation.aspx)
- For a weighted population total, use expansion weights documented for totals. For the same linearization variance calculation, use each cluster's weighted total as `z` and omit the division by `(Σw)²`. [CDC weighting and estimation methodology](https://wwwn.cdc.gov/nchs/data/nhanes3/manuals/wgt_exec.pdf)
- For observed counts, show the count itself. For an estimate without enough information for a defensible standard error, show the margin as unavailable and say what information is missing. Keep the weighting and variance method in the methodology.
