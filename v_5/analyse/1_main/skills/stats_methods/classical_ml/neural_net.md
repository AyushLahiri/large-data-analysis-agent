# Simple neural networks (multilayer perceptron)

Apply the main classical_ml guidance first.

## What "simple" means here

A fully connected feed-forward network from scikit-learn (`MLPClassifier` / `MLPRegressor`), trained on the CPU inside the sandbox:

| Limit | Allowed |
|---|---|
| Hidden layers | 1 or 2 |
| Units per hidden layer | at most 64 |
| Total trainable weights | at most about 10,000, and fewer than the number of training rows |
| Training rows | up to about 100,000; subsample beyond that |
| Architectures not allowed | convolutional, recurrent, attention or deep networks, and anything needing a GPU or another framework |

These limits come from the sandbox (1 CPU, 1 GB memory, 240 seconds). On tabular data, tree ensembles usually match or beat neural networks, so always report a random forest or boosting comparison.

## Fit

- `Pipeline([('scale', StandardScaler()), ('model', MLPClassifier(hidden_layer_sizes=(32,), alpha=1e-3, early_stopping=True, validation_fraction=0.15, max_iter=500, random_state=0))])`.
- Scaling is mandatory. One-hot encode categorical predictors.
- Tune `hidden_layer_sizes` ∈ {(16,), (32,), (32, 16), (64, 32)} and `alpha` (L2 penalty) ∈ {1e-4, 1e-3, 1e-2} by cross-validation, keeping within the limits.
- The MLP does not accept sample weights. Evaluate with weighted metrics and say so.
- Count the weights: (inputs + 1)·h₁ + (h₁ + 1)·h₂ + (h₂ + 1)·outputs. Report the total.

## Checks specific to neural nets

- **Convergence:** if a `ConvergenceWarning` appears or `n_iter_` hits `max_iter`, raise `max_iter` or the learning rate. Report non-convergence if it persists.
- **Seed sensitivity:** fit with 3–5 random seeds and report the spread of the main metric. Large spread means unreliable results.
- **Overfitting:** compare training and validation scores. Increase `alpha` or reduce units if the gap is large.

## Interpret

- Weights are not interpretable. Use permutation importance and partial dependence.
- Wording: "A small neural network (one layer of 32 units) reached a balanced accuracy of 0.74, similar to the random forest (0.75), so the extra complexity does not help here."

## Headline model

- `ŷ = g(W₂ σ(W₁x + b₁) + b₂)`. Notation: σ is the ReLU activation max(0, z), W and b are the weights and biases, and g is the output link (identity for regression, logistic or softmax for classification). Training minimises the loss plus `α‖W‖²`.

References: [scikit-learn neural networks](https://scikit-learn.org/stable/modules/neural_networks_supervised.html) · [Grinsztajn et al. 2022, tree models on tabular data](https://arxiv.org/abs/2207.08815)
