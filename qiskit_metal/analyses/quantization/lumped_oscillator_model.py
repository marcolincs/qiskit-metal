# -*- coding: utf-8 -*-

# This code is part of Qiskit.
#
# (C) Copyright IBM 2017, 2021.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

import pandas as pd
from pint import UnitRegistry

from pyEPR.calcs.convert import Convert

from qiskit_metal.designs import QDesign  # pylint: disable=unused-import
from . import LumpedElementsSim

from ... import Dict
from ... import config

if not config.is_building_docs():
    from .lumped_capacitive import extract_transmon_coupled_Noscillator


# TODO: eliminate every reference to "renderer" in this file
class LOManalysis(LumpedElementsSim):
    """Performs Lumped Oscillator Model analysis on a simulated or user-provided capacitance matrix.

    Default Setup:
        * junctions (Dict)
            * Lj (float): Junction inductance (in nH)
            * Cj (float): Junction capacitance (in fF)
        * freq_readout (float): Coupling readout frequency (in GHz).
        * freq_bus (Union[list, float]): Coupling bus frequencies (in GHz).
            * freq_bus can be a list with the order they appear in the capMatrix.

    Data Labels:
        * lumped_oscillator (pd.DataFrame): Lumped oscillator result at the last simulation pass
        * lumped_oscillator_all (dict): of pd.DataFrame. Lumped oscillator resulting
            at every pass of the simulation

    """
    default_setup = Dict(lom=Dict(
        junctions=Dict(Lj=12, Cj=2), freq_readout=7.0, freq_bus=[6.0, 6.2]))
    """Default setup."""

    # supported labels for data generated from the simulation
    data_labels = ['lumped_oscillator', 'lumped_oscillator_all']
    """Default data labels."""

    def __init__(self, design: 'QDesign', renderer_name: str = 'q3d'):
        """Initialize the Lumped Oscillator Model analysis.

        Args:
            design (QDesign): Pointer to the main qiskit-metal design.
                Used to access the QRenderer.
            renderer_name (str, optional): Which renderer to use. Defaults to 'q3d'.
        """
        # set design and renderer
        super().__init__(design, renderer_name)

    @property
    def lumped_oscillator(self) -> dict:
        """Getter

        Returns:
            dict: Lumped oscillator result at the last simulation pass.
        """
        return self.get_data('lumped_oscillator')

    @lumped_oscillator.setter
    def lumped_oscillator(self, data: dict):
        """Setter

        Args:
            data (dict): Lumped oscillator result at the last simulation pass.
        """
        if not isinstance(data, dict):
            self.logger.warning(
                'Unsupported type %s. Only accepts dict. Please try again.',
                {type(data)})
            return
        self.set_data('lumped_oscillator', data)

    @property
    def lumped_oscillator_all(self) -> pd.DataFrame:
        """Getter

        Returns:
            pd.DataFrame: each line corresponds to a simulation pass number
                and the remainder of the data is the respective lump oscillator information.
        """
        return self.get_data('lumped_oscillator_all')

    @lumped_oscillator_all.setter
    def lumped_oscillator_all(self, data: pd.DataFrame):
        """Setter

        Args:
            data (pd.DataFrame): each line corresponds to a simulation pass number
                and the remainder of the data is the respective lump oscillator information.
        """
        if not isinstance(data, pd.DataFrame):
            self.logger.warning(
                'Unsupported type %s. Only accepts pd.DataFrame. Please try again.',
                {type(data)})
            return
        self.set_data('lumped_oscillator_all', data)

    def run(self, *args, **kwargs):
        """Executes sequentially the system capacitance simulation and lom extraction by
        executing the methods LumpedElementsSim.run_sim(`*args`, `**kwargs`) and LOManalysis.run_lom().
        For input parameter, see documentation for LumpedElementsSim.run_sim().

        Returns:
            (dict): Pass numbers (keys) and respective lump oscillator information (values).
        """
        self.run_sim(*args, **kwargs)
        return self.run_lom()

    def run_lom(self):
        """Executes the lumped oscillator extraction from the capacitance matrix,
        and based on the setup values.

        Returns:
            dict: Pass numbers (keys) and their respective capacitance matrices (values).
        """
        # wipe data from the previous run (if any)
        self.clear_data(self.data_labels)

        s = self.setup.lom

        if self.capacitance_matrix is None:
            self.logger.warning(
                'Please initialize the capacitance_matrix before executing this method.'
            )
            return
        if not self.capacitance_all_passes:
            self.capacitance_all_passes[1] = self.capacitance_matrix.values

        ureg = UnitRegistry()
        ic_amps = Convert.Ic_from_Lj(s.junctions.Lj, 'nH', 'A')
        cj = ureg(f'{s.junctions.Cj} fF').to('farad').magnitude
        fread = ureg(f'{s.freq_readout} GHz').to('GHz').magnitude
        fbus = [ureg(f'{freq} GHz').to('GHz').magnitude for freq in s.freq_bus]

        # derive number of coupling pads
        num_cpads = 2
        if isinstance(fread, list):
            num_cpads += len(fread) - 1
        if isinstance(fbus, list):
            num_cpads += len(fbus) - 1

        # get the LOM for every pass
        all_res = {}
        for idx_cmat, df_cmat in self.capacitance_all_passes.items():
            res = extract_transmon_coupled_Noscillator(
                df_cmat,
                ic_amps,
                cj,
                num_cpads,
                fbus,
                fread,
                g_scale=1,
                print_info=bool(idx_cmat == len(self.capacitance_all_passes)))
            all_res[idx_cmat] = res
        self.lumped_oscillator = all_res[len(self.capacitance_all_passes)]
        all_res = pd.DataFrame(all_res).transpose()
        all_res['χr MHz'] = abs(all_res['chi_in_MHz'].apply(lambda x: x[0]))
        all_res['gr MHz'] = abs(all_res['gbus'].apply(lambda x: x[0]))
        self.lumped_oscillator_all = all_res
        return self.lumped_oscillator_all

    def plot_convergence(self, *args, **kwargs):
        """Plots alpha and frequency versus pass number, as well as convergence of delta (in %).

        It accepts the same inputs as run_lom(), to allow regenerating the LOM
        results before plotting them.
        """
        if self.lumped_oscillator_all is None or args or kwargs:
            self.run_lom(*args, **kwargs)
        # TODO: remove analysis plots from pyEPR and move it here
        self.renderer.plot_convergence_main(self.lumped_oscillator_all)

    def plot_convergence_chi(self, *args, **kwargs):
        """Plot convergence of chi and g, both in MHz, as a function of pass number.

        It accepts the same inputs as run_lom(), to allow regenerating the LOM
        results before plotting them.
        """
        if self.lumped_oscillator_all is None or args or kwargs:
            self.run_lom(*args, **kwargs)
        self.renderer.plot_convergence_chi(self.lumped_oscillator_all)