# Decision tree

Apply the main classical_ml guidance first.

## When it fits

Use it when the user needs readable rules ("households with X and Y are most likely to …"). Single trees are unstable and usually less accurate than forests or boosting, so report a forest's performance alongside when accuracy matters.

## Fit

- `DecisionTreeClassifier` or `DecisionTreeRegressor(max_depth=d, min_samples_leaf=m, random_state=0)`.
- Choose `max_depth` ∈ {2, 3, 4} and `min_samples_leaf` of at least 1–2% of rows by cross-validation. Alternatively, prune with `ccp_alpha` from `cost_complexity_pruning_path`, choosing the largest alpha within one standard error of the best CV score.
- Keep the depth at 4 or less so the rules stay readable.
- Export the rules with `sklearn.tree.export_text(model, feature_names=...)`.

## Checks specific to trees

- **Instability:** refit on 5 bootstrap samples. If the top split changes, say the exact rules are fragile, even if accuracy is similar.
- **Leaf sizes:** report n per leaf. Flag leaves with fewer than about 30 rows.

## Interpret and present

- Present the leaves as a table: Rule (conditions in plain words) | n | % of sample | predicted class share or mean outcome. Sort by outcome. Translate thresholds into units ("built before 1960", not "year_built ≤ 1959.5").
- Wording: "Older homes heated by oil form the highest-use group (n = 412), averaging 22,000 kWh a year."

## Headline criterion

- Each split chooses the predictor and cut-point that most reduce impurity: Gini `G = 1 − Σₖ pₖ²` for classification, or squared error for regression. Notation: pₖ is the share of class k in the node.

References: [scikit-learn trees](https://scikit-learn.org/stable/modules/tree.html) · [Hastie, Tibshirani & Friedman, Elements of Statistical Learning, ch. 9](https://hastie.su.domains/ElemStatLearn/)
