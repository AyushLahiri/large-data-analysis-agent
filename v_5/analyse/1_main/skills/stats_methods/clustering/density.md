# Density-based clustering (HDBSCAN, DBSCAN)

Apply the main clustering guidance first.

## When it fits

Use it when clusters may have irregular shapes, the number of clusters is unknown, and some rows are expected not to belong to any group (noise). It suits numeric, scaled data of low to moderate dimension. Above about 10 variables, reduce them with PCA first.

## Fit

- **Default, HDBSCAN** (Campello, Moulavi & Sander, 2013): `sklearn.cluster.HDBSCAN(min_cluster_size=m).fit(X_scaled)`. Set `m` to the smallest group size that would matter to the user, for example 1–5% of rows. It finds the number of clusters itself and handles clusters of different densities. Label −1 is noise.
- **DBSCAN**, when the user needs one fixed density threshold: `DBSCAN(eps=ε, min_samples=m).fit(X_scaled, sample_weight=w)`. Set `min_samples` to about twice the number of variables. Choose `eps` at the bend of the sorted k-nearest-neighbour distance curve (`NearestNeighbors`), and report the value.
- The silhouette is computed on non-noise rows only. Say so.

## Checks specific to density methods

| Check | Action |
|---|---|
| Noise share | Report the % labelled noise. Above about 30%, the density settings or variables do not suit the data; say the grouping covers only part of the population |
| One giant cluster | Usually means `eps` is too large or the data have no density gaps. Report that there is no clear grouping |
| Sensitivity | Refit with `min_cluster_size` (or `eps`) 25% lower and higher. Report whether the main clusters persist |

## Interpret

- Profile the clusters as in the main guidance. Add a row for "Not assigned (noise)" with its size and profile, because it often holds unusual cases worth noting.
- Wording: "Two clear groups emerged covering 78% of households; the remaining 22% did not fit any group closely."

## Headline definitions

- DBSCAN: a point is a core point if at least m points lie within distance ε. Clusters are maximal sets of density-connected core points and their neighbours.
- HDBSCAN uses mutual reachability distance, `d_mreach(a, b) = max{core_m(a), core_m(b), d(a, b)}`, and keeps the most persistent clusters of the density hierarchy.

References: [scikit-learn HDBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.HDBSCAN.html) · [scikit-learn DBSCAN](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.DBSCAN.html) · [Campello et al. 2013](https://doi.org/10.1007/978-3-642-37456-2_14)
