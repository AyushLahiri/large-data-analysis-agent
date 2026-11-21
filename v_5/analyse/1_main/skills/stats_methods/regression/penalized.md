# Lasso, ridge and elastic net

Apply the main regression guidance first, plus the linear or logistic reference for the outcome type.

## When to use

| Method | Use when | Note |
|---|---|---|
| Ridge (L2) | Many correlated predictors; the goal is stable prediction using all of them | Shrinks all coefficients and keeps every predictor |
| Lasso (L1) | Many candidate predictors; the goal is a short list of the most predictive | Sets some coefficients to exactly zero. With correlated predictors it picks one somewhat arbitrarily |
| Elastic net | Many correlated candidates and selection is wanted | Mixes the two, and selects groups of correlated predictors more stably |

Use these for prediction and predictor screening. For "what is the effect of X adjusting for Z" with a modest number of predictors, use ordinary linear or logistic regression, because penalized coefficients are deliberately biased toward zero.

## Fit (scikit-learn)

- Build a design matrix: one-hot encode categorical predictors keeping all levels (no reference category is needed under a penalty), and keep the label names.
- Standardise every predictor with `StandardScaler` inside a `Pipeline`, so the penalty treats predictors equally.
- Continuous outcome: `ElasticNetCV(l1_ratio=[0.1, 0.5, 0.9, 1.0], cv=10, random_state=0)`. Lasso is `l1_ratio=1`, and ridge is `RidgeCV(alphas=np.logspace(-3, 3, 50))`.
- Binary outcome: `LogisticRegressionCV(penalty='elasticnet', solver='saga', l1_ratios=[0.1, 0.5, 0.9, 1.0], Cs=20, cv=10, scoring='roc_auc', max_iter=5000, random_state=0)`. Lasso is `penalty='l1'` and ridge `penalty='l2'`.
- Weighted: pass `sample_weight=w` to `fit` through the pipeline step (`model__sample_weight=w`). Cross-validation folds remain unweighted splits; say so.
- Choosing the penalty: the CV minimum gives the best predictive accuracy. A sparser model whose CV error is within one standard error of the minimum (the "1-SE rule") is often preferred for interpretability (Hastie, Tibshirani & Wainwright, 2015). Compute it from `mse_path_` for linear models, or from `scores_` for logistic. State which rule was used.
- Keep within the sandbox limits: at most about 50 predictors after encoding, 10 folds, and one model per script unless comparing.

## Checks

- **Out-of-sample performance:** report the CV R² or RMSE (continuous) or the CV AUC (binary). This is the main result. Check that it beats a null model (the mean, or the overall rate). If it does not, say the predictors have little predictive value.
- **Stability:** for lasso, refit on 5 to 10 bootstrap resamples and report how often each predictor is selected. Predictors selected in fewer than half the resamples should be described as unstable.
- **Sample size:** more predictors than observations is allowed. Each predictor category still needs enough cases to estimate; merge sparse categories.

## Key metrics

- **Linear:** CV RMSE and CV R², from `cross_val_score(pipe, X, y, cv=10, scoring='neg_root_mean_squared_error' / 'r2')` or the held-out test set, next to a mean-only baseline.
- **Logistic:** CV AUC and Brier score, next to the overall-rate baseline. If a yes/no rule is asked for, use the classical_ml confusion-matrix metrics at a stated threshold.
- **Always:** the chosen λ (`alpha_` or `C_`), α (`l1_ratio_`), the tuning rule, the number of non-zero coefficients, and the n and folds used.

## Interpret

- **No p-values or confidence intervals:** coefficients after penalization and selection have no valid standard errors from the usual formulas (post-selection inference; Hastie et al., 2015). Do not report p-values, and do not call predictors "significant".
- **Coefficients:** coefficients are on the standardised scale and shrunk toward zero. Interpret their sign and relative size only: "Among the 30 characteristics tested, household size, dwelling age and heating type were the strongest predictors of energy use."
- **Selection:** a predictor dropped by lasso is not proven irrelevant. It may be correlated with a selected predictor.
- **Effect sizes:** if the user needs effect sizes for the selected predictors, refit an ordinary linear or logistic model on them, and say that its p-values are optimistic because the same data chose the predictors.

## Table layout

Main table: Predictor | Standardised coefficient | Selection frequency (lasso or elastic net). Sort by absolute coefficient and show only non-zero coefficients for lasso. Give the CV performance, penalty choice, l1_ratio and n in a short second table or in the title.

## Headline equation

- Elastic net: `β̂ = argmin_β { (1/2n)·Σ(yᵢ − xᵢᵀβ)² + λ[α·‖β‖₁ + ((1 − α)/2)·‖β‖₂²] }`. Notation: λ is the penalty strength, α the L1 share (α = 1 is lasso, α = 0 is ridge), ‖β‖₁ the sum of absolute coefficients and ‖β‖₂² the sum of squared coefficients. In scikit-learn, λ is `alpha` and α is `l1_ratio`.
- For logistic models, the squared-error term is replaced by the negative log-likelihood.

References: [scikit-learn ElasticNetCV](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.ElasticNetCV.html) · [scikit-learn LogisticRegressionCV](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegressionCV.html) · [Hastie, Tibshirani & Wainwright 2015, Statistical Learning with Sparsity](https://hastie.su.domains/StatLearnSparsity/)
