# Linear regression

Apply the main regression guidance first.

## Fit

- Unweighted: `smf.ols('y ~ x + C(g, Treatment(reference="A"))', d).fit(cov_type='HC3')`.
- Weighted: `smf.wls(formula, d, weights=w).fit(cov_type='HC1')`, or `cov_type='cluster', cov_kwds={'groups': d.psu}` when a PSU is documented.
- Use robust (heteroscedasticity-consistent) standard errors by default. HC3 is recommended for small samples (Long & Ervin, 2000). Coefficients and CIs come from `fit.params` and `fit.conf_int(0.05)`.
- Skewed positive outcome without zeros: model `np.log(y)`.
- Skewed outcome with zeros: use a log-link GLM instead, `smf.glm(formula, d, family=sm.families.Poisson(), var_weights=w).fit(cov_type='HC0')`. It is valid for any non-negative outcome, not only counts, and exp(β) is a ratio of means.

## Checks

| Check | Requirement | Action |
|---|---|---|
| Sample size | At least 2 observations per estimated parameter for stable coefficients and CIs (Austin & Steyerberg, 2015). For a model meant to predict, Riley et al. (2020) criteria need far more, so report out-of-sample performance | Run, and flag below 2 per parameter |
| Linearity | Residual means by deciles of fitted values and of each key continuous predictor should be near zero | Add a quadratic or spline term, or log the predictor |
| Constant variance | Not needed with robust SEs | Keep robust SEs. Report them as such |
| Normality of residuals | It matters only for small samples (n below about 30 per parameter group) | With small n and heavy skew, transform the outcome or flag the intervals as approximate |
| Multicollinearity, influence | See the main guidance | — |

## Interpret

| Model form | Coefficient β₁ means | Plain wording |
|---|---|---|
| y on x | A one-unit higher x goes with β₁ units higher y on average | "Each extra room is associated with 35 kWh more annual use, other factors held constant." |
| y on a category | The difference in mean y from the reference group | "Renters use 210 kWh less than owners on average, after adjusting for…" |
| log(y) on x | A one-unit higher x goes with a (exp(β₁) − 1)·100% change in y | "Each extra occupant is associated with about 8% higher spending." |
| y on log(x) | A 10% higher x goes with β₁·ln(1.10) units change in y | "A 10% higher income is associated with 4.2 units more spending." |
| log(y) on log(x) | A 1% higher x goes with about β₁% change in y (an elasticity) | "A 1% higher income goes with about 0.3% higher spending." |
| Log-link GLM | exp(β₁) is the ratio of mean y | "Mean spending is 1.12 times as high (12% higher) per extra room." |
| Interaction x × group | The slope of x differs by β₃ in that group | Report the slope in each group with its CI, using `fit.t_test` for the combined slope |

Always convert log-scale coefficients and their CI bounds to percentages before reporting. Never report a raw log coefficient to the user.

## Key metrics

| Metric | Source | Plain meaning |
|---|---|---|
| R² | `fit.rsquared` | Share of variation in the outcome the model accounts for: "The model accounts for 23% of the variation in spending." It is not a measure of whether the findings are correct. |
| Adjusted R² | `fit.rsquared_adj` | R² penalised for the number of predictors; use it to compare models with different predictor counts |
| RMSE (residual standard error) | `np.sqrt(fit.scale)` (unweighted) | Typical size of a prediction error, in outcome units |
| Overall F-test p-value | `fit.f_pvalue` (it uses the robust covariance) | Whether the predictors jointly explain anything beyond the mean |
| Out-of-sample RMSE, MAE, R² | 5-fold CV with `cross_val_score` on a scikit-learn `LinearRegression`, or a held-out split | Only when prediction is the purpose; out-of-sample R² below the in-sample R² shows overfitting |

For a log outcome, R² and RMSE are on the log scale. Say so, or report RMSE after back-transforming the predictions.

## Headline equation

- `Yᵢ = β₀ + β₁X₁ᵢ + … + βₖXₖᵢ + εᵢ`. Notation: Yᵢ is the outcome for unit i, Xⱼᵢ the predictors, βⱼ the coefficients and εᵢ the error.
- For a log outcome: `ln Yᵢ = β₀ + Σβⱼ Xⱼᵢ + εᵢ`.
- For a log link: `ln E[Yᵢ | X] = β₀ + Σβⱼ Xⱼᵢ`.

References: [statsmodels OLS/WLS](https://www.statsmodels.org/stable/regression.html) · [Long & Ervin 2000, HC standard errors](https://doi.org/10.1080/00031305.2000.10474549) · [Austin & Steyerberg 2015](https://pubmed.ncbi.nlm.nih.gov/25704724/) · [UCLA, log-transformed variables](https://stats.oarc.ucla.edu/other/mult-pkg/faq/general/faqhow-do-i-interpret-a-regression-model-when-some-variables-are-log-transformed/)
