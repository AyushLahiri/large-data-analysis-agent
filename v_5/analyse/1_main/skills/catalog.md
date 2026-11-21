# Analysis skills

Guides are organised in three levels: **folder → main method → sub-method**. Read a guide with `read_skill` using its path without `.md`, such as `stats_methods/regression/linear`.

- **Folder:** the kind of work.
- **Main method:** the guide named after its own folder, such as `stats_methods/regression/regression`. It holds the plan, data preparation, checks, interpretation, presentation and methodology rules shared by everything under it, and says which sub-method to read.
- **Sub-method:** only what is specific to one technique: code, extra checks, interpretation and its headline formula. Read it only after its main method.

Read only the guides the question needs.

## general/ — descriptive work and linking data

- `general/general`: counts, shares, means, cross-tabs, distributions, outliers, margins of error and record previews. Read it for any question that describes data without a statistical test or model, and before any stats method that also needs descriptive tables. Example: How does fuel type vary by region?
- `general/cross_folder_joins`: when the question needs records or summaries from different folders linked. Example: Compare two folders' state-level statistics by matching their state names.

## stats_methods/ — inference, models and machine learning

Go here when the question asks whether a difference or relationship is real, how strong it is after adjusting for other factors, what groups exist, or how well something can be predicted.

| Main method | Read when the question asks… | Sub-methods |
|---|---|---|
| `stats_methods/hypothesis_correlation/hypothesis_correlation` | whether groups differ significantly, whether two categorical variables are associated, or how strongly two variables move together. Example: Does mean spending differ between urban and rural households? | `tests`, `correlation` |
| `stats_methods/regression/regression` | how an outcome relates to several predictors at once ("controlling for", "adjusted"), or for a simple, interpretable prediction model. Example: Which characteristics are associated with energy saving after accounting for income? | `linear`, `logistic`, `penalized` (lasso, ridge, elastic net) |
| `stats_methods/clustering/clustering` | what natural groups or segments exist, with no outcome given. Example: What kinds of households are there by energy use and dwelling? | `kmeans`, `hierarchical`, `density`, `mixture` |
| `stats_methods/classical_ml/classical_ml` | how well an outcome can be predicted or classified, which characteristics predict it best, or for non-linear patterns regression would miss. Example: How well can we predict solar adoption? | `random_forest`, `gradient_boosting`, `decision_tree`, `knn`, `svm`, `naive_bayes`, `neural_net` |

Choosing between neighbours:
- "Is it significant?" between two or a few groups → hypothesis_correlation. "Is it significant after accounting for X?" → regression.
- Explanation with coefficients → regression. Predictive accuracy or many non-linear predictors → classical_ml, which also runs a regression baseline.
- An outcome is given → regression or classical_ml. No outcome, just "find groups" → clustering.

Rules for every stats method:
- Compute all the metrics the method lists. Report at most three that give the main overview, and flag any other metric only when it shows a problem.
- Run the method and flag weaknesses, unless a result is impossible.
- Use 95% confidence unless the user asks otherwise.
