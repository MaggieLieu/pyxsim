from pyxsim import \
    TableApecModel, XSpecThermalModel
from yt.utilities.answer_testing.framework import \
    GenericArrayTest
from yt.testing import requires_module, fake_random_ds

def setup():
    from yt.config import ytcfg
    ytcfg["yt", "__withintesting"] = "True"

ds = fake_random_ds(64)
@requires_module("astropy")
def test_apec():

    amod = TableApecModel(0.1, 10.0, 10000, thermal_broad=True)
    amod.prepare_spectrum(0.2)

    acspec, amspec = amod.get_spectrum(6.0)
    spec1 = acspec+0.3*amspec

    def spec1_test():
        return spec1.v

    for test in [GenericArrayTest(ds, spec1_test)]:
        test_apec.__name__ = test.description
        yield test