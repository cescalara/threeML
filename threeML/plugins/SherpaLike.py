import numpy as np
from sherpa.astro import datastack
from sherpa.models import TableModel
from threeML.pluginPrototype import pluginPrototype
from threeML.models.Parameter import Parameter

__instrument_name = "All OGIP compliant instruments"


class Likelihood2SherpaTableModel():
    """Creates from a 3ML Likelihhod model a table model that can be used in sherpa.
    It should be used to convert a threeML.models.LikelihoodModel
    into a sherpa.models.TableModel such that values are evaluated
    at the boundaries of the energy bins for the pha data for which one wants to calculate
    the likelihood.

    Parameters
    -----------
    likelihoodModel :  threeML.models.LikelihoodModel
    model
    """

    def __init__(self, likelihoodModel):
        self.likelihoodModel = likelihoodModel
        self.table_model = TableModel("table.source")

        # fetch energies
        self.e_lo = np.array(datastack.get_arf(1).energ_lo)
        self.e_hi = np.array(datastack.get_arf(1).energ_hi)

        # TODO figure out what to do if the binning is different across the datastack
        self.table_model._TableModel__x = self.e_lo  # according to Sherpa TableModel specs, TBV

        # determine which sources are inside the ON region
        self.onPtSrc = []  # list of point sources in the ON region
        nPtsrc = self.likelihoodModel.getNumberOfPointSources()
        for ipt in range(nPtsrc):
            # TODO check if source is in the ON region?
            self.onPtSrc.append(ipt)
        self.onExtSrc = []  # list of extended sources in the ON region
        nExtsrc = self.likelihoodModel.getNumberOfExtendedSources()
        if nExtsrc > 0:
            raise NotImplemented("Cannot support extended sources yet")

    def update(self):
        """Update the model values.
        """
        vals = np.zeros(len(self.table_model._TableModel__x))
        for ipt in self.onPtSrc:
            vals += [self.likelihoodModel.pointSources[ipt].spectralModel.photonFlux(bounds[0], bounds[1]) for bounds in
                     zip(self.e_lo, self.e_hi)]
            # integrated fluxes over same energy bins as for dataset, according to Sherpa TableModel specs, TBV
        self.table_model._TableModel__y = vals


class SherpaLike(pluginPrototype):
    """Generic plugin based on sherpa for data in OGIP format

    Parameters
    ----------
    name : str
    dataset name
    phalist : list of strings
    pha file names
    stat : str
    statistics to be used
    """

    def __init__(self, name, phalist, stat):
        # load data and set statistics
        self.name = name
        self.ds = datastack.DataStack()
        for phaname in phalist:
            self.ds.load_pha(phaname)
        # TODO add manual specs of bkg, arf, and rmf

        datastack.ui.set_stat(stat)

        # Effective area correction is disabled by default, i.e.,
        # the nuisance parameter is fixed to 1
        self.nuisanceParameters = {}
        self.nuisanceParameters['InterCalib'] = Parameter("InterCalib", 1, 0.9, 1.1, 0.01, fixed=True, nuisance=True)

    def setModel(self, likelihoodModel):
        """Set model for the source region

        Parameters
        ----------
        likelihoodModel : threeML.models.LikelihoodModel
        sky model for the source region
        """
        self.model = Likelihood2SherpaTableModel(likelihoodModel)
        self.model.update()  # to initialize values
        self.model.ampl = 1.
        self.ds.set_source(self.model.table_model)

    def _updateModel(self):
        """Updates the sherpa table model"""
        self.model.update()
        self.ds.set_source(self.model.table_model)

    def setEnergyRange(self,e_lo,e_hi):
        """Define an energy threshold for the fit
        which is different from the full range in the pha files

        Parameters
        ------------
        e_lo : float
        lower energy threshold in keV
        e_hi : float
        higher energy threshold in keV
        """
        self.ds.notice(e_lo,e_hi)

    def getLogLike(self):
        """Returns the current statistics value

        Returns
        -------------
        statval : float
        value of the statistics
        """
        self._updateModel()
        return -datastack.ui.calc_stat()

    def getName(self):
        """Return a name for this dataset set during the construction

        Returns:
        ----------
        name : str
        name of the dataset
        """
        return self.name

    def getNuisanceParameters(self):
        """Return a list of nuisance parameters.
        Return an empty list if there are no nuisance parameters.
        Not implemented yet.
        """
        # TODO implement nuisance parameters
        return self.nuisanceParameters.keys()

    def innerFit(self):
        """Inner fit. Just a hack to get it to work now.
        Will be removed.
        """
        # TODO remove once the inner fit requirement has been dropped
        return self.getLogLike()

    def display(self):
        """creates plots comparing data to model
        """
        datastack.ui.set_xlog()
        datastack.ui.set_ylog()
        self.ds.plot_data()
        self.ds.plot_model(overplot=True)
        # TODO see if possible to show model subcomponents
