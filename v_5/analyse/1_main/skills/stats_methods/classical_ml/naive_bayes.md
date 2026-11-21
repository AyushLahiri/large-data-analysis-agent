# Naive Bayes

Apply the main classical_ml guidance first.

## When it fits

Use it as a fast, low-variance classifier for small samples, many categorical predictors, or as a baseline. It assumes predictors are independent within each class. That is rarely true, so its rankings are often useful but its probabilities are usually poorly calibrated.

## Fit

- Categorical predictors: `CategoricalNB(min_categories=...)` on ordinal-encoded categories (`OrdinalEncoder`).
- Numeric, roughly normal predictors: `GaussianNB()`. Log-transform skewed positive variables first.
- For mixed types, fit both and combine log-probabilities, or discretise the numeric predictors into quantile bins for `CategoricalNB`, and say so.
- Tune only the smoothing (`alpha` ∈ {0.1, 0.5, 1}) for CategoricalNB.
- Weights: `fit(X, y, sample_weight=w)`.

## Checks specific to naive Bayes

- **Calibration:** always report the Brier score. Do not present its probabilities as risks unless calibrated with `CalibratedClassifierCV`.
- **Redundant predictors:** near-duplicate predictors are double-counted, which pushes probabilities to extremes. Drop duplicates.

## Interpret

- Per-class feature likelihoods can be shown for categorical predictors, as the share of each category within each class. Permutation importance gives the overall ranking.

## Headline model

- `P(y = k | x) ∝ P(y = k) Πⱼ P(xⱼ | y = k)`. Notation: P(y = k) is the class prior, and P(xⱼ | y = k) the per-predictor likelihood, which is categorical frequency or Gaussian density.

References: [scikit-learn naive Bayes](https://scikit-learn.org/stable/modules/naive_bayes.html)
