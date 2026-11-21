# Hierarchical clustering

Apply the main clustering guidance first.

## When it fits

Use it for mixed variable types (with Gower distance), samples of up to about 5,000 rows, or when the user wants to see how groups split into subgroups.

## Fit

- **Numeric only:** `scipy.cluster.hierarchy.linkage(X_scaled, method='ward')`, then `fcluster(Z, k, criterion='maxclust')`. Ward linkage needs Euclidean distance and tends to give compact groups of similar size.
- **Mixed types (Gower distance; Gower, 1971):**
  - Numeric variables: |xᵢ − xⱼ| / range.
  - Categorical variables: 0 if equal, 1 if not.
  - Average over the variables present in both rows.
  - Build the condensed distance vector with `scipy.spatial.distance.pdist` per variable, or a small NumPy loop.
  - Then `linkage(d, method='average')`. Do not use Ward with Gower distance.
- Linkage choice:
  - Ward (numeric), or average (any distance), as defaults.
  - Avoid single linkage, which chains clusters together.
  - Complete linkage gives tight clusters but is sensitive to outliers.
- To choose k, compare the silhouette across cuts using `silhouette_score(D_square, labels, metric='precomputed')`, and look for a large jump in merge heights (`Z[:, 2]`).
- Cophenetic correlation, `scipy.cluster.hierarchy.cophenet(Z, d)`, shows how faithfully the tree preserves the original distances. Values near 1 are better; report it.
- No weights in fitting: profile the clusters with weights afterwards.

## Checks specific to hierarchical clustering

- **Memory:** a condensed distance vector holds n(n − 1)/2 values. Keep n at or below about 5,000 in the sandbox, or cluster a random sample and assign the remaining rows to the nearest cluster medoid.
- **Ties and categorical-only data:** many identical Gower distances make the merge order arbitrary. Check stability carefully.

## Interpret

- Report the cut (k) and its profiles as in the main guidance. If useful, mention which clusters merge next as the tree is cut higher ("groups 2 and 3 are the most similar").
- A dendrogram cannot be shown as a table. Describe the nesting in at most one sentence.

## Headline formulas

- Gower: `d(i, j) = Σᵥ δᵢⱼᵥ dᵢⱼᵥ / Σᵥ δᵢⱼᵥ`. Notation: dᵢⱼᵥ is the per-variable dissimilarity, and δᵢⱼᵥ = 1 when variable v is observed for both units.
- Ward merge cost: the increase in total within-cluster sum of squares, `Δ(A, B) = (n_A n_B / (n_A + n_B))·‖μ_A − μ_B‖²`.

References: [SciPy hierarchy](https://docs.scipy.org/doc/scipy-1.15.3/reference/cluster.hierarchy.html) · [Gower 1971](https://doi.org/10.2307/2528823) · [scikit-learn silhouette_score](https://scikit-learn.org/stable/modules/generated/sklearn.metrics.silhouette_score.html)
