# Logistic regression

Apply the main regression guidance first. Code the outcome as 1 for the event of interest and 0 otherwise, and state which is which.

## Fit

- Unweighted: `smf.logit(formula, d).fit(cov_type='HC0')`. Average marginal effects come from `fit.get_margeff(at='overall').summary_frame()`.
- Weighted: `smf.glm(formula, d, family=sm.families.Binomial(), var_weights=w / w.mean()).fit(cov_type='HC0')`. Use `cov_type='cluster', cov_kwds={'groups': d.psu}` when a PSU is documented, and say that this approximates design-based errors. For marginal effects, average the predicted probabilities across the sample with the predictor set to each value.
- Odds ratios are `np.exp(fit.params)` and `np.exp(fit.conf_int(0.05))`.
- For a multi-category outcome, prefer collapsing to the binary contrast the question asks about. Fit `smf.mnlogit` only if the user needs all categories.

## Checks

| Check | Requirement | Action |
|---|---|---|
| Events per parameter | Count the less frequent outcome (events or non-events). Fewer than 10 per parameter risks biased, unstable estimates (Peduzzi et al., 1996). Riley et al. (2020) show that fixed rules are crude and prediction models usually need more | Run, then flag below 10. Reduce parameters by merging categories or dropping weak adjusters |
| Separation | A category or value range in which everyone has the same outcome gives huge coefficients and SEs, or a separation warning | Merge that category meaningfully or remove the term, and say so. If the term is essential, report the raw percentages for it instead |
| Linearity in the log-odds | Check continuous predictors with a spline or quadratic term | Keep the curved term if it matters, and interpret through predicted probabilities |
| Rare outcome | Below about 10% prevalence, odds ratios approximate risk ratios | Above 10%, never describe an odds ratio as "times as likely" |
| Fit and discrimination | See the key metrics | Report them every time; the out-of-sample versions only when prediction is the purpose |

## Key metrics

| Metric | Source | Plain meaning |
|---|---|---|
| Events / non-events | Counts of y | The basis for the sample-size check |
| AUC (C-statistic) | `sklearn.metrics.roc_auc_score(y, fit.predict())` | Chance the model ranks a random "yes" above a random "no"; 0.5 is guessing, 0.7–0.8 acceptable, above 0.8 good, as a rough guide |
| McFadden pseudo-R² | `fit.prsquared` (Logit) or `1 − fit.llf/fit.llnull` (GLM) | Improvement over a model with no predictors. Values of 0.2–0.4 are already a good fit; it is not comparable to linear R² |
| Likelihood-ratio test p-value | `fit.llr_pvalue` (Logit) | Whether the predictors jointly improve on no predictors |
| Brier score | `sklearn.metrics.brier_score_loss(y, p)` | Mean squared error of the predicted probabilities; compare it with the baseline p̄(1 − p̄) |
| Calibration | `sklearn.calibration.calibration_curve(y, p, n_bins=10)` | Whether predicted probabilities match observed rates; report a short table when prediction is the purpose |
| Confusion-matrix rates at a threshold | `confusion_matrix`, `balanced_accuracy_score` | Only when the user wants a yes/no rule. Report sensitivity (true positive rate), specificity, false positive rate, precision and balanced accuracy, and name the threshold |

## Interpret

Give the user probabilities first and odds ratios second. Odds ratios are routinely misread as risk ratios, and they cannot be compared across models with different predictors (Mood, 2010).

| Quantity | Plain wording |
|---|---|
| Predicted probability by group | "With other factors at their sample values, 38% of owners and 27% of renters are predicted to save energy." |
| Average marginal effect | "Being a renter is associated with an 11 percentage point lower chance of saving energy, on average (95% CI 7 to 15 points)." |
| Odds ratio > 1 | "The odds of saving energy are 1.6 times as high for owners as for renters." Add a probability statement alongside it. |
| Odds ratio < 1 | "The odds are 35% lower" for OR = 0.65. Do not invert silently. |
| Continuous predictor | Give the OR per meaningful unit, such as per 10,000 of income, not per single unit, when single units are tiny. |
| Interaction | Report predicted probabilities for each combination. Product-term ORs are not reliable summaries. |

## Table layout

Main table: Term | Odds ratio | 95% CI lower | 95% CI upper | p-value. Add a second table of predicted probabilities or marginal effects for the predictors the question is about. Name the reference categories and the event coding in the titles.

## Headline equation

- `logit P(Yᵢ = 1) = ln[pᵢ / (1 − pᵢ)] = β₀ + β₁X₁ᵢ + … + βₖXₖᵢ`. Notation: pᵢ is the probability of the event for unit i, Xⱼᵢ the predictors, and exp(βⱼ) the odds ratio for a one-unit higher Xⱼ.

References: [statsmodels discrete models](https://www.statsmodels.org/stable/discretemod.html) · [Peduzzi et al. 1996](https://doi.org/10.1016/S0895-4356(96)00236-3) · [Riley et al. 2020](https://www.bmj.com/content/368/bmj.m441) · [Mood 2010](https://doi.org/10.1093/esr/jcp006)
