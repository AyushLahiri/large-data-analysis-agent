# Hypothesis tests and correlations

Use this for questions about whether groups differ, whether two variables are associated, or how strongly they move together. Then read one reference:

- Comparing groups (means, distributions or proportions), or testing a categorical association → read **stats_methods/hypothesis_correlation/tests**.
- Measuring how strongly two variables move together → read **stats_methods/hypothesis_correlation/correlation**.

Read both when the question needs both. For adjusted associations with several predictors, use **stats_methods/regression/regression** instead.

## Plan

- Turn the question into one primary hypothesis: the outcome, the grouping or second variable, the population and the expected direction if the user gave one. Run extra comparisons only when the question asks for them.
- Use a two-sided test at α = 0.05 and 95% confidence intervals unless the user asks otherwise.
- Classify each variable before choosing a method. Continuous, ordinal, binary, nominal or count types decide the test, so check the codebook rather than the storage type. Coded categories stored as numbers are not continuous.
- Run the test and report any check it fails, unless a result is impossible: an empty group, a constant variable, or fewer than two observations in a group. "Underpowered" or "assumption doubtful" is a flag, not a reason to skip.

## Prepare the data

- Apply the general missingness, weight and denominator rules. Drop missing values and documented missing codes only on the variables in this test, which is complete-case analysis. Report the analysed n, the number dropped and the missing share per variable.
- If more than about 10% of the analysed rows are dropped, say so in the answer. Complete-case results can be biased when missingness depends on other characteristics (Sterne et al., 2009). Name the likely direction only when the data show it, for example by comparing the dropped and kept rows on a known variable.
- Keep recodes explicit and substantive. Merge sparse categories only when the merged group is meaningful, and say which codes went where.
- Pull only the needed columns. Compute group counts first, because they decide which checks apply.

## Weights and survey design

- Descriptive estimates such as means, shares and correlations use the selected weight. Show unweighted n beside them.
- Ordinary SciPy tests treat rows as independent and equally weighted. Running them on weighted counts is wrong. For a weighted comparison, test the same contrast as a weighted regression with robust standard errors; each reference gives the form. If a primary sampling unit (PSU) or cluster column is documented, cluster the standard errors on it, and say that strata were not used, which gives a slightly conservative approximation. This is the regression form of a design-based Wald test (Lumley & Scott, 2017).
- If no design-aware form exists for the test, run the unweighted test on unweighted counts, keep the weighted estimates, and flag that the p-value ignores the survey design.

## Checks that can invalidate a result

| Check | Why it matters | Action |
|---|---|---|
| Independence of observations | Every standard test assumes it. Repeated measures, matched pairs or several rows per unit break it. | Use a paired test or cluster the standard errors. Never treat repeated rows as independent. |
| Group and cell sizes | Small groups give unstable estimates and unreliable approximations. | Report n per group. Apply the size rule in the reference. Flag any group below 60. |
| Variable type matches the test | Means of nominal codes are meaningless. | Choose the test by measurement level. |
| Outliers and skew (continuous outcomes) | Extreme values can drive means and Pearson r. | Compare the median with the mean, and Spearman with Pearson. Report both if they disagree in direction or significance. Do not delete values only because they are extreme. |
| Multiple comparisons | Many tests inflate false positives. | Adjust p-values. See the reference. |

Do not choose between tests based on a preliminary significance test of an assumption, such as Levene's test before a t-test. Use the robust default (Delacre, Lakens & Leys, 2017).

## Interpret

- Lead with the effect size and its interval in the variable's own units: the difference in means, the difference in percentage points, the odds ratio or r. The p-value comes second (Wasserstein & Lazar, 2016).
- A p-value is how surprising the data would be if there were truly no difference or association. It is not the probability that the finding is true. It does not measure size or importance.
- p < 0.05 at 95%: say "statistically significant at the 95% level" and give the plain meaning, for example "the gap is larger than chance alone would likely produce".
- p ≥ 0.05: say "no statistically significant difference was detected". Never say "no difference" or "the groups are the same". If the interval is wide, say the data cannot rule out a meaningful difference.
- Separate statistical significance from practical importance. With large n, trivial differences become significant, so judge size in context.
- Association is not causation. Say "associated with", not "causes" or "leads to", unless the design supports it.

## Present

**Calculate all, report three:** compute the effect, interval, test statistic and every check in the reference. In the answer and tables, report at most three that together give the main overview of the result for this question. Mention any other metric only when it shows a problem, as one plain flag sentence, for example "The model's probabilities run too high for the riskiest group (poor calibration)."

- **Table:** one result table per test with the estimate for each group, the effect and its CI, the test statistic, the p-value and unweighted n. Use one row per comparison and readable labels. Put the check outcomes in the table only when they are part of the result, such as an expected-count flag.
- **Words:** one to three sentences giving the direction, the size in natural units with its CI, whether it is significant at the stated level, and one caveat only if it changes how the result should be read. Do not narrate the checks that passed, list package names, or explain how the test works.
- **Flags:** if a check failed but the test ran, add one plain sentence such as "Two groups have fewer than 60 respondents, so treat this comparison cautiously."

## Methodology (method.steps)

Use short bold labels in pipeline order and stay within eight bullets:

- **Sample:** population, filters, analysed n, and rows dropped for missing values.
- **Recodes:** exact codes or groups, only if any were made.
- **Weights:** the weight used, or "unweighted", and the standard-error type (robust, or clustered on the named PSU).
- **Checks:** only the checks that failed or changed the method, such as sparse cells or a switch to a rank test.
- **Test:** the test's name and its headline formula in plain Unicode, such as `t = (x̄₁ − x̄₂) / √(s₁²/n₁ + s₂²/n₂)`.
- **Notation:** one line defining each symbol.
- **Inference:** the confidence level, sidedness and any multiple-comparison adjustment.

Do not explain how the method works beyond the formula and notation.

References: [ASA statement on p-values](https://www.amstat.org/asa/files/pdfs/p-valuestatement.pdf) · [Delacre et al. 2017, Welch by default](https://rips-irsp.com/articles/10.5334/irsp.82) · [Sterne et al. 2009, missing data](https://www.bmj.com/content/338/bmj.b2393) · [Lumley & Scott 2017, survey regression](https://projecteuclid.org/journals/statistical-science/volume-32/issue-2/Fitting-Regression-Models-to-Survey-Data/10.1214/16-STS605.pdf)
