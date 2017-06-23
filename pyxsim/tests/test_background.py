from pyxsim.source_generators.background import make_background
from pyxsim.spectral_models import TableApecModel, WabsModel
from pyxsim.instruments import ACIS_I
from yt.testing import requires_module
from soxs.spectra import ApecGenerator
import os
import tempfile
import shutil
import numpy as np
from sherpa.astro.ui import load_user_model, add_user_pars, \
    load_pha, ignore, fit, set_model, set_stat, set_method, \
    covar, get_covar_results, set_covar_opt
from soxs.instrument import RedistributionMatrixFile, \
    AuxiliaryResponseFile
from soxs.instrument_registry import get_instrument_from_registry

prng = 24

def setup():
    from yt.config import ytcfg
    ytcfg["yt", "__withintesting"] = "True"

acis_spec = get_instrument_from_registry("acisi_cy18")

rmf = RedistributionMatrixFile(acis_spec["rmf"])
arf = AuxiliaryResponseFile(acis_spec['arf'])

fit_model = TableApecModel(rmf.elo[0], rmf.ehi[-1], rmf.n_de, thermal_broad=False)

def mymodel(pars, x, xhi=None):
    tm = WabsModel(pars[0])
    tbabs = tm.get_absorb(x)
    bapec = fit_model.return_spectrum(pars[1], pars[2], pars[3], pars[4])
    return tbabs*bapec

@requires_module("sherpa")
def test_background():

    tmpdir = tempfile.mkdtemp()
    curdir = os.getcwd()
    os.chdir(tmpdir)

    kT_sim = 1.0
    Z_sim = 0.0
    norm_sim = 4.0e-2
    nH_sim = 0.04
    redshift = 0.01

    exp_time = (200., "ks")
    area = (1000., "cm**2")
    fov = (20.0, "arcmin")

    prng = 24

    agen = ApecGenerator(0.05, 12.0, 5000, broadening=False)
    spec = agen.get_spectrum(kT_sim, Z_sim, redshift, norm_sim)
    spec.apply_foreground_absorption(norm_sim)

    events = make_background(area, exp_time, fov, (30.0, 45.0), spec, prng=prng)

    new_events = ACIS_I(events, prng=prng)

    new_events.write_channel_spectrum("background_evt.pi", overwrite=True)

    os.system("cp %s %s ." % (arf.filename, rmf.filename))

    load_user_model(mymodel, "wapec")
    add_user_pars("wapec", ["nH", "kT", "metallicity", "redshift", "norm"],
                  [0.01, 4.0, 0.2, redshift, norm_sim*0.8],
                  parmins=[0.0, 0.1, 0.0, -20.0, 0.0],
                  parmaxs=[10.0, 20.0, 10.0, 20.0, 1.0e9],
                  parfrozen=[False, False, False, True, False])

    load_pha("background_evt.pi")
    set_stat("cstat")
    set_method("simplex")
    ignore(":0.5, 8.0:")
    set_model("wapec")
    fit()
    set_covar_opt("sigma", 1.6)
    covar()
    res = get_covar_results()

    assert np.abs(res.parvals[0]-nH_sim) < res.parmaxes[0]
    assert np.abs(res.parvals[1]-kT_sim) < res.parmaxes[1]
    assert np.abs(res.parvals[2]-Z_sim) < res.parmaxes[2]
    assert np.abs(res.parvals[3]-norm_sim) < res.parmaxes[3]

    os.chdir(curdir)
    shutil.rmtree(tmpdir)

if __name__ == "__main__":
    test_background()