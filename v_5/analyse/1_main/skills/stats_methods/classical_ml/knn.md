# k-nearest neighbours

Apply the main classical_ml guidance first.

## When it fits

Use it for small to moderate numeric data (up to about 50,000 rows and 20 predictors) where cases with similar characteristics should have similar outcomes. Performance degrades with many predictors, because distances lose meaning in high dimensions.

## Fit

- `Pipeline([('scale', StandardScaler()), ('model', KNeighborsClassifier(n_neighbors=k, weights='distance'))])`, or `KNeighborsRegressor`.
- Tune `k` ∈ {5, 11, 21, 51} by cross-validation. Use odd k for binary classification.
- Scaling is mandatory. One-hot encode categorical predictors, or prefer a tree model when there are many.
- Survey weights cannot be used in fitting. Evaluate with weighted metrics and say so.

## Checks specific to kNN

- **Dimensionality:** with more than about 20 predictors, compare against random forest. If kNN is clearly worse, report the forest.
- **Ties and duplicates:** many identical rows make neighbours arbitrary. Mention it if the predictors are mostly categorical.

## Interpret

- kNN has no global coefficients. Use permutation importance and partial dependence.
- Wording: "Predictions are based on the 21 most similar households."

## Headline model

- `ŷ(x) = Σ_{i∈N_k(x)} vᵢyᵢ / Σ vᵢ`, where vᵢ = 1/d(x, xᵢ) for distance weighting. Notation: N_k(x) is the set of the k nearest training rows by Euclidean distance on the scaled predictors.

References: [scikit-learn neighbors](https://scikit-learn.org/stable/modules/neighbors.html)
