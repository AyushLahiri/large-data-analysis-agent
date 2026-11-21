# Clustering

Use this when the user wants to find groups, segments or profiles that are not already defined in the data. Then read one reference:

- Numeric variables, and roughly round groups of similar size are expected → read **stats_methods/clustering/kmeans**.
- Mixed numeric and categorical variables, a small sample, or the user wants to see how groups nest → read **stats_methods/clustering/hierarchical**.
- Groups of irregular shape, or many points expected to be noise or outliers → read **stats_methods/clustering/density** (DBSCAN, HDBSCAN).
- Soft membership (probability of belonging), or clusters of different shapes and sizes in numeric data → read **stats_methods/clustering/mixture** (Gaussian mixture).

Python has `scikit-learn`, `scipy`, `numpy` and `pandas`. To predict an existing label, use **stats_methods/classical_ml/classical_ml** instead.

## Plan

- Clustering is descriptive. It always returns groups, even when the data have none, so the job is to show whether the groups are real and useful.
- Choose the clustering variables from the user's purpose, such as energy behaviours to segment households. Keep outcomes the user wants to compare across segments out of the clustering, and use them afterwards to profile the clusters.
- Use few, relevant variables, typically 3 to 15. Irrelevant variables blur real structure.
- Run the method and flag any weak result. Skip only when clustering is impossible: fewer rows than about 10 per variable, or a constant variable set.

## Prepare the data

- Apply the general missingness rules. Drop rows missing any clustering variable, which is complete-case analysis. Report n and the share dropped, and say so in the answer if it exceeds about 10%.
- Map coded categories to labels. Treat nominal codes as categories, never as numbers.
- **Scale numeric variables:** distance-based methods are dominated by large-unit variables. Standardise with `StandardScaler`, or use `RobustScaler` when outliers are heavy. Log-transform strongly right-skewed positive variables, such as income or spending, before scaling.
- **Categorical variables:** k-means and Gaussian mixtures need numeric input. With mixed types, use Gower distance with hierarchical clustering (see hierarchical). Do not run k-means on one-hot dummies.
- **Correlated variables:** near-duplicate measures give their concept extra weight. Drop duplicates, or reduce them with PCA (`sklearn.decomposition.PCA`), keeping components that explain about 80% of the variance and saying how many were kept.
- **Weights:** k-means and DBSCAN accept `sample_weight`; hierarchical clustering and Gaussian mixtures do not. Without weight support, cluster unweighted, then profile the clusters with weighted shares and means, and say so.

## Checks

| Check | How | Action |
|---|---|---|
| Number of clusters | Compare k = 2 to 8 with the method's criterion (see the reference) plus the silhouette | Choose the k the criteria support and that the user can interpret. Report the runner-up if it is close |
| Separation | Average silhouette, `sklearn.metrics.silhouette_score`. As a rough guide (Kaufman & Rousseeuw, 1990), above 0.5 is reasonable structure, 0.25–0.5 weak, below 0.25 no substantial structure | Below 0.25, report that there is no clear grouping. You may still describe the partition, as a convenience segmentation |
| Stability | Refit on 20 bootstrap resamples or random halves and compare the labels with `adjusted_rand_score`. Per cluster, mean Jaccard of 0.75 or more is stable, 0.6–0.75 is a pattern with unclear membership, and 0.5 or less dissolves (Hennig, 2007) | Report unstable clusters as tentative |
| Cluster size | Share of rows per cluster | A cluster with very few members, or below about 5% of rows, is usually outliers. Flag it, or merge by choosing a smaller k |
| Sandbox size | Pairwise-distance methods need memory that grows with n² | For hierarchical or Gower clustering, use at most about 5,000 rows. Draw a random sample, or fit on a sample and assign the rest |

## Interpret

- Describe each cluster by how it differs from the overall average: a profile table of means or shares per cluster for the clustering variables and key outcomes.
- Name a cluster after its two or three most distinctive features, for example "large, older homes with high heating use". Avoid names that imply causes or value judgements.
- A cluster is a summary, not a natural type. Say it is "one useful way to group" the data. When separation or stability is weak, say the boundaries are fuzzy.
- Differences in outcomes across clusters are descriptive. If the user asks whether they are significant, read hypothesis_correlation, and note that p-values are optimistic because the clusters were built from related data.

## Key metrics to report

**Calculate all, report three:** compute every metric listed for the method. In the answer and tables, report at most three that together give the main overview of the result for this question. Mention any other metric only when it shows a problem, as one plain flag sentence, for example "The model's probabilities run too high for the riskiest group (poor calibration)."

| Metric | Report | Meaning for the user |
|---|---|---|
| k and the method that chose it | Always | How many groups, and why that number |
| Cluster sizes, n and % (weighted % if weights apply) | Always | How common each group is |
| Average silhouette | Always (Gower: use the precomputed distances) | How distinct the groups are, from −1 to 1 |
| Stability (mean ARI or per-cluster Jaccard) | Always | Whether the same groups reappear on slightly different data |
| Davies-Bouldin (lower is better), Calinski-Harabasz (higher is better) | Optional, for comparing k | Secondary separation checks |
| BIC (Gaussian mixture), noise share (density methods) | When that method is used | See the reference |

## Present

- **Table 1, profile:** one row per cluster with the cluster label, n, % of the population, and the mean or % for each key variable. Add an "All" row for comparison.
- **Table 2, quality:** k, silhouette, stability, and the method-specific metric.
- **Words:** two to four sentences naming each cluster by its defining features and size, then one sentence on how clear and stable the grouping is. Do not list every variable's value in prose; the table holds them.

## Methodology (method.steps)

Use short bold labels in pipeline order and stay within eight bullets:

- **Sample:** population, analysed n, and rows dropped.
- **Variables:** the clustering variables, with the profiling-only variables named separately.
- **Transforms:** scaling, logs and PCA components kept.
- **Weights:** used in fitting, or used only for profiling.
- **Method:** the algorithm, its key settings (linkage, distance, k range) and the headline objective from the reference.
- **Notation:** one line defining the symbols.
- **Choosing k:** the criterion used and the value chosen.
- **Validation:** the silhouette and stability results.

References: [scikit-learn clustering](https://scikit-learn.org/stable/modules/clustering.html) · [Rousseeuw 1987, silhouettes](https://doi.org/10.1016/0377-0427(87)90125-7) · [Hennig 2007, cluster stability](https://ideas.repec.org/a/eee/csdana/v52y2007i1p258-271.html) · [fpc clusterboot thresholds](https://search.r-project.org/CRAN/refmans/fpc/html/clusterboot.html)
