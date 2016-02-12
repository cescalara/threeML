import emcee
import emcee.utils
import numpy
import collections
import re
from IPython.display import display
import uncertainties

from threeML.io.table import Table
from threeML.parallel.parallel_client import ParallelClient
from threeML.config.config import threeML_config
from threeML.io.progress_bar import ProgressBar
from threeML.io.triangle import corner
from threeML.exceptions.custom_exceptions import ModelAssertionViolation, LikelihoodIsInfinite, custom_warnings


def sample_with_progress(p0, sampler, n_samples, **kwargs):
    # Create progress bar

    progress = ProgressBar(n_samples)

    # Loop collecting n_samples samples

    pos, prob, state = [None, None, None]

    for i, result in enumerate(sampler.sample(p0, iterations=n_samples, **kwargs)):
        # Show progress

        progress.animate((i + 1))

        # Get the vectors with the results

        pos, prob, state = result

    # Make sure we show 100% completion

    progress.animate(n_samples)

    # Go to new line

    print("")

    return pos, prob, state


def sample_without_progress(p0, sampler, n_samples, **kwargs):
    return sampler.run_mcmc(p0, n_samples, **kwargs)


class BayesianAnalysis(object):

    def __init__(self, likelihood_model, data_list, **kwargs):
        """
        Bayesian analysis.

        :param likelihood_model: the likelihood model
        :param data_list: the list of datasets to use (normally an instance of DataList)
        :param kwargs: use 'verbose=True' for verbose operation
        :return:
        """

        # Process optional keyword parameters

        self.verbose = False

        for k, v in kwargs.iteritems():

            if k.lower() == "verbose":

                self.verbose = bool(kwargs["verbose"])

        self._likelihood_model = likelihood_model

        self.data_list = data_list

        # Make sure that the current model is used in all data sets

        for dataset in self.data_list.values():

            dataset.set_model(self._likelihood_model)

        # Init the samples to None

        self._samples = None
        self._raw_samples = None
        self._sampler = None

        # Get the initial list of free parameters, useful for debugging purposes

        self._free_parameters = self._likelihood_model.getFreeParameters()

    def sample(self, n_walkers, burn_in, n_samples):
        """
        Sample the posterior with the Goodman & Weare's Affine Invariant Markov chain Monte Carlo
        """

        self._update_free_parameters()

        n_dim = len(self._free_parameters.keys())

        # Get starting point

        p0 = self._get_starting_points(n_walkers)

        if threeML_config['parallel']['use-parallel']:

            c = ParallelClient()
            view = c[:]

            sampler = emcee.EnsembleSampler(n_walkers, n_dim,
                                            self._get_posterior,
                                            pool=view)

        else:

            sampler = emcee.EnsembleSampler(n_walkers, n_dim,
                                            self._get_posterior)

        print("Running burn-in of %s samples...\n" % burn_in)

        pos, prob, state = sample_with_progress(p0, sampler, burn_in)

        # Reset sampler

        sampler.reset()

        # Run the true sampling

        print("\nSampling...\n")

        _ = sample_with_progress(pos, sampler, n_samples, rstate0=state)

        acc = numpy.mean(sampler.acceptance_fraction)

        print("Mean acceptance fraction: %s" % acc)

        self._sampler = sampler
        self._raw_samples = sampler.flatchain

        self._build_samples_dictionary()

        return self.samples

    def sample_parallel_tempering(self, n_temps, n_walkers, burn_in, n_samples):
        """
        Sample with parallel tempering
        """

        free_parameters = self._likelihood_model.getFreeParameters()

        n_dim = len(free_parameters.keys())

        sampler = emcee.PTSampler(n_temps, n_walkers, n_dim, self._log_like, self._logp)

        # Get one starting point for each temperature

        p0 = numpy.empty((n_temps, n_walkers, n_dim))

        for i in range(n_temps):
            p0[i, :, :] = self._get_starting_points(n_walkers)

        print("Running burn-in of %s samples...\n" % burn_in)

        p, lnprob, lnlike = sample_with_progress(p0, sampler, burn_in)

        # Reset sampler

        sampler.reset()

        print("\nSampling...\n")

        _ = sample_with_progress(p, sampler, n_samples,
                                 lnprob0=lnprob, lnlike0=lnlike)

        self._sampler = sampler

        # Now build the _samples dictionary

        self._raw_samples = sampler.flatchain.reshape(-1, sampler.flatchain.shape[-1])

        self._build_samples_dictionary()

        return self.samples

    def _build_samples_dictionary(self):
        """
        Build the dictionary to access easily the samples by parameter

        :return: none
        """

        self._samples = collections.OrderedDict()

        for i, (src_name, param_name) in enumerate(self._free_parameters.keys()):

            # The first time we encounter a source we create a dictionary for that source

            if src_name not in self._samples.keys():

                self._samples[src_name] = collections.OrderedDict()

            # Add the samples for this parameter for this source

            self._samples[src_name][param_name] = self._raw_samples[:,i]

    @property
    def raw_samples(self):
        """
        Access the samples from the posterior distribution generated by the selected sampler in raw form (i.e.,
        in the format returned by the sampler)

        :return: the samples as returned by the sampler
        """

        return self._raw_samples

    @property
    def samples(self):
        """
        Access the samples from the posterior distribution generated by the selected sampler

        :return: a dictionary with the samples from the posterior distribution for each parameter
        """
        return self._samples

    @property
    def sampler(self):
        """
        Access the instance of the sampler used to sample the posterior distribution
        :return: an instance of the sampler
        """

        return self._sampler

    def get_credible_intervals(self, probability=95):
        """
        Print and returns the (equal-tail) credible intervals for all free parameters in the model

        :param probability: the probability for this credible interval (default: 95, corresponding to 95%)
        :return: a dictionary with the lower bound and upper bound of the credible intervals, as well as the median
        """

        # Gather the credible intervals (percentiles of the posterior)

        credible_intervals = collections.OrderedDict()

        for i, (src_name, param_name) in enumerate(self._free_parameters.keys()):

            # The first time we encounter a source we create a dictionary for that source

            if src_name not in credible_intervals.keys():

                credible_intervals[src_name] = collections.OrderedDict()

            # Get the percentiles from the posterior samples

            lower_bound,median,upper_bound = numpy.percentile(self.samples[src_name][param_name],
                                                              (100-probability,50,probability))

            # Save them in the dictionary

            credible_intervals[src_name][param_name] = {'lower bound': lower_bound,
                                                        'median': median,
                                                        'upper bound': upper_bound}

        # Print a table with the errors

        data = []
        name_length = 0

        for i, (src_name, param_name) in enumerate(self._free_parameters.keys()):

            current_name = "%s_of_%s" % (src_name, param_name)

            # Format the value and the error with sensible significant
            # numbers

            lower_bound, median, upper_bound = [credible_intervals[src_name][param_name][key] for key in ('lower bound',
                                                                                                          'median',
                                                                                                          'upper bound')
                                                ]

            # Process the negative "error"

            x = uncertainties.ufloat(median, abs(lower_bound - median))

            # Split the uncertainty in number, negative error, and exponent (if any)

            number, unc_lower_bound, exponent = re.match('\(?(\-?[0-9]+\.?[0-9]+) ([0-9]+\.[0-9]+)\)?(e[\+|\-][0-9]+)?',
                                           x.__str__().replace("+/-", " ")).groups()

            # Process the positive "error"

            x = uncertainties.ufloat(median, abs(upper_bound - median))

            # Split the uncertainty in number, positive error, and exponent (if any)

            _, unc_upper_bound, _ = re.match('\(?(\-?[0-9]+\.?[0-9]+) ([0-9]+\.[0-9]+)\)?(e[\+|\-][0-9]+)?',
                                  x.__str__().replace("+/-", " ")).groups()

            if exponent is None:

                # Number without exponent

                pretty_string = "%s -%s +%s" % (number, unc_lower_bound, unc_upper_bound)

            else:

                # Number with exponent

                pretty_string = "(%s -%s +%s)%s" % (number, unc_lower_bound, unc_upper_bound, exponent)

            unit = self._free_parameters[src_name, param_name].unit

            data.append([current_name, pretty_string, unit])

            if len(current_name) > name_length:
                name_length = len(current_name)

        # Create and display the table

        table = Table(rows=data,
                      names=["Name", "Value", "Unit"],
                      dtype=('S%i' % name_length, str, 'S15'))

        display(table)

        return credible_intervals


    def corner_plot(self, **kwargs):
        """
        Produce the corner plot showing the marginal distributions in one and two directions.

        :param kwargs: arguments to be passed to the corner function
        :return: a matplotlib.figure instance
        """

        if self.samples is not None:

            assert len(self._free_parameters.keys()) == self.raw_samples[0].shape[0], ("Mismatch between sample"
                                                                                       " dimensions and number of free"
                                                                                       " parameters")

            labels = []
            priors = []

            for i, (src_name, param_name) in enumerate(self._free_parameters.keys()):
                this_label = "%s of %s" % (param_name, src_name)

                labels.append(this_label)

                priors.append(self._likelihood_model.parameters[src_name][param_name].prior)

            fig = corner(self.raw_samples, labels=labels,
                         quantiles=[0.16, 0.50, 0.84],
                         priors=priors, **kwargs)

            return fig

        else:

            raise RuntimeError("You have to run the sampler first, using the sample() method")

    def _update_free_parameters(self):
        """
        Update the dictionary of the current free parameters
        :return:
        """

        self._free_parameters = self._likelihood_model.getFreeParameters()

    def _get_posterior(self, trial_values):
        """Compute the posterior for the normal sampler"""

        # Here we don't use the self._logp nor the self._logLike to
        # avoid looping twice over the parameters (for speed)

        # Assign this trial values to the parameters and
        # store the corresponding values for the priors

        self._update_free_parameters()

        assert len(self._free_parameters) == len(trial_values), ("Something is wrong. Number of free parameters "
                                                                 "do not match the number of trial values.")

        log_prior = 0

        for i, (src_name, param_name) in enumerate(self._free_parameters.keys()):

            this_param = self._likelihood_model.parameters[src_name][param_name]

            this_param.setValue(trial_values[i])

            pval = this_param.getPriorValue()

            if not numpy.isfinite(pval):
                # Outside allowed region of parameter space

                return -numpy.inf

            log_prior += pval

        # Get the value of the log-likelihood for this parameters

        try:

            # Loop over each dataset and get the likelihood values for each set

            log_like_values = map(lambda dataset: dataset.get_log_like(), self.data_list.values())

        except ModelAssertionViolation:

            # Fit engine or sampler outside of allowed zone

            return -numpy.inf

        except:

            # We don't want to catch more serious issues

            raise

        # Sum the values of the log-like

        log_like = numpy.sum(log_like_values)

        if not numpy.isfinite(log_like):
            # Issue warning

            custom_warnings.warn("Likelihood value is infinite for parameters %s" % trial_values, LikelihoodIsInfinite)

            return -numpy.inf

        return log_like + log_prior

    def _get_starting_points(self, n_walkers, variance=0.1):

        # Generate the starting points for the walkers by getting random
        # values for the parameters close to the current value

        # Fractional variance for randomization
        # (0.1 means var = 0.1 * value )

        p0 = []

        for i in range(n_walkers):

            this_p0 = []

            for (src_name, param_name) in self._free_parameters.keys():
                this_par = self._likelihood_model.parameters[src_name][param_name]

                this_val = this_par.getRandomizedValue(variance)

                this_p0.append(this_val)

            p0.append(this_p0)

        return p0

    def _logp(self, trial_values):
        """Compute the sum of log-priors, used in the parallel tempering sampling"""

        # Compute the sum of the log-priors

        logp = 0

        for i, (src_name, param_name) in enumerate(self._free_parameters.keys()):
            this_param = self._likelihood_model.parameters[src_name][param_name]

            logp += this_param.prior(trial_values[i])

        return logp

    def _log_like(self, trial_values):
        """Compute the log-likelihood, used in the parallel tempering sampling"""

        # Compute the log-likelihood

        # Set the parameters to their trial values

        for i, (src_name, param_name) in enumerate(self._free_parameters.keys()):
            this_param = self._likelihood_model.parameters[src_name][param_name]

            this_param.setValue(trial_values[i])

        # Get the value of the log-likelihood for this parameters

        try:

            # Loop over each dataset and get the likelihood values for each set

            log_like_values = map(lambda dataset: dataset.get_log_like(), self.data_list.values())

        except ModelAssertionViolation:

            # Fit engine or sampler outside of allowed zone

            return -numpy.inf

        except:

            # We don't want to catch more serious issues

            raise

        # Sum the values of the log-like

        log_like = numpy.sum(log_like_values)

        if not numpy.isfinite(log_like):
            # Issue warning

            custom_warnings.warn("Likelihood value is infinite for parameters %s" % trial_values, LikelihoodIsInfinite)

            return -numpy.inf

        return log_like
