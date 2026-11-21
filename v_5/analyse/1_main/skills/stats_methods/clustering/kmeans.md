# K-means

Apply the main clustering guidance first.

## When it fits

Use it for numeric, scaled variables when roughly spherical groups of similar spread are plausible. It assigns every row to a cluster, outliers included, and is sensitive to them.

## Fit

- `KMeans(n_clusters=k, n_init=20, random_state=0).fit(X_scaled, sample_weight=w)`. Multiple starts (`n_init`) avoid a poor local solution.
- For n above about 100,000, use `MiniBatchKMeans` with the same settings.
- To choose k, compare k = 2 to 8 using:
  - the average silhouette (highest is preferred);
  - the elbow in `inertia_`, which is only a visual aid and is subjective;
  - optionally the gap statistic (Tibshirani, Walther & Hastie, 2001), comparing log within-cluster dispersion with uniform reference data. Choose the smallest k whose gap is within one standard error of the next k's gap.
- For the stability check, refit on bootstrap samples and compare labels on the original rows with `adjusted_rand_score`.

## Checks specific to k-means

- **Outliers:** extreme rows pull centroids toward them. Compare the solution with and without the top 1% of distances to the centroids. If the profiles change, report it, or prefer HDBSCAN or a robust scaler.
- **Unequal cluster spread:** k-means favours equal-sized, equal-spread groups. If one cluster has a much larger within-cluster spread, consider a Gaussian mixture.

## Interpret

- Centroids are cluster means on the scaled data. Report profiles on the original units, computed from the raw data per cluster, never the scaled centroids.
- Wording: "We found three household groups. The largest (46%) uses little energy and lives in newer flats. …"

## Headline objective

- `min Σₖ Σ_{i∈Cₖ} wᵢ‖xᵢ − μₖ‖²`. Notation: Cₖ is cluster k, μₖ its weighted mean (centroid), xᵢ the scaled variables for unit i and wᵢ its weight.

References: [scikit-learn KMeans](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.KMeans.html) · [Tibshirani et al. 2001, gap statistic](https://doi.org/10.1111/1467-9868.00293)
