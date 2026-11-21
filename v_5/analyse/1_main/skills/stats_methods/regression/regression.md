# Regression analysis

Use this when the question asks how an outcome relates to several predictors at once ("controlling for", "adjusted", "which factors matter most", "predict"). Then read one reference:

- Continuous outcome, such as spending, consumption or a score → read **stats_methods/regression/linear**.
- Binary outcome (yes/no) → read **stats_methods/regression/logistic**.
- Many candidate predictors, strongly correlated predictors, or a request to select or rank predictors for prediction → read **stats_methods/regression/penalized** (lasso, ridge, elastic net), plus the linear or logistic reference for the outcome type.

Python has `statsmodels` (formula API `statsmodels.formula.api as smf`), `scikit-learn`, `scipy` and `pandas`.

## Plan

- Decide the purpose, because it changes what is reported:
  - **Explanation:** what the association of X with Y is after adjusting for Z. Report coefficients with CIs.
  - **Prediction:** how well a set of predictors forecasts Y. Report out-of-sample performance.
- Name the outcome, the predictors of interest and the adjustment variables before fitting. Choose adjustment variables because they plausibly affect both X and Y. Never use a significance screen to choose them. Do not adjust for variables caused by X; they absorb part of its effect.
- Use 95% confidence intervals and α = 0.05 unless the user asks otherwise.
- Fit the model and flag any check it fails, unless fitting is impossible: no variation in the outcome, more parameters than observations, or perfect separation that cannot be resolved.

## Prepare the data

- Apply the general missingness and weight rules. Drop rows with missing values on the model's variables only, which is complete-case analysis. Report the analysed n, the number dropped and the missing share per variable. If more than about 10% of rows are dropped, say so in the answer, because estimates can be biased when missingness depends on other characteristics (Sterne et al., 2009).
- Map coded categories to labels before fitting, so terms and tables read as labels.
- Check the units and plausibility of every continuous predictor, such as negative incomes or top-codes.

## Recodes, baselines and transformations

- **Categorical predictors:** enter them as categories with an explicit reference: `C(var, Treatment(reference='Owned'))`. Pick a reference that is substantively natural (the usual or comparison group) and well populated. Never pick a tiny group. Say what the reference is in the table title or the words.
- **Sparse categories:** merge only into a meaningful neighbour, and say so. A category with very few observations, or none with the outcome (logistic), gives unstable or infinite estimates.
- **Continuous predictors:** keep them continuous. Splitting them into high and low loses information and can create spurious results (Altman & Royston, 2006). If the relationship is curved, add a quadratic term or a spline, `bs(x, df=3)`, and interpret through predicted values.
- **Log transformations:** use them for strictly positive, right-skewed variables such as income or spending, where proportional change is the natural scale. Use natural logs. If zeros are present, do not quietly use log(y + c), because that changes the meaning. Use a log-link model for the outcome instead (see linear), and handle zero predictors explicitly.
- **Scaling:** to compare continuous predictors' sizes, standardise them. Dividing by 2 SD (Gelman, 2008) makes them comparable with binary predictors. Report which scaling was used.
- **Interactions:** add one only when the question asks whether an association differs by group, or there is a strong substantive reason. Always keep both main effects (`x * group`). Centre continuous terms so the main effects stay interpretable. Report interactions as predicted values or slopes per group, not raw product coefficients. Three-way interactions need large cells; avoid them unless asked.

## Weights and survey design

- Fit with the selected weight. Use robust standard errors, clustered on the PSU when one is documented. Say that strata were not used, which gives a slightly conservative approximation of design-based errors (Lumley & Scott, 2017).
- Weights fix representativeness. They do not fix a misspecified model (Solon, Haider & Wooldridge, 2015). If weighted and unweighted coefficients differ materially, mention it, because it suggests effects differ across groups the weights emphasise.

## Checks common to all models

| Check | How | If it fails |
|---|---|---|
| Sample size per parameter | Count parameters: each continuous term, each non-reference category, each interaction term | Run, then flag using the limit in the reference |
| Multicollinearity | VIF: `statsmodels.stats.outliers_influence.variance_inflation_factor` on the design matrix | A VIF above 10 (above 5 is a warning) means individual coefficients are unstable. Report it; drop or combine duplicate measures, or use penalized regression |
| Influential observations | Cook's distance from `fit.get_influence()` | Refit without the few largest. If conclusions change, report both. Never silently delete data |
| Specification | Compare the model with a curved term or a key interaction added | Keep the simpler model unless the addition changes the conclusion or clearly improves fit |

## Interpret

- Every coefficient is conditional: "holding the other variables in the model constant". Say this once, plainly.
- Give each effect in the outcome's natural units, or as a ratio for logs and logistic models, with its CI. Then say whether it is significant at the stated level.
- Size matters more than stars. With large samples, tiny effects are significant, so give a sense of scale, such as the difference between typical groups.
- Regression on survey data shows adjusted associations, not causal effects. Say "is associated with … after accounting for …".
- Do not interpret the intercept unless every predictor has a meaningful zero or is centred.
- R² (linear) or AUC (logistic) describes fit, not importance or validity. Mention it briefly.

## Key metrics to report

**Calculate all, report three:** compute every metric listed for the method. In the answer and tables, report at most three that together give the main overview of the result for this question. Mention any other metric only when it shows a problem, as one plain flag sentence, for example "The model's probabilities run too high for the riskiest group (poor calibration)."

| Model | Explanation purpose | Prediction purpose (add, out-of-sample) |
|---|---|---|
| All | Analysed n, number of parameters, and each coefficient with its 95% CI and p-value | Training and test n (or the CV scheme), plus a baseline (mean or overall rate) for comparison |
| Linear | R², adjusted R², residual standard error (RMSE) in outcome units, overall F-test p-value | Test or CV RMSE, MAE, out-of-sample R² |
| Logistic | Events and non-events, McFadden pseudo-R² (`fit.prsquared`), AUC (C-statistic), likelihood-ratio test p-value (`fit.llr_pvalue`) | Test or CV AUC, Brier score, a calibration table; if a classification rule is asked for, the confusion matrix, sensitivity, specificity, false positive rate, precision and balanced accuracy at the stated threshold |
| Penalized | — (no inferential metrics) | CV RMSE or R² (linear), or CV AUC and Brier score (logistic); the chosen λ, α and its rule; the number of non-zero coefficients |

Each reference gives the plain meaning of its metrics. Put the metrics in the title or a small second table, not in the coefficient table.

## Present

- **Table:** one coefficient table. Use one row per term with readable labels and the reference category named. Columns: estimate (or odds ratio), 95% CI lower, 95% CI upper, p-value. Put n, R² or AUC, and the reference groups in the table title or in a short second table. Do not add standard-error, t or z columns unless asked. Stay within 10 columns and 50 rows. With many terms, show those that answer the question and summarise the rest in words.
- **Words:** two to four sentences. Give the main finding with size, direction and CI, any important qualification (an interaction, a non-linear pattern), and at most one caveat that changes the reading. Do not list the checks that passed, variable codes or package names.
- **Flags:** for a failed check, add one plain sentence, for example: "Only 6 households per predictor were available, so estimates may be unstable."

## Methodology (method.steps)

Use short bold labels in pipeline order and stay within eight bullets:

- **Sample:** population, filters, analysed n, and rows dropped for missing values.
- **Variables:** the outcome, the predictors and the reference categories.
- **Recodes and transforms:** merges, logs, splines and scaling, only if any were made.
- **Weights:** the weight, or "unweighted", and the standard-error type (HC robust, or clustered on the named PSU).
- **Checks:** only those that failed or changed the model (VIF, influence, separation, size).
- **Model:** the headline equation in plain Unicode, from the reference.
- **Notation:** one line defining the symbols.
- **Inference / validation:** the confidence level; for penalized models, cross-validation folds and the tuning rule.

Do not explain how the method works beyond the equation and notation.

References: [Austin & Steyerberg 2015](https://pubmed.ncbi.nlm.nih.gov/25704724/) · [Riley et al. 2020, sample size](https://www.bmj.com/content/368/bmj.m441) · [Altman & Royston 2006](https://pmc.ncbi.nlm.nih.gov/articles/PMC1458573/) · [UCLA, log-transformed variables](https://stats.oarc.ucla.edu/other/mult-pkg/faq/general/faqhow-do-i-interpret-a-regression-model-when-some-variables-are-log-transformed/) · [Lumley & Scott 2017](https://projecteuclid.org/journals/statistical-science/volume-32/issue-2/Fitting-Regression-Models-to-Survey-Data/10.1214/16-STS605.pdf) · [Solon et al. 2015](https://www.nber.org/system/files/working_papers/w18859/w18859.pdf)
