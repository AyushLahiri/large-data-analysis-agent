# Random forest

Apply the main classical_ml guidance first.

## Fit

- Use `RandomForestClassifier` or `RandomForestRegressor(n_estimators=500, min_samples_leaf=5, max_features='sqrt', n_jobs=1, random_state=0)`. For regression, use `max_features=1.0` or `0.33`.
- For imbalanced classes, `class_weight='balanced_subsample'` improves minority recall, but it shifts the probabilities. Report calibration if probabilities are shown.
- Small tuning grid: `min_samples_leaf` ∈ {1, 5, 20} and `max_features` ∈ {'sqrt', 0.5}. More trees do not overfit; 500 is usually enough.
- The out-of-bag score (`oob_score=True`) is a quick internal check, but the reported metrics come from the test set.
- Encoding: one-hot or ordinal codes both work; ordinal codes are fine for trees.

## Checks specific to random forests

- **Overfitting:** training scores are near perfect by design. Compare the cross-validated and test scores only.
- **Importance:** never report `feature_importances_` (impurity) as the main ranking. Use permutation importance on the test set (Strobl et al., 2007).
- **Correlated predictors** split importance between them. Mention it when two top predictors measure similar things.

## Interpret

- Wording: "Of the 25 characteristics, dwelling age, heating type and household size were the most useful for predicting high energy use. Shuffling dwelling age reduced balanced accuracy by 0.06."
- Use partial dependence for direction. The forest itself has no coefficients.

## Headline model

- `f̂(x) = (1/B) Σ_b T_b(x)` for regression, or the majority vote or mean class probability for classification. Notation: T_b is a tree grown on bootstrap sample b with a random subset of predictors considered at each split, and B is the number of trees (Breiman, 2001).

References: [scikit-learn forests](https://scikit-learn.org/stable/modules/ensemble.html#forests-of-randomized-trees) · [Breiman 2001](https://doi.org/10.1023/A:1010933404324) · [Strobl et al. 2007](https://pmc.ncbi.nlm.nih.gov/articles/PMC1796903/)
