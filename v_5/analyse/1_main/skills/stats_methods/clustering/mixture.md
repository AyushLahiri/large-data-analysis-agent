# Gaussian mixture models

Apply the main clustering guidance first.

## When it fits

Use it for numeric, scaled data when clusters may differ in size, spread or orientation, or when the user wants each row's probability of belonging to each group rather than a hard label.

## Fit

- `GaussianMixture(n_components=k, covariance_type='full', n_init=5, random_state=0).fit(X_scaled)`. `predict` gives the most likely cluster; `predict_proba` gives membership probabilities.
- To choose k and the covariance type, fit k = 1 to 8 with `covariance_type` in {'full', 'diag', 'tied', 'spherical'} and pick the lowest `bic(X)` (Fraley & Raftery, 2002). Including k = 1 tests whether there are any clusters at all. If one component wins, report that no grouping is supported.
- No sample weights: fit unweighted and profile with weights.

## Checks specific to mixtures

| Check | Action |
|---|---|
| BIC margin | A BIC difference under about 2 between the top models is weak evidence; above 10 is strong (Kass & Raftery, 1995). Report the runner-up when the margin is small |
| Classification certainty | Report the mean of the highest membership probability per row, and the % of rows below 0.7. Many uncertain rows mean overlapping groups |
| Degenerate components | A component with tiny variance or very few rows signals overfitting. Use fewer components or a simpler covariance type |
| Non-normal variables | Skewed variables create spurious extra components. Log-transform skewed positive variables first |

## Interpret

- Profile by the most likely cluster, as in the main guidance. Mention the membership certainty in one phrase.
- Wording: "Three groups fit the data best. Most households clearly belong to one group, but about 15% sit between the 'moderate' and 'high' users."

## Headline model

- `p(x) = Σₖ πₖ 𝒩(x | μₖ, Σₖ)`. Notation: πₖ is the share of cluster k, and μₖ, Σₖ its mean and covariance.
- `BIC = −2 ln L̂ + p ln n`, where p is the number of parameters; lower is better.

References: [scikit-learn GaussianMixture](https://scikit-learn.org/stable/modules/generated/sklearn.mixture.GaussianMixture.html) · [Fraley & Raftery 2002](https://doi.org/10.1198/016214502760047131) · [Kass & Raftery 1995, Bayes factors](https://doi.org/10.1080/01621459.1995.10476572)
