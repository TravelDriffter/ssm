class _AutoRegressiveObservationsBase(Observations):
    """
    Base class for autoregressive observations of the form,

    E[x_t | x_{t-1}, z_t=k, u_t]
        = \sum_{l=1}^{L} A_k^{(l)} x_{t-l} + b_k + V_k u_t.

    where L is the number of lags and u_t is the input.
    """
    def __init__(self, K, D, M=0, lags=1):
        super(_AutoRegressiveObservationsBase, self).__init__(K, D, M)

        # Distribution over initial point
        self.mu_init = np.zeros((K, D))

        # AR parameters
        assert lags > 0
        self.lags = lags
        self.bs = npr.randn(K, D)
        self.Vs = npr.randn(K, D, M)

        # Inheriting classes may treat _As differently
        self._As = None

    @property
    def As(self):
        return self._As

    @As.setter
    def As(self, value):
        self._As = value

    @property
    def params(self):
        return self.As, self.bs, self.Vs

    @params.setter
    def params(self, value):
        self.As, self.bs, self.Vs = value

    def permute(self, perm):
        self.mu_init = self.mu_init[perm]
        self.As = self.As[perm]
        self.bs = self.bs[perm]
        self.Vs = self.Vs[perm]

    def _compute_mus(self, data, input, mask, tag):
        # assert np.all(mask), "ARHMM cannot handle missing data"
        K, M = self.K, self.M
        T, D = data.shape
        As, bs, Vs, mu0s = self.As, self.bs, self.Vs, self.mu_init

        # Instantaneous inputs
        mus = np.empty((K, T, D))
        mus = []
        for k, (A, b, V, mu0) in enumerate(zip(As, bs, Vs, mu0s)):
            # Initial condition
            mus_k_init = mu0 * np.ones((self.lags, D))

            # Subsequent means are determined by the AR process
            mus_k_ar = np.dot(input[self.lags:, :M], V.T)
            for l in range(self.lags):
                Al = A[:, l*D:(l + 1)*D]
                mus_k_ar = mus_k_ar + np.dot(data[self.lags-l-1:-l-1], Al.T)
            mus_k_ar = mus_k_ar + b

            # Append concatenated mean
            mus.append(np.vstack((mus_k_init, mus_k_ar)))

        return np.array(mus)

    def smooth(self, expectations, data, input, tag):
        """
        Compute the mean observation under the posterior distribution
        of latent discrete states.
        """
        T = expectations.shape[0]
        mask = np.ones((T, self.D), dtype=bool)
        mus = np.swapaxes(self._compute_mus(data, input, mask, tag), 0, 1)
        return (expectations[:, :, None] * mus).sum(1)


class AutoRegressiveObservations(_AutoRegressiveObservationsBase):
    """
    AutoRegressive observation model with Gaussian noise.

        (x_t | z_t = k, u_t) ~ N(A_k x_{t-1} + b_k + V_k u_t, S_k)

    where S_k is a positive definite covariance matrix.

    The parameters are fit via maximum likelihood estimation.
    """
    def __init__(self, K, D, M=0, lags=1,
                 l2_penalty_A=1e-8,
                 l2_penalty_b=1e-8,
                 l2_penalty_V=1e-8):
        super(AutoRegressiveObservations, self).\
            __init__(K, D, M, lags=lags)

        # Initialize the dynamics and the noise covariances
        self._As = .95 * np.array([
                np.column_stack([random_rotation(D), np.zeros((D, (lags-1) * D))])
            for _ in range(K)])

        self._sqrt_Sigmas_init = np.tile(np.eye(D)[None, ...], (K, 1, 1))
        self._sqrt_Sigmas = npr.randn(K, D, D)

        # Regularization penalties on A, b, and V
        self.l2_penalty_A = l2_penalty_A
        self.l2_penalty_b = l2_penalty_b
        self.l2_penalty_V = l2_penalty_V


    @property
    def Sigmas_init(self):
        return np.matmul(self._sqrt_Sigmas_init, np.swapaxes(self._sqrt_Sigmas_init, -1, -2))

    @property
    def Sigmas(self):
        return np.matmul(self._sqrt_Sigmas, np.swapaxes(self._sqrt_Sigmas, -1, -2))

    @Sigmas.setter
    def Sigmas(self, value):
        assert value.shape == (self.K, self.D, self.D)
        self._sqrt_Sigmas = np.linalg.cholesky(value)

    @property
    def params(self):
        return super(AutoRegressiveObservations, self).params + (self._sqrt_Sigmas,)

    @params.setter
    def params(self, value):
        self._sqrt_Sigmas = value[-1]
        super(AutoRegressiveObservations, self.__class__).params.fset(self, value[:-1])

    def permute(self, perm):
        super(AutoRegressiveObservations, self).permute(perm)
        self._sqrt_Sigmas = self._sqrt_Sigmas[perm]

    @ensure_args_are_lists
    def initialize(self, datas, inputs=None, masks=None, tags=None, localize=True):
        from sklearn.linear_model import LinearRegression

        # Sample time bins for each discrete state
        # Use the data to cluster the time bins if specified.
        Ts = [data.shape[0] for data in datas]
        if localize:
            from sklearn.cluster import KMeans
            km = KMeans(self.K)
            km.fit(np.vstack(datas))
            zs = np.split(km.labels_, np.cumsum(Ts)[:-1])
            zs = [z[:-self.lags] for z in zs]               # remove the ends
        else:
            zs = [npr.choice(self.K, size=T-self.lags) for T in Ts]

        # Initialize the weights with linear regression
        Sigmas = []
        for k in range(self.K):
            ts = [np.where(z == k)[0] for z in zs]
            Xs = [np.column_stack([data[t + l] for l in range(self.lags)] + [input[t, :self.M]])
                  for t, data, input in zip(ts, datas, inputs)]
            ys = [data[t+self.lags] for t, data in zip(ts, datas)]

            # Solve the linear regression
            coef_, intercept_, Sigma = fit_linear_regression(Xs, ys)
            self.As[k] = coef_[:, :self.D * self.lags]
            self.Vs[k] = coef_[:, self.D * self.lags:]
            self.bs[k] = intercept_
            Sigmas.append(Sigma)

        # Set the variances all at once to use the setter
        self.Sigmas = np.array(Sigmas)

    def log_likelihoods(self, data, input, mask, tag=None):
        assert np.all(mask), "Cannot compute likelihood of autoregressive obsevations with missing data."
        L = self.lags
        mus = self._compute_mus(data, input, mask, tag)

        # Compute the likelihood of the initial data and remainder separately
        # stats.multivariate_studentst_logpdf supports broadcasting, but we get
        # significant performance benefit if we call it with (TxD), (D,), (D,D), and (,)
        # arrays as inputs
        ll_init = np.column_stack([stats.multivariate_normal_logpdf(data[:L], mu[:L], Sigma)
                               for mu, Sigma in zip(mus, self.Sigmas_init)])

        ll_ar = np.column_stack([stats.multivariate_normal_logpdf(data[L:], mu[L:], Sigma)
                               for mu, Sigma in zip(mus, self.Sigmas)])

        return np.row_stack((ll_init, ll_ar))

    def m_step(self, expectations, datas, inputs, masks, tags, J0=None, h0=None):
        K, D, M, lags = self.K, self.D, self.M, self.lags

        # Collect all the data
        xs, ys, Ezs = [], [], []
        for e, data, input, mask, tag in zip(expectations, datas, inputs, masks, tags):
            # Only use data if it is complete
            if not np.all(mask):
                raise Exception("Encountered missing data in AutoRegressiveObservations!")

            xs.append(
                np.hstack([data[self.lags-l-1:-l-1] for l in range(self.lags)]
                          + [input[self.lags:, :self.M], np.ones((data.shape[0]-self.lags, 1))]))
            ys.append(data[self.lags:])
            Ezs.append(e["Ez"][self.lags:])

        # M step: Fit the weighted linear regressions for each K and D
        if J0 is None and h0 is None:
            J_diag = np.concatenate((self.l2_penalty_A * np.ones(D * lags),
                                 self.l2_penalty_V * np.ones(M),
                                 self.l2_penalty_b * np.ones(1)))
            J = np.tile(np.diag(J_diag)[None, :, :], (K, 1, 1))
            h = np.zeros((K, D * lags + M + 1, D))
        else:
            assert J0.shape == (K, D*lags + M + 1, D*lags + M + 1)
            assert h0.shape == (K, D*lags + M + 1, D)
            J = J0
            h = h0

        J_diag = np.concatenate((self.l2_penalty_A * np.ones(D * lags),
                                 self.l2_penalty_V * np.ones(M),
                                 self.l2_penalty_b * np.ones(1)))
        J = np.tile(np.diag(J_diag)[None, :, :], (K, 1, 1))
        h = np.zeros((K, D * lags + M + 1, D))
        for x, y, Ez in zip(xs, ys, Ezs):
            # Einsum is concise but slow!
            # J += np.einsum('tk, ti, tj -> kij', Ez, x, x)
            # h += np.einsum('tk, ti, td -> kid', Ez, x, y)
            # Do weighted products for each of the k states
            for k in range(K):
                weighted_x = x * Ez[:, k:k+1]
                J[k] += np.dot(weighted_x.T, x)
                h[k] += np.dot(weighted_x.T, y)

        mus = np.linalg.solve(J, h)
        self.As = np.swapaxes(mus[:, :D*lags, :], 1, 2)
        self.Vs = np.swapaxes(mus[:, D*lags:D*lags+M, :], 1, 2)
        self.bs = mus[:, -1, :]

        # Update the covariance
        sqerr = np.zeros((K, D, D))
        weight = 1e-8 * np.ones(K)
        for x, y, Ez in zip(xs, ys, Ezs):
            yhat = np.matmul(x[None, :, :], mus)
            resid = y[None, :, :] - yhat
            sqerr += np.einsum('tk,kti,ktj->kij', Ez, resid, resid)
            weight += np.sum(Ez, axis=0)
        Sigmas = sqerr / weight[:, None, None] + 1e-8 * np.eye(D)

        # If any states are unused, set their parameters to a perturbation of a used state
        usage = sum([Ez.sum(0) for Ez in Ezs])
        unused = np.where(usage < 1)[0]
        used = np.where(usage > 1)[0]
        if len(unused) > 0:
            for k in unused:
                i = npr.choice(used)
                self.As[k] = self.As[i] + 0.01 * npr.randn(*self.As[i].shape)
                self.Vs[k] = self.Vs[i] + 0.01 * npr.randn(*self.Vs[i].shape)
                self.bs[k] = self.bs[i] + 0.01 * npr.randn(*self.bs[i].shape)
                Sigmas[k] = Sigmas[i]

        # Store the updated covariances
        self.Sigmas = Sigmas

    def sample_x(self, z, xhist, input=None, tag=None, with_noise=True):
        D, As, bs, Vs = self.D, self.As, self.bs, self.Vs

        if xhist.shape[0] < self.lags:
            # Sample from the initial distribution
            S = np.linalg.cholesky(self.Sigmas_init[z]) if with_noise else 0
            return self.mu_init[z] + np.dot(S, npr.randn(D))
        else:
            # Sample from the autoregressive distribution
            mu = Vs[z].dot(input[:self.M]) + bs[z]
            for l in range(self.lags):
                Al = As[z][:,l*D:(l+1)*D]
                mu += Al.dot(xhist[-l-1])

            S = np.linalg.cholesky(self.Sigmas[z]) if with_noise else 0
            return mu + np.dot(S, npr.randn(D))

    def hessian_expected_log_dynamics_prob(self, Ez, data, input, mask, tag=None):
        assert np.all(mask), "Cannot compute Hessian of autoregressive obsevations with missing data."
        assert self.lags == 1, "Does not compute Hessian of autoregressive observations with lags > 1"
        T = data.shape[0]
        K = self.K
        D = self.D

        # diagonal blocks, size ((T, D, D))
        diagonal_blocks = np.zeros((T, D, D))

        # initial distribution contributes a Gaussian term to first diagonal block
        diagonal_blocks[0] = -1 * np.sum(Ez[0, :, None, None] * np.linalg.inv(self.Sigmas_init), axis=0)

        # first part is transition dynamics - goes to all terms except final one
        # E_q(z) x_{t} A_{z_t+1}.T Sigma_{z_t+1}^{-1} A_{z_t+1} x_{t}
        inv_Sigmas = np.linalg.inv(self.Sigmas)
        dynamics_terms = np.array([A.T@inv_Sigma@A for A, inv_Sigma in zip(self.As, inv_Sigmas)]) # A^T Qinv A terms
        diagonal_blocks[:-1] += -1 * np.sum(Ez[1:,:,None,None] * dynamics_terms[None,:], axis=1)

        # second part of diagonal blocks are inverse covariance matrices - goes to all but first time bin
        # E_q(z) x_{t+1} Sigma_{z_t+1}^{-1} x_{t+1}
        diagonal_blocks[1:] += -1 * np.sum(Ez[1:,:,None,None] * inv_Sigmas[None,:], axis=1)

        # lower diagonal blocks are (T-1,D,D):
        # E_q(z) x_{t+1} Sigma_{z_t+1}^{-1} A_{z_t+1} x_t
        off_diag_terms = np.array([inv_Sigma@A for A, inv_Sigma in zip(self.As, inv_Sigmas)])
        lower_diagonal_blocks = np.sum(Ez[1:,:,None,None] * off_diag_terms[None,:], axis=1)

        return diagonal_blocks, lower_diagonal_blocks


class AutoRegressiveObservationsNoInput(AutoRegressiveObservations):
    """
    AutoRegressive observation model without the inputs.
    """
    def __init__(self, K, D, M=0, lags=1,
                 l2_penalty_A=1e-8,
                 l2_penalty_b=1e-8):

        super(AutoRegressiveObservationsNoInput, self).\
            __init__(K, D, M=0, lags=lags,
                     l2_penalty_A=l2_penalty_A,
                     l2_penalty_b=l2_penalty_b)


class AutoRegressiveDiagonalNoiseObservations(AutoRegressiveObservations):
    """
    AutoRegressive observation model with diagonal Gaussian noise.

        (x_t | z_t = k, u_t) ~ N(A_k x_{t-1} + b_k + V_k u_t, S_k)

    where

        S_k = diag([sigma_{k,1}, ..., sigma_{k, D}])

    The parameters are fit via maximum likelihood estimation.
    """
    def __init__(self, K, D, M=0, lags=1,
                 l2_penalty_A=1e-8,
                 l2_penalty_b=1e-8,
                 l2_penalty_V=1e-8):

        super(AutoRegressiveDiagonalNoiseObservations, self).\
            __init__(K, D, M, lags=lags,
                     l2_penalty_A=l2_penalty_A,
                     l2_penalty_b=l2_penalty_b,
                     l2_penalty_V=l2_penalty_V)

        # Initialize the dynamics and the noise covariances
        self._As = .95 * np.array([
                np.column_stack([random_rotation(D), np.zeros((D, (lags-1) * D))])
            for _ in range(K)])

        # Get rid of the square root parameterization and replace with log diagonal
        del self._sqrt_Sigmas_init
        del self._sqrt_Sigmas
        self._log_sigmasq_init = np.zeros((K, D))
        self._log_sigmasq = np.zeros((K, D))

    @property
    def sigmasq_init(self):
        return np.exp(self._log_sigmasq_init)

    @sigmasq_init.setter
    def sigmasq_init(self, value):
        assert value.shape == (self.K, self.D)
        assert np.all(value > 0)
        self._log_sigmasq_init = np.log(value)

    @property
    def sigmasq(self):
        return np.exp(self._log_sigmasq)

    @sigmasq.setter
    def sigmasq(self, value):
        assert value.shape == (self.K, self.D)
        assert np.all(value > 0)
        self._log_sigmasq = np.log(value)

    @property
    def Sigmas_init(self):
        return np.array([np.diag(np.exp(log_s)) for log_s in self._log_sigmasq_init])

    @property
    def Sigmas(self):
        return np.array([np.diag(np.exp(log_s)) for log_s in self._log_sigmasq])

    @Sigmas.setter
    def Sigmas(self, value):
        assert value.shape == (self.K, self.D, self.D)
        sigmasq = np.array([np.diag(S) for S in value])
        assert np.all(sigmasq > 0)
        self._log_sigmasq = np.log(sigmasq)

    @property
    def params(self):
        return super(AutoRegressiveObservations, self).params + (self._log_sigmasq,)

    @params.setter
    def params(self, value):
        self._log_sigmasq = value[-1]
        super(AutoRegressiveObservations, self.__class__).params.fset(self, value[:-1])

    def permute(self, perm):
        super(AutoRegressiveObservations, self).permute(perm)
        self._log_sigmasq_init = self._log_sigmasq_init[perm]
        self._log_sigmasq = self._log_sigmasq[perm]

    def log_likelihoods(self, data, input, mask, tag):
        assert np.all(mask), "Cannot compute likelihood of autoregressive obsevations with missing data."

        L = self.lags
        mus = self._compute_mus(data, input, mask, tag)

        # Compute the likelihood of the initial data and remainder separately
        # stats.multivariate_studentst_logpdf supports broadcasting, but we get
        # significant performance benefit if we call it with (TxD), (D,), (D,D), and (,)
        # arrays as inputs
        ll_init = np.column_stack([stats.diagonal_gaussian_logpdf(data[:L], mu[:L], sigmasq)
                               for mu, sigmasq in zip(mus, self.sigmasq_init)])

        ll_ar = np.column_stack([stats.diagonal_gaussian_logpdf(data[L:], mu[L:], sigmasq)
                               for mu, sigmasq in zip(mus, self.sigmasq)])


        # Compute the likelihood of the initial data and remainder separately
        # ll_init = stats.diagonal_gaussian_logpdf(data[:L, None, :], mus[:L], self.sigmasq_init)
        # ll_ar = stats.diagonal_gaussian_logpdf(data[L:, None, :], mus[L:], self.sigmasq)
        return np.row_stack((ll_init, ll_ar))


class IndependentAutoRegressiveObservations(_AutoRegressiveObservationsBase):
    def __init__(self, K, D, M=0, lags=1):
        super(IndependentAutoRegressiveObservations, self).__init__(K, D, M, lags=lags)

        self._As = np.concatenate((.95 * np.ones((K, D, 1)), np.zeros((K, D, lags-1))), axis=2)
        self._log_sigmasq_init = np.zeros((K, D))
        self._log_sigmasq = np.zeros((K, D))

    @property
    def sigmasq_init(self):
        return np.exp(self._log_sigmasq_init)

    @property
    def sigmasq(self):
        return np.exp(self._log_sigmasq)

    @property
    def As(self):
        return np.array([
                np.column_stack([np.diag(Ak[:,l]) for l in range(self.lags)])
            for Ak in self._As
        ])

    @As.setter
    def As(self, value):
        # TODO: extract the diagonal components
        raise NotImplementedError

    @property
    def params(self):
        return self._As, self.bs, self.Vs, self._log_sigmasq

    @params.setter
    def params(self, value):
        self._As, self.bs, self.Vs, self._log_sigmasq = value

    def permute(self, perm):
        self.mu_init = self.mu_init[perm]
        self._As = self._As[perm]
        self.bs = self.bs[perm]
        self.Vs = self.Vs[perm]
        self._log_sigmasq_init = self._log_sigmasq_init[perm]
        self._log_sigmasq = self._log_sigmasq[perm]

    @ensure_args_are_lists
    def initialize(self, datas, inputs=None, masks=None, tags=None):
        # Initialize with linear regressions
        from sklearn.linear_model import LinearRegression
        data = np.concatenate(datas)
        input = np.concatenate(inputs)
        T = data.shape[0]

        for k in range(self.K):
            for d in range(self.D):
                ts = npr.choice(T-self.lags, replace=False, size=(T-self.lags)//self.K)
                x = np.column_stack([data[ts + l, d:d+1] for l in range(self.lags)] + [input[ts, :self.M]])
                y = data[ts+self.lags, d:d+1]
                lr = LinearRegression().fit(x, y)

                self.As[k, d] = lr.coef_[:, :self.lags]
                self.Vs[k, d] = lr.coef_[:, self.lags:self.lags+self.M]
                self.bs[k, d] = lr.intercept_

                resid = y - lr.predict(x)
                sigmas = np.var(resid, axis=0)
                self._log_sigmasq[k, d] = np.log(sigmas + 1e-16)

    def _compute_mus(self, data, input, mask, tag):
        """
        Re-implement compute_mus for this class since we can do it much
        more efficiently than in the general AR case.
        """
        T, D = data.shape
        As, bs, Vs = self.As, self.bs, self.Vs

        # Instantaneous inputs, lagged data, and bias
        mus = np.matmul(Vs[None, ...], input[self.lags:, None, :self.M, None])[:, :, :, 0]
        for l in range(self.lags):
            mus += As[:, :, l] * data[self.lags-l-1:-l-1, None, :]
        mus += bs

        # Pad with the initial condition
        mus = np.concatenate((self.mu_init * np.ones((self.lags, self.K, self.D)), mus))

        assert mus.shape == (T, self.K, D)
        return mus

    def log_likelihoods(self, data, input, mask, tag):
        mus = self._compute_mus(data, input, mask, tag)

        # Compute the likelihood of the initial data and remainder separately
        L = self.lags
        ll_init = stats.diagonal_gaussian_logpdf(data[:L, None, :], mus[:L], self.sigmasq_init)
        ll_ar = stats.diagonal_gaussian_logpdf(data[L:, None, :], mus[L:], self.sigmasq)
        return np.row_stack((ll_init, ll_ar))

    def m_step(self, expectations, datas, inputs, masks, tags, **kwargs):
        D, M = self.D, self.M

        for d in range(self.D):
            # Collect data for this dimension
            xs, ys, weights = [], [], []
            for e, data, input, mask in zip(expectations, datas, inputs, masks):
                # Only use data if it is complete
                if np.all(mask[:, d]):
                    xs.append(
                        np.hstack([data[self.lags-l-1:-l-1, d:d+1] for l in range(self.lags)]
                                  + [input[self.lags:, :M], np.ones((data.shape[0]-self.lags, 1))]))
                    ys.append(data[self.lags:, d])
                    weights.append(e["Ez"][self.lags:])

            xs = np.concatenate(xs)
            ys = np.concatenate(ys)
            weights = np.concatenate(weights)

            # If there was no data for this dimension then skip it
            if len(xs) == 0:
                self.As[:, d, :] = 0
                self.Vs[:, d, :] = 0
                self.bs[:, d] = 0
                continue

            # Otherwise, fit a weighted linear regression for each discrete state
            for k in range(self.K):
                # Check for zero weights (singular matrix)
                if np.sum(weights[:, k]) < self.lags + M + 1:
                    self.As[k, d] = 1.0
                    self.Vs[k, d] = 0
                    self.bs[k, d] = 0
                    self._log_sigmasq[k, d] = 0
                    continue

                # Solve for the most likely A,V,b (no prior)
                Jk = np.sum(weights[:, k][:, None, None] * xs[:,:,None] * xs[:, None,:], axis=0)
                hk = np.sum(weights[:, k][:, None] * xs * ys[:, None], axis=0)
                muk = np.linalg.solve(Jk, hk)

                self.As[k, d] = muk[:self.lags]
                self.Vs[k, d] = muk[self.lags:self.lags+M]
                self.bs[k, d] = muk[-1]

                # Update the variances
                yhats = xs.dot(np.concatenate((self.As[k, d], self.Vs[k, d], [self.bs[k, d]])))
                sqerr = (ys - yhats)**2
                sigma = np.average(sqerr, weights=weights[:, k], axis=0) + 1e-16
                self._log_sigmasq[k, d] = np.log(sigma)

    def sample_x(self, z, xhist, input=None, tag=None, with_noise=True):
        D, As, bs, sigmas = self.D, self.As, self.bs, self.sigmasq

        # Sample the initial condition
        if xhist.shape[0] < self.lags:
            sigma_init = self.sigmasq_init[z] if with_noise else 0
            return self.mu_init[z] + np.sqrt(sigma_init) * npr.randn(D)

        # Otherwise sample the AR model
        muz = bs[z].copy()
        for lag in range(self.lags):
            muz += As[z, :, lag] * xhist[-lag - 1]

        sigma = sigmas[z] if with_noise else 0
        return muz + np.sqrt(sigma) * npr.randn(D)


# Robust autoregressive models with diagonal Student's t noise
class _RobustAutoRegressiveObservationsMixin(object):
    """
    Mixin for AR models where the noise is distributed according to a
    multivariate t distribution,

        epsilon ~ t(0, Sigma, nu)

    which is equivalent to,

        tau ~ Gamma(nu/2, nu/2)
        epsilon | tau ~ N(0, Sigma / tau)

    We use this equivalence to perform the M step (update of Sigma and tau)
    via an inner expectation maximization algorithm.

    This mixin mus be used in conjunction with either AutoRegressiveObservations or
    AutoRegressiveDiagonalNoiseObservations, which provides the parameterization for
    Sigma.  The mixin does not capitalize on structure in Sigma, so it will pay
    a small complexity penalty when used in conjunction with the diagonal noise model.
    """
    def __init__(self, K, D, M=0, lags=1,
                 l2_penalty_A=1e-8,
                 l2_penalty_b=1e-8,
                 l2_penalty_V=1e-8):

        super(_RobustAutoRegressiveObservationsMixin, self).\
            __init__(K, D, M=M, lags=lags,
                     l2_penalty_A=l2_penalty_A,
                     l2_penalty_b=l2_penalty_b,
                     l2_penalty_V=l2_penalty_V)
        self._log_nus = np.log(4) * np.ones(K)

    @property
    def nus(self):
        return np.exp(self._log_nus)

    @property
    def params(self):
        return super(_RobustAutoRegressiveObservationsMixin, self).params + (self._log_nus,)

    @params.setter
    def params(self, value):
        self._log_nus = value[-1]
        super(_RobustAutoRegressiveObservationsMixin, self.__class__).params.fset(self, value[:-1])

    def permute(self, perm):
        super(_RobustAutoRegressiveObservationsMixin, self).permute(perm)
        self._log_nus = self._log_nus[perm]

    def log_likelihoods(self, data, input, mask, tag):
        assert np.all(mask), "Cannot compute likelihood of autoregressive obsevations with missing data."
        mus = self._compute_mus(data, input, mask, tag)

        # Compute the likelihood of the initial data and remainder separately
        L = self.lags
        # Compute the likelihood of the initial data and remainder separately
        # stats.multivariate_studentst_logpdf supports broadcasting, but we get
        # significant performance benefit if we call it with (TxD), (D,), (D,D), and (,)
        # arrays as inputs
        ll_init = np.column_stack([stats.multivariate_normal_logpdf(data[:L], mu[:L], Sigma)
                               for mu, Sigma in zip(mus, self.Sigmas_init)])

        ll_ar = np.column_stack([stats.multivariate_studentst_logpdf(data[L:], mu[L:], Sigma, nu)
                               for mu, Sigma, nu in zip(mus, self.Sigmas, self.nus)])

        return np.row_stack((ll_init, ll_ar))

    def m_step(self, expectations, datas, inputs, masks, tags, num_em_iters=1, J0=None, h0=None):
        """
        Student's t is a scale mixture of Gaussians.  We can estimate its
        parameters using the EM algorithm. See the notebook in doc/students_t
        for complete details.
        """
        self._m_step_ar(expectations, datas, inputs, masks, tags, num_em_iters, J0=J0, h0=h0)
        self._m_step_nu(expectations, datas, inputs, masks, tags)

    def _m_step_ar(self, expectations, datas, inputs, masks, tags, num_em_iters, J0=None, h0=None):
        K, D, M, lags = self.K, self.D, self.M, self.lags

        # Collect data for this dimension
        xs, ys, Ezs = [], [], []
        for e, data, input, mask, tag in zip(expectations, datas, inputs, masks, tags):
            # Only use data if it is complete
            if not np.all(mask):
                raise Exception("Encountered missing data in AutoRegressiveObservations!")

            xs.append(
                np.hstack([data[lags-l-1:-l-1] for l in range(lags)]
                          + [input[lags:, :self.M], np.ones((data.shape[0]-lags, 1))]))
            ys.append(data[lags:])
            Ezs.append(e["Ez"][lags:])

        for itr in range(num_em_iters):
            # E Step: compute expected precision for each data point given current parameters
            taus = []
            for x, y in zip(xs, ys):
                Afull = np.concatenate((self.As, self.Vs, self.bs[:, :, None]), axis=2)
                mus = np.matmul(Afull[None, :, :, :], x[:, None, :, None])[:, :, :, 0]

                # nu: (K,)  mus: (T, K, D)  sigmas: (K, D, D)  y: (T, D)  -> tau: (T, K)
                alpha = self.nus / 2 + D/2
                sqrt_Sigmas = np.linalg.cholesky(self.Sigmas)
                beta = self.nus / 2 + 1/2 * stats.batch_mahalanobis(sqrt_Sigmas, y[:, None, :] - mus)
                taus.append(alpha / beta)

            # M step: Fit the weighted linear regressions for each K and D
            # This is exactly the same as the M-step for the AutoRegressiveObservations,
            # but it has an extra scaling factor of tau applied to the weight.
            if J0 is None and h0 is None:
                J_diag = np.concatenate((self.l2_penalty_A * np.ones(D * lags),
                                 self.l2_penalty_V * np.ones(M),
                                 self.l2_penalty_b * np.ones(1)))
                J = np.tile(np.diag(J_diag)[None, :, :], (K, 1, 1))
                h = np.zeros((K, D * lags + M + 1, D))
            else:
                assert J0.shape == (K, D*lags + M + 1, D*lags + M + 1)
                assert h0.shape == (K, D*lags + M + 1, D)
                J = J0
                h = h0

            for x, y, Ez, tau in zip(xs, ys, Ezs, taus):
                weight = Ez * tau
                # Einsum is concise but slow!
                # J += np.einsum('tk, ti, tj -> kij', weight, x, x)
                # h += np.einsum('tk, ti, td -> kid', weight, x, y)
                # Do weighted products for each of the k states
                for k in range(K):
                    weighted_x = x * weight[:, k:k+1]
                    J[k] += np.dot(weighted_x.T, x)
                    h[k] += np.dot(weighted_x.T, y)

            mus = np.linalg.solve(J, h)
            self.As = np.swapaxes(mus[:, :D*lags, :], 1, 2)
            self.Vs = np.swapaxes(mus[:, D*lags:D*lags+M, :], 1, 2)
            self.bs = mus[:, -1, :]

            # Update the covariance
            sqerr = np.zeros((K, D, D))
            weight = np.zeros(K)
            for x, y, Ez, tau in zip(xs, ys, Ezs, taus):
                yhat = np.matmul(x[None, :, :], mus)
                resid = y[None, :, :] - yhat
                sqerr += np.einsum('tk,kti,ktj->kij', Ez * tau, resid, resid)
                weight += np.sum(Ez, axis=0)

            self.Sigmas = sqerr / weight[:, None, None] + 1e-8 * np.eye(D)

    def _m_step_nu(self, expectations, datas, inputs, masks, tags):
        """
        Update the degrees of freedom parameter of the multivariate t distribution
        using a generalized Newton update. See notes in the ssm repo.
        """
        K, D, L = self.K, self.D, self.lags
        E_taus = np.zeros(K)
        E_logtaus = np.zeros(K)
        weights = np.zeros(K)
        for e, data, input, mask, tag in zip(expectations, datas, inputs, masks, tags):
            # nu: (K,)  mus: (T, K, D)  Sigmas: (K, D, D)  y: (T, D)  -> tau: (T, K)
            mus = np.swapaxes(self._compute_mus(data, input, mask, tag), 0, 1)

            alpha = self.nus/2 + D/2
            sqrt_Sigma = np.linalg.cholesky(self.Sigmas)
            # TODO: Performance could be improved by iterating over K outside batch_mahalanobis
            beta = self.nus/2 + 1/2 * stats.batch_mahalanobis(sqrt_Sigma, data[L:, None, :] - mus[L:])

            E_taus += np.sum(e["Ez"][L:, :] * alpha / beta, axis=0)
            E_logtaus += np.sum(e["Ez"][L:, :] * (digamma(alpha) - np.log(beta)), axis=0)
            weights += np.sum(e["Ez"], axis=0)

        E_taus /= weights
        E_logtaus /= weights

        for k in range(K):
            self._log_nus[k] = np.log(generalized_newton_studentst_dof(E_taus[k], E_logtaus[k]))

    def sample_x(self, z, xhist, input=None, tag=None, with_noise=True):
        D, As, bs, Vs, Sigmas, nus = self.D, self.As, self.bs, self.Vs, self.Sigmas, self.nus
        if xhist.shape[0] < self.lags:
            S = np.linalg.cholesky(self.Sigmas_init[z]) if with_noise else 0
            return self.mu_init[z] + np.dot(S, npr.randn(D))
        else:
            mu = Vs[z].dot(input[:self.M]) + bs[z]
            for l in range(self.lags):
                mu += As[z][:,l*D:(l+1)*D].dot(xhist[-l-1])

            tau = npr.gamma(nus[z] / 2.0, 2.0 / nus[z])
            S = np.linalg.cholesky(Sigmas[z] / tau) if with_noise else 0
            return mu + np.dot(S, npr.randn(D))


class RobustAutoRegressiveObservations(_RobustAutoRegressiveObservationsMixin, AutoRegressiveObservations):
    """
    AR model where the noise is distributed according to a multivariate t distribution,

        epsilon ~ t(0, Sigma, nu)

    which is equivalent to,

        tau ~ Gamma(nu/2, nu/2)
        epsilon | tau ~ N(0, Sigma / tau)

    Here, Sigma is a general covariance matrix.
    """
    pass


class RobustAutoRegressiveObservationsNoInput(RobustAutoRegressiveObservations):
    """
    RobusAutoRegressiveObservations model without the inputs.
    """
    def __init__(self, K, D, M=0, lags=1,
             l2_penalty_A=1e-8,
             l2_penalty_b=1e-8,
             l2_penalty_V=1e-8):

        super(RobustAutoRegressiveObservationsNoInput, self).\
            __init__(K, D, M=0, lags=lags,
                     l2_penalty_A=l2_penalty_A,
                     l2_penalty_b=l2_penalty_b,
                     l2_penalty_V=l2_penalty_V)



class RobustAutoRegressiveDiagonalNoiseObservations(
    _RobustAutoRegressiveObservationsMixin, AutoRegressiveDiagonalNoiseObservations):
    """
    AR model where the noise is distributed according to a multivariate t distribution,

        epsilon ~ t(0, Sigma, nu)

    which is equivalent to,

        tau ~ Gamma(nu/2, nu/2)
        epsilon | tau ~ N(0, Sigma / tau)

    Here, Sigma is a diagonal covariance matrix.
    """
    pass

# Robust autoregressive models with diagonal Student's t noise
class AltRobustAutoRegressiveDiagonalNoiseObservations(AutoRegressiveDiagonalNoiseObservations):
    """
    An alternative formulation of the robust AR model where the noise is
    distributed according to a independent scalar t distribution,

    For each output dimension d,

        epsilon_d ~ t(0, sigma_d^2, nu_d)

    which is equivalent to,

        tau_d ~ Gamma(nu_d/2, nu_d/2)
        epsilon_d | tau_d ~ N(0, sigma_d^2 / tau_d)

    """
    def __init__(self, K, D, M=0, lags=1):
        super(AltRobustAutoRegressiveDiagonalNoiseObservations, self).__init__(K, D, M=M, lags=lags)
        self._log_nus = np.log(4) * np.ones((K, D))

    @property
    def nus(self):
        return np.exp(self._log_nus)

    @property
    def params(self):
        return self.As, self.bs, self.Vs, self._log_sigmasq, self._log_nus

    @params.setter
    def params(self, value):
        self.As, self.bs, self.Vs, self._log_sigmasq, self._log_nus = value

    def permute(self, perm):
        super(AltRobustAutoRegressiveDiagonalNoiseObservations, self).permute(perm)
        self.inv_nus = self.inv_nus[perm]

    def log_likelihoods(self, data, input, mask, tag):
        assert np.all(mask), "Cannot compute likelihood of autoregressive obsevations with missing data."
        mus = np.swapaxes(self._compute_mus(data, input, mask, tag), 0, 1)

        # Compute the likelihood of the initial data and remainder separately
        L = self.lags
        ll_init = stats.diagonal_gaussian_logpdf(data[:L, None, :], mus[:L], self.sigmasq_init)
        ll_ar = stats.independent_studentst_logpdf(data[L:, None, :], mus[L:], self.sigmasq, self.nus)
        return np.row_stack((ll_init, ll_ar))

    def m_step(self, expectations, datas, inputs, masks, tags,
               num_em_iters=1, optimizer="adam", num_iters=10, **kwargs):
        """
        Student's t is a scale mixture of Gaussians.  We can estimate its
        parameters using the EM algorithm. See the notebook in doc/students_t
        for complete details.
        """
        self._m_step_ar(expectations, datas, inputs, masks, tags, num_em_iters)
        self._m_step_nu(expectations, datas, inputs, masks, tags, optimizer, num_iters, **kwargs)

    def _m_step_ar(self, expectations, datas, inputs, masks, tags, num_em_iters):
        K, D, M, lags = self.K, self.D, self.M, self.lags

        # Collect data for this dimension
        xs, ys, Ezs = [], [], []
        for e, data, input, mask, tag in zip(expectations, datas, inputs, masks, tags):
            # Only use data if it is complete
            if not np.all(mask):
                raise Exception("Encountered missing data in AutoRegressiveObservations!")

            xs.append(
                np.hstack([data[lags-l-1:-l-1] for l in range(lags)]
                          + [input[lags:, :self.M], np.ones((data.shape[0]-lags, 1))]))
            ys.append(data[lags:])
            Ezs.append(e["Ez"][lags:])

        for itr in range(num_em_iters):
            # E Step: compute expected precision for each data point given current parameters
            taus = []
            for x, y in zip(xs, ys):
                # mus = self._compute_mus(data, input, mask, tag)
                # sigmas = self._compute_sigmas(data, input, mask, tag)
                Afull = np.concatenate((self.As, self.Vs, self.bs[:, :, None]), axis=2)
                mus = np.matmul(Afull[None, :, :, :], x[:, None, :, None])[:, :, :, 0]

                # nu: (K,D)  mus: (T, K, D)  sigmas: (K, D)  y: (T, D)  -> tau: (T, K, D)
                alpha = self.nus / 2 + 1/2
                beta = self.nus / 2 + 1/2 * (y[:, None, :] - mus)**2 / self.sigmasq
                taus.append(alpha / beta)

            # M step: Fit the weighted linear regressions for each K and D
            J = np.tile(np.eye(D * lags + M + 1)[None, None, :, :], (K, D, 1, 1))
            h = np.zeros((K, D,  D*lags + M + 1,))
            for x, y, Ez, tau in zip(xs, ys, Ezs, taus):
                robust_ar_statistics(Ez, tau, x, y, J, h)

            mus = np.linalg.solve(J, h)
            self.As = mus[:, :, :D*lags]
            self.Vs = mus[:, :, D*lags:D*lags+M]
            self.bs = mus[:, :, -1]

            # Fit the variance
            sqerr = 0
            weight = 0
            for x, y, Ez, tau in zip(xs, ys, Ezs, taus):
                yhat = np.matmul(x[None, :, :], np.swapaxes(mus, -1, -2))
                sqerr += np.einsum('tk, tkd, ktd -> kd', Ez, tau, (y - yhat)**2)
                weight += np.sum(Ez, axis=0)
            self._log_sigmasq = np.log(sqerr / weight[:, None] + 1e-16)

    def _m_step_nu(self, expectations, datas, inputs, masks, tags, optimizer, num_iters, **kwargs):
        K, D, L = self.K, self.D, self.lags
        E_taus = np.zeros((K, D))
        E_logtaus = np.zeros((K, D))
        weights = np.zeros(K)
        for e, data, input, mask, tag in zip(expectations, datas, inputs, masks, tags):
            # nu: (K,D)  mus: (T, K, D)  sigmas: (K, D)  y: (T, D)  -> w: (T, K, D)
            mus = np.swapaxes(self._compute_mus(data, input, mask, tag), 0, 1)

            alpha = self.nus/2 + 1/2
            beta = self.nus/2 + 1/2 * (data[L:, None, :] - mus[L:])**2 / self.sigmasq

            Ez = e["Ez"]
            E_taus += np.sum(Ez[L:, :, None] * alpha / beta, axis=0)
            E_logtaus += np.sum(Ez[L:, :, None] * (digamma(alpha) - np.log(beta)), axis=0)
            weights += np.sum(Ez, axis=0)

        E_taus /= weights[:, None]
        E_logtaus /= weights[:, None]

        for k in range(K):
            for d in range(D):
                self._log_nus[k, d] = np.log(generalized_newton_studentst_dof(E_taus[k, d], E_logtaus[k, d]))

    def sample_x(self, z, xhist, input=None, tag=None, with_noise=True):
        D, As, bs, sigmasq, nus = self.D, self.As, self.bs, self.sigmasq, self.nus
        if xhist.shape[0] < self.lags:
            sigma_init = self.sigmasq_init[z] if with_noise else 0
            return self.mu_init[z] + np.sqrt(sigma_init) * npr.randn(D)
        else:
            mu = bs[z].copy()
            for l in range(self.lags):
                mu += As[z][:,l*D:(l+1)*D].dot(xhist[-l-1])

            tau = npr.gamma(nus[z] / 2.0, 2.0 / nus[z])
            var = sigmasq[z] / tau if with_noise else 0
            return mu + np.sqrt(var) * npr.randn(D)
