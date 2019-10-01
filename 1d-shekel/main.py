import sys
import numpy as np
import tensorflow as tf
import matplotlib.pyplot as plt

np.random.seed(1111)
tf.random.set_seed(1111)

eqnPath = "1d-shekel"
sys.path.append(eqnPath)
sys.path.append("utils")
from pod import get_pod_bases, prep_data, scarcify
from nn import NeuralNetwork
from logger import Logger
from shekelutils import plot_results

# HYPER PARAMETERS

if len(sys.argv) > 1:
    with open(sys.argv[1]) as hpFile:
        hp = json.load(hpFile)
else:
    hp = {}
    # Space (dx = 1/30, n_e = 10/dx)
    hp["n_e"] = 300
    # Snapshots count
    hp["n_t"] = 10000
    # Train/Val repartition
    hp["train_test_ratio"] = 0.5
    # POD stopping param
    hp["eps"] = 1e-10
    # Setting up the TF SGD-based optimizer (set tf_epochs=0 to cancel it)
    hp["tf_epochs"] = 100000
    hp["tf_lr"] = 0.001
    hp["tf_decay"] = 0.
    hp["tf_b1"] = 0.9
    hp["tf_eps"] = None
    hp["lambda"] = 1e-6
    hp["log_frequency"] = 1000
    # Shekel params
    hp["bet_count"] = 10
    hp["gam_count"] = 10

# Getting the POD bases, with u_L(x, mu) = V.u_rb(x, mu) ~= u_h(x, mu)
# u_rb are the reduced coefficients we're looking for
U_h, X_U_rb_star = prep_data(hp["n_e"], hp["n_t"], hp["bet_count"], hp["gam_count"])
V = get_pod_bases(U_h, hp["n_e"], hp["n_t"], hp["eps"])

# Sizes
n_L = V.shape[1]
n_d = X_U_rb_star.shape[1]

# Projecting
U_rb_star = (V.T.dot(U_h)).T

# Splitting data
n_t_train = int(hp["train_test_ratio"] * hp["n_t"])
X_U_rb_train, U_rb_train, X_U_rb_test, U_rb_test = \
        scarcify(X_U_rb_star, U_rb_star, n_t_train)

# Creating the neural net model, and logger
# In: (gam_0, gam_1, gam_2)
# Out: u_rb = (u_rb_1, u_rb_2, ..., u_rb_L)
hp["layers"] = [n_d, 10, 20, n_L]
logger = Logger(hp)
model = NeuralNetwork(hp, logger)

# Setting the error function
def error():
    U_rb_pred = model.predict(X_U_rb_test)
    return 1/U_rb_pred.shape[0] * tf.reduce_sum(tf.square(U_rb_pred - U_rb_test))
logger.set_error_fn(error)

# Training
model.fit(X_U_rb_train, U_rb_train)

# Predicting the coefficients
U_rb_pred = model.predict(X_U_rb_test)
print(f"Error calculated on n_t_train = {n_t_train} samples" +
      f" ({int(100 * hp['train_test_ratio'])}%)")

# Retrieving the function with the predicted coefficients
U_h_pred = V.dot(U_rb_pred.T)

# Plotting and saving the results
plot_results(X_U_rb_test, U_rb_test, U_rb_pred, U_h, U_h_pred, hp)
plot_results(X_U_rb_test, U_rb_test, U_rb_pred, U_h, U_h_pred, hp, eqnPath)
