import tensorflow as tf
import numpy as np
from utils import unroll_data
import keras
from keras.models import Sequential
from keras.layers import Dense, Activation, SimpleRNN, GRU, Dropout, LSTM, LeakyReLU
from keras import regularizers
from keras.optimizers import Adam
from scipy.stats import multivariate_normal

print("TensorFlow Version: {}".format(tf.__version__))
print("Keras      Version: {}".format(keras.__version__))

from keras.initializers import glorot_uniform  # Or your initializer of choice


def reset_weights(session, model):
    for layer in model.layers:
        if hasattr(layer, "kernel_initializer"):
            layer.kernel.initializer.run(session=session)


def map_variance(samples, df0, scale0):
    """
    This estimator assumes an scaled inverse-chi squared prior over the
    variance and a Gaussian likelihood. The parameters d and scale
    of the internal function parameterize the posterior of the variance.
    Taken from Bayesian Data Analysis, ch2 (Gelman)

    samples: N length array or NxD array
    df0: prior degrees of freedom
    scale0: prior scale parameter
    mu: (optional) mean function

    returns: float or d-length array, mode of the posterior
    """
    if np.ndim(samples) > 1:
        n, d = np.shape(samples)
    else:
        n = np.shape(samples)[0]
        d = 1

    v = np.var(samples, axis=0)
    df = df0 + n
    scale = (df0 * scale0 + n * v) / df
    return df * scale / (df * 2)


def kalman_update(x, z, P, A, Q, R):
    # time update steps
    z_hat_pre = A.dot(z)
    P_pre = A.dot(P).dot(A.T) + Q

    # measurement updates
    K = np.matmul(P_pre, np.linalg.inv(P_pre + R))
    z_hat = z_hat_pre + np.matmul(K, x - z_hat_pre)
    P = np.matmul((np.eye(np.shape(x)[0]) - K), P_pre)

    return z_hat, P


def kalman_prediction(z, A):
    return A.dot(z)


def kalman_log_prob(x, z, P, A, Q, R):
    z_hat_pre = A.dot(z)
    P_pre = A.dot(P).dot(A.T) + Q

    # combined measurement noise into covariance
    Sigma = np.linalg.inv(np.linalg.inv(P_pre) + np.linalg.inv(R))

    # evaluate
    return multivariate_normal.logpdf(x, mean=z_hat_pre, cov=Sigma)


class LinearEvent(object):
    """ this is the base clase of the event model """

    def __init__(self, d, var_df0, var_scale0, optimizer=None, n_epochs=10, init_model=False,
                 kernel_initializer='glorot_uniform', l2_regularization=0.00, batch_size=32, prior_log_prob=0.0,
                 reset_weights=False):
        """

        :param d: dimensions of the input space
        """
        self.d = d
        self.f_is_trained = False
        self.f0_is_trained = False
        self.f0 = np.zeros(d)

        self.x_history = [np.zeros((0, self.d))]
        # self.y_history = [np.zeros((0, self.d))]
        self.prior_probability = prior_log_prob

        if optimizer is None:
            optimizer = Adam(lr=0.01, beta_1=0.9, beta_2=0.999, epsilon=None, decay=0.0, amsgrad=False)

        self.compile_opts = dict(optimizer=optimizer, loss='mean_squared_error')
        self.kernel_initializer = kernel_initializer
        self.kernel_regularizer = regularizers.l2(l2_regularization)
        self.n_epochs = n_epochs
        self.batch_size = batch_size
        self.var_df0 = var_df0
        self.var_scale0 = var_scale0
        self.d = d
        self.reset_weights = reset_weights
        self.training_pairs = []
        self.prediction_errors = np.zeros((0, self.d), dtype=np.float)
        self.model_weights = None

        # initialize the covariance with the mode of the prior distribution
        self.Sigma = np.eye(d) * var_df0 * var_scale0 / (var_df0 + 2)

        self.is_visited = False  # governs the special case of model's first prediction (i.e. with no experience)

        # switch for inheritance -- don't want to init the model for sub-classes
        if init_model:
            self.init_model()

    def init_model(self):
        self._compile_model()
        self.model_weights = self.model.get_weights()
        return self.model

    def _compile_model(self):
        self.model = Sequential([
            Dense(self.d, input_shape=(self.d,), use_bias=True, kernel_initializer=self.kernel_initializer,
                  kernel_regularizer=self.kernel_regularizer),
            Activation('linear')
        ])
        self.model.compile(**self.compile_opts)

    def set_model(self, sess, model):
        self.sess = sess
        self.model = model
        self.do_reset_weights()

    def reestimate(self):
        self.do_reset_weights()
        self.estimate()

    def do_reset_weights(self):
        # self._compile_model()
        reset_weights(self.sess, self.model)
        self.model_weights = self.model.get_weights()

    def update(self, X, Y, update_estimate=True):
        """
        Parameters
        ----------
        X: NxD array-like data of inputs

        y: NxD array-like data of outputs

        Returns
        -------
        None

        """
        if X.ndim > 1:
            X = X[-1, :]  # only consider last example
        assert X.ndim == 1
        assert X.shape[0] == self.d
        assert Y.ndim == 1
        assert Y.shape[0] == self.d

        x_example = X.reshape((1, self.d))
        y_example = Y.reshape((1, self.d))

        # concatenate the training example to the active event token
        self.x_history[-1] = np.concatenate([self.x_history[-1], x_example], axis=0)
        # self.y_history[-1] = np.concatenate([self.y_history[-1], y_example], axis=0)

        # also, create a list of training pairs (x, y) for efficient sampling
        #  picks  random time-point in the history
        self.training_pairs.append(tuple([x_example, y_example]))

        if update_estimate:
            self.estimate()
            self.f_is_trained = True

    def update_f0(self, Y, update_estimate=True):
        self.update(np.zeros(self.d), Y, update_estimate=update_estimate)
        self.f0_is_trained = True

        # precompute f0 for speed
        self.f0 = self._predict_f0()

    def predict_next(self, X):
        """
        wrapper for the prediction function that changes the prediction to the identity function
        for untrained models (this is an initialization technique)

        """
        self.model.set_weights(self.model_weights)

        if not self.f_is_trained:
            if np.ndim(X) > 1:
                return np.copy(X[-1, :]).reshape(1, -1)
            return np.copy(X).reshape(1, -1)

        return self._predict_next(X)

    def _predict_next(self, X):
        """
        Parameters
        ----------
        X: 1xD array-like data of inputs

        Returns
        -------
        y: 1xD array of prediction vectors

        """
        if X.ndim > 1:
            X0 = X[-1, :]
        else:
            X0 = X

        return self.model.predict(np.reshape(X0, newshape=(1, self.d)))

    def predict_f0(self):
        """
        wrapper for the prediction function that changes the prediction to the identity function
        for untrained models (this is an initialization technique)

        N.B. This answer is cached for speed

        """
        return self.f0

    def _predict_f0(self):
        return self._predict_next(np.zeros(self.d))

    def log_likelihood_f0(self, Y):

        if not self.f0_is_trained:
            return self.prior_probability

        # predict the initial point
        Y_hat = self.predict_f0()

        # return the probability
        return multivariate_normal.logpdf(Y.reshape(-1), mean=Y_hat.reshape(-1), cov=self.Sigma)

    def log_likelihood_next(self, X, Y):
        Y_hat = self.predict_next(X)
        return multivariate_normal.logpdf(Y.reshape(-1), mean=Y_hat.reshape(-1), cov=self.Sigma)

    def log_likelihood_sequence(self, X, Y):
        Y_hat = self.predict_next_generative(X)
        return multivariate_normal.logpdf(Y.reshape(-1), mean=Y_hat.reshape(-1), cov=self.Sigma)

    # create a new cluster of scenes
    def new_token(self):
        if len(self.x_history) == 1 and self.x_history[0].shape[0] == 0:
            # special case for the first cluster which is already created
            return
        self.x_history.append(np.zeros((0, self.d)))
        # self.y_history.append(np.zeros((0, self.d)))

    def predict_next_generative(self, X):
        # the LDS is a markov model, so these functions are the same
        return self.predict_next(X)

    def run_generative(self, n_steps, initial_point=None):
        if initial_point is None:
            x_gen = self._predict_f0()
        else:
            x_gen = np.reshape(initial_point, (1, self.d))
        for ii in range(1, n_steps):
            x_gen = np.concatenate([x_gen, self.predict_next_generative(x_gen[:ii, :])])
        return x_gen

    def estimate(self):
        if self.reset_weights:
            self.do_reset_weights()
        else:
            self.model.set_weights(self.model_weights)

        # self.model.fit(self.x_train, self.y_train, epochs=self.n_epochs, verbose=0)
        n_pairs = len(self.training_pairs)

        def draw_sample_pair():
            # draw a random cluster for the history
            idx = np.random.randint(n_pairs)
            return self.training_pairs[idx]

        # run batch gradient descent on all of the past events!
        for _ in range(self.n_epochs):

            # draw a set of training examples from the history
            x_batch = []
            y_batch = []
            for _ in range(self.batch_size):

                x_sample, y_sample = draw_sample_pair()

                # these data aren't
                x_batch.append(x_sample)
                y_batch.append(y_sample)

            x_batch = np.reshape(x_batch, (self.batch_size, self.d))
            y_batch = np.reshape(y_batch, (self.batch_size, self.d))
            self.model.train_on_batch(x_batch, y_batch)

        # cache the model weights
        self.model_weights = self.model.get_weights()

        # Update Sigma
        x_train_0, y_train_0 = self.training_pairs[-1]
        y_hat = self.model.predict(x_train_0)
        self.prediction_errors = np.concatenate([self.prediction_errors, y_train_0 - y_hat], axis=0)
        if np.shape(self.prediction_errors)[0] > 1:
            self.Sigma = np.eye(self.d) * map_variance(self.prediction_errors, self.var_df0, self.var_scale0)


class NonLinearEvent(LinearEvent):

    def __init__(self, d, var_df0, var_scale0, n_hidden=None, hidden_act='tanh',
                 optimizer=None, n_epochs=10, init_model=False, kernel_initializer='glorot_uniform',
                 l2_regularization=0.00, dropout=0.50, prior_log_prob=0.0, reset_weights=False):
        LinearEvent.__init__(self, d, var_df0, var_scale0, optimizer=optimizer, n_epochs=n_epochs,
                             init_model=False, kernel_initializer=kernel_initializer,
                             l2_regularization=l2_regularization, prior_log_prob=prior_log_prob,
                             reset_weights=reset_weights)

        if n_hidden is None:
            n_hidden = d
        self.n_hidden = n_hidden
        self.hidden_act = hidden_act
        self.dropout = dropout

        if init_model:
            self.init_model()

    def _compile_model(self):
        self.model = Sequential()
        self.model.add(Dense(self.n_hidden, input_shape=(self.d,), activation=self.hidden_act,
                             kernel_regularizer=self.kernel_regularizer,
                             kernel_initializer=self.kernel_initializer))
        self.model.add(Dropout(self.dropout))
        self.model.add(Dense(self.d, activation='linear',
                             kernel_regularizer=self.kernel_regularizer,
                             kernel_initializer=self.kernel_initializer))
        self.model.compile(**self.compile_opts)


class StationaryEvent(LinearEvent):

    def _predict_next(self, X):
        """
        Parameters
        ----------
        X: 1xD array-like data of inputs

        Returns
        -------
        y: 1xD array of prediction vectors

        """

        return self.model.predict(np.zeros((1, self.d)))



class RecurentLinearEvent(LinearEvent):

    # RNN which is initialized once and then trained using stochastic gradient descent
    # i.e. each new scene is a single example batch of size 1

    def __init__(self, d, var_df0, var_scale0, t=3,
                 optimizer=None, n_epochs=10, l2_regularization=0.00, batch_size=32,
                 kernel_initializer='glorot_uniform', init_model=False, prior_log_prob=0.0, reset_weights=False):
        #
        # D = dimension of single input / output example
        # t = number of time steps to unroll back in time for the recurrent layer
        # n_hidden1 = # of nodes in first hidden layer
        # n_hidden2 = # of nodes in second hidden layer
        # hidden_act1 = activation f'n of first hidden layer
        # hidden_act2 = activation f'n of second hidden layer
        # sgd_kwargs = arguments for the stochastic gradient descent algorithm
        # n_epochs = how many gradient descent steps to perform for each training batch
        # dropout = what fraction of nodes to drop out during training (to prevent overfitting)

        LinearEvent.__init__(self, d, var_df0, var_scale0, optimizer=optimizer, n_epochs=n_epochs,
                             init_model=False, kernel_initializer=kernel_initializer,
                             l2_regularization=l2_regularization, prior_log_prob=prior_log_prob,
                             reset_weights=reset_weights)

        self.t = t
        self.n_epochs = n_epochs

        # list of clusters of scenes:
        # each element of list = history of scenes for given cluster
        # history = N x D tensor, N = # of scenes in cluster, D = dimension of single scene
        #
        self.x_history = [np.zeros((0, self.d))]
        self.batch_size = batch_size

        if init_model:
            self.init_model()

        # cache the initial weights for retraining speed
        self.init_weights = None

    def do_reset_weights(self):
        # # self._compile_model()
        if self.init_weights is None:
            for layer in self.model.layers:
                new_weights = [glorot_uniform()(w.shape).eval(session=self.sess) for w in layer.get_weights()]
                layer.set_weights(new_weights)
            self.model_weights = self.model.get_weights()
            self.init_weights = self.model.get_weights()
        else:
            self.model.set_weights(self.init_weights)

    # initialize model once so we can then update it online
    def _compile_model(self):
        self.model = Sequential()
        self.model.add(SimpleRNN(self.d, input_shape=(self.t, self.d),
                                 activation=None, kernel_initializer=self.kernel_initializer,
                                 kernel_regularizer=self.kernel_regularizer))
        self.model.compile(**self.compile_opts)

    # concatenate current example with the history of the last t-1 examples
    # this is for the recurrent layer
    #
    def _unroll(self, x_example):
        x_train = np.concatenate([self.x_history[-1][-(self.t - 1):, :], x_example], axis=0)
        x_train = np.concatenate([np.zeros((self.t - x_train.shape[0], self.d)), x_train], axis=0)
        x_train = x_train.reshape((1, self.t, self.d))
        return x_train

    # predict a single example
    def _predict_next(self, X):
        # Note: this function predicts the next conditioned on the training data the model has seen

        if X.ndim > 1:
            X = X[-1, :]  # only consider last example
        assert np.ndim(X) == 1
        assert X.shape[0] == self.d

        x_test = X.reshape((1, self.d))

        # concatenate current example with history of last t-1 examples
        # this is for the recurrent part of the network
        x_test = self._unroll(x_test)
        return self.model.predict(x_test)

    def _predict_f0(self):
        return self.predict_next_generative(np.zeros(self.d))

    def update(self, X, Y, update_estimate=True):
        if X.ndim > 1:
            X = X[-1, :]  # only consider last example
        assert X.ndim == 1
        assert X.shape[0] == self.d
        assert Y.ndim == 1
        assert Y.shape[0] == self.d

        x_example = X.reshape((1, self.d))
        y_example = Y.reshape((1, self.d))

        # concatenate the training example to the active event token
        self.x_history[-1] = np.concatenate([self.x_history[-1], x_example], axis=0)
        # self.y_history[-1] = np.concatenate([self.y_history[-1], y_example], axis=0)

        # also, create a list of training pairs (x, y) for efficient sampling
        #  picks  random time-point in the history
        _n = np.shape(self.x_history[-1])[0]
        x_train_example = np.reshape(
                    unroll_data(self.x_history[-1][max(_n - self.t, 0):, :], self.t)[-1, :, :], (1, self.t, self.d)
                )
        self.training_pairs.append(tuple([x_train_example, y_example]))

        if update_estimate:
            self.estimate()
            self.f_is_trained = True

    def predict_next_generative(self, X):
        X0 = np.reshape(unroll_data(X, self.t)[-1, :, :], (1, self.t, self.d))
        return self.model.predict(X0)

    # optional: run batch gradient descent on all past event clusters
    def estimate(self):
        if self.reset_weights:
            self.do_reset_weights()

        n_pairs = len(self.training_pairs)

        def draw_sample_pair():
            # draw a random cluster for the history
            idx = np.random.randint(n_pairs)
            return self.training_pairs[idx]

        # run batch gradient descent on all of the past events!
        for _ in range(self.n_epochs):

            # draw a set of training examples from the history
            x_batch = np.zeros((0, self.t, self.d))
            y_batch = np.zeros((0, self.d))
            for _ in range(self.batch_size):

                x_sample, y_sample = draw_sample_pair()

                x_batch = np.concatenate([x_batch, x_sample], axis=0)
                y_batch = np.concatenate([y_batch, y_sample], axis=0)

            self.model.train_on_batch(x_batch, y_batch)
        self.model_weights = self.model.get_weights()

        # Update Sigma
        x_train_0, y_train_0 = self.training_pairs[-1]
        y_hat = self.model.predict(x_train_0)
        self.prediction_errors = np.concatenate([self.prediction_errors, y_train_0 - y_hat], axis=0)
        if np.shape(self.prediction_errors)[0] > 1:
            self.Sigma = np.eye(self.d) * map_variance(self.prediction_errors, self.var_df0, self.var_scale0)


class RecurrentEvent(RecurentLinearEvent):

    def __init__(self, d, var_df0, var_scale0, t=3, n_hidden=None, hidden_act='tanh', optimizer=None,
                 n_epochs=10, dropout=0.50, l2_regularization=0.00, batch_size=32,
                 kernel_initializer='glorot_uniform', init_model=False, prior_log_prob=0.0, reset_weights=False):

        RecurentLinearEvent.__init__(self, d, var_df0, var_scale0, t=t, optimizer=optimizer, n_epochs=n_epochs,
                                     l2_regularization=l2_regularization, batch_size=batch_size,
                                     kernel_initializer=kernel_initializer, init_model=False, prior_log_prob=prior_log_prob,
                                     reset_weights=reset_weights)

        if n_hidden is None:
            self.n_hidden = d
        else:
            self.n_hidden = n_hidden
        self.hidden_act = hidden_act
        self.dropout = dropout

        if init_model:
            self.init_model()

    def _compile_model(self):
        self.model = Sequential()
        # input_shape[0] = timesteps; we pass the last self.t examples for train the hidden layer
        # input_shape[1] = input_dim; each example is a self.d-dimensional vector
        self.model.add(SimpleRNN(self.n_hidden, input_shape=(self.t, self.d), #activation=self.hidden_act,
                                 kernel_regularizer=self.kernel_regularizer,
                                 kernel_initializer=self.kernel_initializer))
        self.model.add(LeakyReLU(alpha=0.3))
        self.model.add(Dropout(self.dropout))
        self.model.add(Dense(self.d, activation=None, kernel_regularizer=self.kernel_regularizer,
                  kernel_initializer=self.kernel_initializer))
        self.model.compile(**self.compile_opts)



class GRUEvent(RecurentLinearEvent):

    def __init__(self, d, var_df0, var_scale0, t=3, n_hidden=None, hidden_act='tanh', optimizer=None,
                 n_epochs=10, dropout=0.50, l2_regularization=0.00, batch_size=32,
                 kernel_initializer='glorot_uniform', init_model=False, prior_log_prob=0.0, reset_weights=False):

        RecurentLinearEvent.__init__(self, d, var_df0, var_scale0, t=t, optimizer=optimizer, n_epochs=n_epochs,
                                     l2_regularization=l2_regularization, batch_size=batch_size,
                                     kernel_initializer=kernel_initializer, init_model=False,
                                     prior_log_prob=prior_log_prob, reset_weights=reset_weights)

        if n_hidden is None:
            self.n_hidden = d
        else:
            self.n_hidden = n_hidden
        self.hidden_act = hidden_act
        self.dropout = dropout

        if init_model:
            self.init_model()

    def _compile_model(self):
        self.model = Sequential()
        # input_shape[0] = timesteps; we pass the last self.t examples for train the hidden layer
        # input_shape[1] = input_dim; each example is a self.d-dimensional vector
        self.model.add(GRU(self.n_hidden, input_shape=(self.t, self.d), #activation=self.hidden_act,
                                 kernel_regularizer=self.kernel_regularizer,
                                 kernel_initializer=self.kernel_initializer))
        self.model.add(LeakyReLU(alpha=0.3))
        self.model.add(Dropout(self.dropout))
        self.model.add(Dense(self.d, activation=None, kernel_regularizer=self.kernel_regularizer,
                  kernel_initializer=self.kernel_initializer))
        self.model.compile(**self.compile_opts)


class LSTMEvent(RecurentLinearEvent):

    def __init__(self, d, var_df0, var_scale0, t=3, n_hidden=None, hidden_act='tanh', optimizer=None,
                 n_epochs=10, dropout=0.50, l2_regularization=0.00,
                 batch_size=32, kernel_initializer='glorot_uniform', init_model=False, prior_log_prob=0.0,
                 reset_weights=False):

        RecurentLinearEvent.__init__(self, d, var_df0, var_scale0, t=t, optimizer=optimizer, n_epochs=n_epochs,
                                     l2_regularization=l2_regularization, batch_size=batch_size,
                                     kernel_initializer=kernel_initializer, init_model=False,
                                     prior_log_prob=prior_log_prob, reset_weights=reset_weights)

        if n_hidden is None:
            self.n_hidden = d
        else:
            self.n_hidden = n_hidden
        self.hidden_act = hidden_act
        self.dropout = dropout

        if init_model:
            self.init_model()

    def _compile_model(self):
        self.model = Sequential()
        # input_shape[0] = timesteps; we pass the last self.t examples for train the hidden layer
        # input_shape[1] = input_dim; each example is a self.d-dimensional vector
        self.model.add(LSTM(self.n_hidden, input_shape=(self.t, self.d), #activation=self.hidden_act,
                           kernel_regularizer=self.kernel_regularizer,
                           kernel_initializer=self.kernel_initializer))
        self.model.add(LeakyReLU(alpha=0.3))
        self.model.add(Dropout(self.dropout))
        self.model.add(Dense(self.d, activation=None, kernel_regularizer=self.kernel_regularizer,
                             kernel_initializer=self.kernel_initializer))
        self.model.compile(**self.compile_opts)
