
"""Module with a class defining an Artificial Neural Network."""

import os
import pickle
import tensorflow as tf
import tensorflow_probability as tfp
import numpy as np

from .logger import LoggerCallback

tfk = tf.keras
K = tf.keras.backend
tfd = tfp.distributions

NORM_NONE = "none"
NORM_MEANSTD = "meanstd"
NORM_CENTER = "center"
NORM_MINMAX = "minmax"

tfk = tf.keras
K = tf.keras.backend
tfd = tfp.distributions


class DenseVariational(tfk.layers.Layer):
    """Bayesian Inference layer adapted from http://krasserm.github.io/2019/03/14/bayesian-neural-networks/"""
    def __init__(self,
                 units,
                 kl_weight,
                 activation=None,
                 prior_sigma_1=1.5,
                 prior_sigma_2=0.1,
                 prior_pi=0.5, **kwargs):
        self.units = units
        self.kl_weight = kl_weight
        self.activation = tfk.activations.get(activation)
        self.prior_sigma_1 = prior_sigma_1
        self.prior_sigma_2 = prior_sigma_2
        self.prior_pi = prior_pi
        self.prior_pi_1 = prior_pi
        self.prior_pi_2 = 1.0 - prior_pi
        self.init_sigma = np.sqrt(self.prior_pi_1 * self.prior_sigma_1 ** 2 +
                                  self.prior_pi_2 * self.prior_sigma_2 ** 2)

        super().__init__(**kwargs)

    def get_config(self):
        config = super().get_config().copy()
        config.update({
            'units': self.units,
            'kl_weight': self.kl_weight,
            'activation': self.activation,
            'prior_sigma_1': self.prior_sigma_1,
            'prior_sigma_2': self.prior_sigma_2,
            'prior_pi': self.prior_pi,
        })
        return config

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.units

    def build(self, input_shape):
        self.kernel_mu = self.add_weight(name='kernel_mu',
                                         shape=(input_shape[1], self.units),
                                         initializer=tfk.initializers.RandomNormal(stddev=self.tensor(self.init_sigma)),
                                         dtype=self.dtype,
                                         trainable=True)
        self.bias_mu = self.add_weight(name='bias_mu',
                                       shape=(self.units,),
                                       initializer=tfk.initializers.RandomNormal(stddev=self.tensor(self.init_sigma)),
                                       dtype=self.dtype,
                                       trainable=True)
        self.kernel_rho = self.add_weight(name='kernel_rho',
                                          shape=(input_shape[1], self.units),
                                          initializer=tfk.initializers.Constant(0.),
                                          dtype=self.dtype,
                                          trainable=True)
        self.bias_rho = self.add_weight(name='bias_rho',
                                        shape=(self.units,),
                                        initializer=tfk.initializers.Constant(0.),
                                        dtype=self.dtype,
                                        trainable=True)
        super().build(input_shape)

    def call(self, inputs, **kwargs):
        kernel_sigma = 1e-3 + tf.math.softplus(0.1 * self.kernel_rho)
        kernel = self.kernel_mu + kernel_sigma * tf.random.normal(self.kernel_mu.shape, dtype=self.dtype)
        bias_sigma = 1e-3 + tf.math.softplus(0.1 * self.bias_rho)
        bias = self.bias_mu + bias_sigma * tf.random.normal(self.bias_mu.shape, dtype=self.dtype)

        self.add_loss(self.kl_loss(kernel, self.kernel_mu, kernel_sigma) +
                      self.kl_loss(bias, self.bias_mu, bias_sigma))

        return self.activation(K.dot(inputs, kernel) + bias)

    def kl_loss(self, w, mu, sigma):
        variational_dist = tfp.distributions.Normal(mu, sigma)
        return self.kl_weight * K.sum(variational_dist.log_prob(w) - self.log_prior_prob(w))

    def log_prior_prob(self, w):
        comp_1_dist = tfp.distributions.Normal(0.0, self.tensor(self.prior_sigma_1))
        comp_2_dist = tfp.distributions.Normal(0.0, self.tensor(self.prior_sigma_2))
        c = np.log(np.expm1(1.))
        return K.log(c + self.prior_pi_1 * comp_1_dist.prob(w) +
                         self.prior_pi_2 * comp_2_dist.prob(w))

    def tensor(self, x):
        return tf.convert_to_tensor(x, dtype=self.dtype)


class BayesianNeuralNetwork:
    def __init__(self, layers, lr, klw, soft_0=0.01, sigma_alea=0.01, adv_eps=None, norm=NORM_NONE, model=None, norm_bounds=None):
        # Making sure the dtype is consistent
        self.dtype = "float64"
        tf.keras.backend.set_floatx(self.dtype)

        # Setting up optimizer and params
        self.optimizer = tf.optimizers.Adam(learning_rate=lr)
        self.layers = layers
        self.lr = lr
        self.klw = klw
        self.norm_bounds = norm_bounds
        self.logger = None
        self.batch_size = 0
        self.norm = norm
        self.adv_eps = adv_eps

        self.soft_0 = soft_0
        self.sigma_alea = sigma_alea

        # Setting up the model
        tf.keras.backend.set_floatx(self.dtype)
        if model is None:
            self.model = self.build_model()
        else:
            self.model = model

    def build_model(self):
        """Descriptive Keras model."""

        n_L = self.layers[-1]
        model = tfk.models.Sequential([
            tfk.layers.InputLayer(self.layers[0]),
            *[
            DenseVariational(
                units=width,
                activation="relu",
                kl_weight=self.klw,
                dtype=self.dtype,
            ) for width in self.layers[1:-1]],
            DenseVariational(
                units=n_L,
                activation="linear",
                dtype=self.dtype,
                kl_weight=self.klw,
            ),
            # tfp.layers.DistributionLambda(lambda t:
            #     tfd.MultivariateNormalDiag(
            #         loc=t[..., :n_L],
            #         scale_diag=1e-5 + tf.math.softplus(self.soft_0 * t[..., n_L:]),
            #     ),
            # ),
        ])

        return model

    @tf.function
    def loss(self, y_obs, y_pred):
        """Negative Log-Likelihood."""
        dist = tfp.distributions.Normal(loc=y_pred, scale=self.sigma_alea)
        return K.sum(-dist.log_prob(y_obs))

    @tf.function
    def grad(self, X, v):
        """Compute the loss and its derivatives w.r.t. the inputs."""
        with tf.GradientTape() as tape:
            loss_value = self.loss(v, self.model(X))
        grads = tape.gradient(loss_value, self.wrap_training_variables())
        return loss_value, grads

    def wrap_training_variables(self):
        return self.model.trainable_variables

    def set_normalize_bounds(self, X):
        if self.norm == NORM_CENTER or self.norm == NORM_MINMAX:
            lb = X.min(0)
            ub = X.max(0)
            self.norm_bounds = (lb, ub)
        elif self.norm == NORM_MEANSTD:
            lb = X.mean(0)
            ub = X.std(0)
            self.norm_bounds = (lb, ub)

    def normalize(self, X):
        if self.norm_bounds is None:
            return self.tensor(X)

        if self.norm == NORM_CENTER:
            lb, ub = self.norm_bounds
            X = (X - lb) - 0.5 * (ub - lb)
        elif self.norm == NORM_MEANSTD:
            mean, std = self.norm_bounds
            X = (X - mean) / std

        return self.tensor(X)

    def fit(self, X_v, v, epochs, logger=None, batch_size=None):
        """Train the model over a given dataset, and parameters."""
        # Setting up logger
        self.logger = logger
        callbacks = []
        if self.logger is not None:
            self.logger.log_train_start()

        # Normalizing and preparing inputs
        self.set_normalize_bounds(X_v)
        X_v = self.normalize(X_v)
        v = self.tensor(v)

        # Optimizing
        for e in range(epochs):
            loss_value, grads = self.grad(X_v, v)
            self.optimizer.apply_gradients(
                    zip(grads, self.wrap_training_variables()))
            if self.logger is not None:
                self.logger.log_train_epoch(e, loss_value)

        if self.logger is not None:
            self.logger.log_train_end(epochs, tf.constant(0., dtype=self.dtype))

    def predict_dist(self, X):
        """Get the prediction distribution for a new input X."""
        X = self.normalize(X)
        return tfp.distributions.Normal(loc=self.model(X), scale=self.sigma_alea)

    def predict(self, X, samples=200):
        """Get the prediction for a new input X, sampled many times."""
        X = self.normalize(X)
        y_pred_samples = np.zeros((samples, X.shape[0], self.layers[-1]))
        for i in range(samples):
            y_pred_samples[i, ...] = self.model(X).numpy()
        y_pred = y_pred_samples.mean(0)
        y_pred_var = y_pred_samples.var(0)
        return y_pred, y_pred_var

    def summary(self):
        """Print a summary of the TensorFlow/Keras model."""
        return self.model.summary()

    def tensor(self, X):
        """Convert input into a TensorFlow Tensor with the class dtype."""
        return X.astype("float64")
        # return tf.convert_to_tensor(X, dtype=self.dtype)

    def save_to(self, model_path, params_path):
        """Save the (trained) model and params for later use."""
        with open(params_path, "wb") as f:
            pickle.dump((self.layers, self.lr, self.klw, self.norm, self.norm_bounds), f)
        tf.keras.models.save_model(self.model, model_path)

    @classmethod
    def load_from(cls, model_path, params_path):
        """Load a (trained) model and params."""

        if not os.path.exists(model_path):
            raise FileNotFoundError("Can't find cached model.")
        if not os.path.exists(params_path):
            raise FileNotFoundError("Can't find cached model params.")

        print(f"Loading model from {model_path}")
        with open(params_path, "rb") as f:
            layers, lr, klw, norm, norm_bounds = pickle.load(f)
        print(f"Loading model params from {params_path}")
        model = tf.keras.models.load_model(model_path,
                    custom_objects={"DenseVariational": DenseVariational})
        return cls(layers, lr, klw, model=model, norm=norm, norm_bounds=norm_bounds)
