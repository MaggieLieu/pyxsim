"""
Classes for generating lists of detected events
"""
import numpy as np
from pyxsim.utils import mylog
from yt.units.yt_array import YTQuantity, YTArray, uconcatenate
import astropy.io.fits as pyfits
import astropy.wcs as pywcs
import h5py
from pyxsim.utils import validate_parameters, parse_value
from soxs.simput import write_photon_list
from yt.utilities.parallel_tools.parallel_analysis_interface import \
    communication_system, parallel_capable, get_mpi_type

comm = communication_system.communicators[-1]


def communicate_events(my_events, root=0):
    if parallel_capable:
        new_events = {}
        mpi_int = get_mpi_type("int32")
        mpi_double = get_mpi_type("float64")
        local_num_events = my_events["xsky"].size
        sizes = comm.comm.gather(local_num_events, root=root)
        if comm.rank == 0:
            num_events = sum(sizes)
            disps = [sum(sizes[:i]) for i in range(len(sizes))]
            for key in my_events:
                new_events[key] = np.zeros(num_events)
        else:
            sizes = []
            disps = []
            for key in my_events:
                new_events[key] = np.empty([])
        for key in my_events:
            if key in ["pi", "pha"]:
                mpi_type = mpi_int
            else:
                mpi_type = mpi_double
            comm.comm.Gatherv([my_events[key], local_num_events, mpi_type],
                              [new_events[key], (sizes, disps), mpi_type], root=root)
            if key == "eobs":
                new_events[key] = YTArray(new_events[key], "keV")
            if key.endswith("sky"):
                new_events[key] = YTArray(new_events[key], "deg")
        return new_events
    else:
        return my_events


def _handle_simput(events, exp_time, area, emin, emax):

    if emin is None and emax is None:
        idxs = slice(None, None, None)
    else:
        if emin is None:
            emin = events["eobs"].min().value
        if emax is None:
            emax = events["eobs"].max().value
        idxs = np.logical_and(events["eobs"].d >= emin, events["eobs"].d <= emax)
    
    flux = np.sum(events["eobs"][idxs]).to("erg")/exp_time/area

    return flux.v, events["xsky"].d[idxs], events["ysky"].d[idxs], events["eobs"].d[idxs]


class EventList(object):

    def __init__(self, events, parameters):
        self.events = events
        self.parameters = parameters
        self.num_events = comm.mpi_allreduce(events["xsky"].shape[0])

    def keys(self):
        return self.events.keys()

    def has_key(self, key):
        return key in self.keys()

    def items(self):
        return self.events.items()

    def values(self):
        return self.events.values()

    def __getitem__(self,key):
        return self.events[key]

    def __repr__(self):
        return self.events.__repr__()

    def __contains__(self, key):
        return key in self.events

    def __add__(self, other):
        validate_parameters(self.parameters, other.parameters, skip=["sky_center"])
        events = {}
        for item1, item2 in zip(self.items(), other.items()):
            k1, v1 = item1
            k2, v2 = item2
            events[k1] = uconcatenate([v1,v2])
        return type(self)(events, dict(self.parameters))

    def __iter__(self):
        return iter(self.events)

    @classmethod
    def from_h5_file(cls, h5file):
        """
        Initialize an :class:`~pyxsim.event_list.EventList` from a HDF5 file with filename *h5file*.
        """
        events = {}
        parameters = {}

        f = h5py.File(h5file, "r")

        p = f["/parameters"]
        parameters["exp_time"] = YTQuantity(p["exp_time"].value, "s")
        parameters["area"] = YTQuantity(p["area"].value, "cm**2")
        parameters["sky_center"] = YTArray(p["sky_center"][:], "deg")

        d = f["/data"]

        num_events = d["xsky"].size
        start_e = comm.rank*num_events//comm.size
        end_e = (comm.rank+1)*num_events//comm.size

        events["xsky"] = YTArray(d["xsky"][start_e:end_e], "deg")
        events["ysky"] = YTArray(d["ysky"][start_e:end_e], "deg")
        events["eobs"] = YTArray(d["eobs"][start_e:end_e], "keV")

        f.close()

        return EventList(events, parameters)

    @classmethod
    def from_fits_file(cls, fitsfile):
        """
        Initialize an :class:`~pyxsim.event_list.EventList` from a FITS 
        file with filename *fitsfile*.
        """
        hdulist = pyfits.open(fitsfile, memmap=True)

        tblhdu = hdulist["EVENTS"]

        events = {}
        parameters = {}

        parameters["exp_time"] = YTQuantity(tblhdu.header["EXPOSURE"], "s")
        parameters["area"] = YTQuantity(tblhdu.header["AREA"], "cm**2")
        parameters["sky_center"] = YTArray([tblhdu.header["TCRVL2"], 
                                            tblhdu.header["TCRVL3"]], "deg")

        num_events = tblhdu.header["NAXIS2"]
        start_e = comm.rank*num_events//comm.size
        end_e = (comm.rank+1)*num_events//comm.size

        wcs = pywcs.WCS(naxis=2)
        wcs.wcs.crpix = [tblhdu.header["TCRPX2"], tblhdu.header["TCRPX3"]]
        wcs.wcs.crval = parameters["sky_center"].d
        wcs.wcs.cdelt = [tblhdu.header["TCDLT2"], tblhdu.header["TCDLT3"]]
        wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
        wcs.wcs.cunit = ["deg"]*2

        xx = tblhdu.data["X"][start_e:end_e]
        yy = tblhdu.data["Y"][start_e:end_e]
        xx, yy = wcs.wcs_pix2world(xx, yy, 1)

        events["xsky"] = YTArray(xx, "degree")
        events["ysky"] = YTArray(yy, "degree")
        events["eobs"] = YTArray(tblhdu.data["ENERGY"][start_e:end_e]/1000., "keV")

        hdulist.close()

        return EventList(events, parameters)

    def write_fits_file(self, fitsfile, fov, nx, overwrite=False):
        """
        Write events to a FITS binary table file. The result is a
        standard "event file" which can be processed by standard
        X-ray analysis tools.

        Parameters
        ----------
        fitsfile : string
            The name of the event file to write.
        fov : float, (value, unit) tuple, :class:`~yt.units.yt_array.YTQuantity`, or :class:`~astropy.units.Quantity`
            The field of view of the event file. If units are not 
            provided, they are assumed to be in arcminutes.
        nx : integer
            The resolution of the image (number of pixels on a side). 
        overwrite : boolean, optional
            Set to True to overwrite a previous file.
        """
        from astropy.time import Time, TimeDelta

        events = communicate_events(self.events)

        fov = parse_value(fov, "arcmin")

        if comm.rank == 0:

            exp_time = float(self.parameters["exp_time"])

            t_begin = Time.now()
            dt = TimeDelta(exp_time, format='sec')
            t_end = t_begin + dt

            dtheta = fov.to("deg").v / nx

            wcs = pywcs.WCS(naxis=2)
            wcs.wcs.crpix = [0.5*(nx+1)]*2
            wcs.wcs.crval = self.parameters["sky_center"].d
            wcs.wcs.cdelt = [-dtheta, dtheta]
            wcs.wcs.ctype = ["RA---TAN", "DEC--TAN"]
            wcs.wcs.cunit = ["deg"] * 2

            xx, yy = wcs.wcs_world2pix(self["xsky"].d, self["ysky"].d, 1)

            keepx = np.logical_and(xx >= 0.5, xx <= float(nx)+0.5)
            keepy = np.logical_and(yy >= 0.5, yy <= float(nx)+0.5)
            keep = np.logical_and(keepx, keepy)

            n_events = keep.sum()

            mylog.info("Threw out %d events because " % (xx.size-n_events) +
                       "they fell outside the field of view.")

            col_e = pyfits.Column(name='ENERGY', format='E', unit='eV',
                                  array=events["eobs"].in_units("eV").d[keep])
            col_x = pyfits.Column(name='X', format='D', unit='pixel',
                                  array=xx[keep])
            col_y = pyfits.Column(name='Y', format='D', unit='pixel',
                                  array=yy[keep])

            cols = [col_e, col_x, col_y]

            coldefs = pyfits.ColDefs(cols)
            tbhdu = pyfits.BinTableHDU.from_columns(coldefs)
            tbhdu.name = "EVENTS"

            tbhdu.header["MTYPE1"] = "sky"
            tbhdu.header["MFORM1"] = "x,y"
            tbhdu.header["MTYPE2"] = "EQPOS"
            tbhdu.header["MFORM2"] = "RA,DEC"
            tbhdu.header["TCTYP2"] = "RA---TAN"
            tbhdu.header["TCTYP3"] = "DEC--TAN"
            tbhdu.header["TCRVL2"] = float(self.parameters["sky_center"][0])
            tbhdu.header["TCRVL3"] = float(self.parameters["sky_center"][1])
            tbhdu.header["TCDLT2"] = -dtheta
            tbhdu.header["TCDLT3"] = dtheta
            tbhdu.header["TCRPX2"] = 0.5*(nx+1)
            tbhdu.header["TCRPX3"] = 0.5*(nx+1)
            tbhdu.header["TLMIN2"] = 0.5
            tbhdu.header["TLMIN3"] = 0.5
            tbhdu.header["TLMAX2"] = float(nx)+0.5
            tbhdu.header["TLMAX3"] = float(nx)+0.5
            tbhdu.header["EXPOSURE"] = exp_time
            tbhdu.header["TSTART"] = 0.0
            tbhdu.header["TSTOP"] = exp_time
            tbhdu.header["AREA"] = float(self.parameters["area"])
            tbhdu.header["HDUVERS"] = "1.1.0"
            tbhdu.header["RADECSYS"] = "FK5"
            tbhdu.header["EQUINOX"] = 2000.0
            tbhdu.header["HDUCLASS"] = "OGIP"
            tbhdu.header["HDUCLAS1"] = "EVENTS"
            tbhdu.header["HDUCLAS2"] = "ACCEPTED"
            tbhdu.header["DATE"] = t_begin.tt.isot
            tbhdu.header["DATE-OBS"] = t_begin.tt.isot
            tbhdu.header["DATE-END"] = t_end.tt.isot
            if "emin" in self.parameters:
                tbhdu.header["EMIN"] = self.parameters["emin"]
            if "emax" in self.parameters:
                tbhdu.header["EMAX"] = self.parameters["emax"]

            hdulist = [pyfits.PrimaryHDU(), tbhdu]

            pyfits.HDUList(hdulist).writeto(fitsfile, overwrite=overwrite)

        comm.barrier()

    def write_simput_file(self, prefix, overwrite=False, emin=None, emax=None,
                          simput_prefix=None, append=False):
        r"""
        Write events to a SIMPUT file that may be read by the SIMX instrument
        simulator.

        Parameters
        ----------
        prefix : string
            The filename prefix for the photon list file, and 
            for the SIMPUT catalog file unless *simput_prefix*
            is specified, see below.
        overwrite : boolean, optional
            Set to True to overwrite previous files.
        e_min : float, optional
            The minimum energy of the photons to save in keV.
        e_max : float, optional
            The maximum energy of the photons to save in keV.
        simput_prefix : string, optional
            The prefix of the SIMPUT catalog file to write or append 
            to. If not set, it will be the same as *prefix*.
        append : boolean, optional
            If True, append a new source an existing SIMPUT 
            catalog. Default: False
        """
        if simput_prefix is None:
            simput_prefix = prefix

        events = communicate_events(self.events)

        if comm.rank == 0:

            mylog.info("Writing SIMPUT catalog file %s_simput.fits " % simput_prefix +
                       "and SIMPUT photon list file %s_phlist.fits." % prefix)

            flux, xsky, ysky, eobs = _handle_simput(events, self.parameters["exp_time"], 
                                                    self.parameters["area"], emin, emax)

            write_photon_list(simput_prefix, prefix, flux, xsky, ysky, eobs,
                              overwrite=overwrite, append=append)

        comm.barrier()

    def write_h5_file(self, h5file):
        """
        Write an :class:`~pyxsim.event_list.EventList` to the HDF5 file given by *h5file*.
        """
        events = communicate_events(self.events)

        if comm.rank == 0:

            f = h5py.File(h5file, "w")

            p = f.create_group("parameters")
            p.create_dataset("exp_time", data=float(self.parameters["exp_time"]))
            p.create_dataset("area", data=float(self.parameters["area"]))
            p.create_dataset("sky_center", data=self.parameters["sky_center"].d)

            d = f.create_group("data")
            d.create_dataset("xsky", data=events["xsky"].d)
            d.create_dataset("ysky", data=events["ysky"].d)
            d.create_dataset("eobs", data=events["eobs"].d)
            f.close()

        comm.barrier()

    def write_fits_image(self, imagefile, fov, nx, emin=None, 
                         emax=None, overwrite=False):
        r"""
        Generate a image by binning X-ray counts and write it to a FITS file.

        Parameters
        ----------
        imagefile : string
            The name of the image file to write.
        fov : float, (value, unit) tuple, :class:`~yt.units.yt_array.YTQuantity`, or :class:`~astropy.units.Quantity`
            The field of view of the image. If units are not provided, they
            are assumed to be in arcminutes.
        nx : integer
            The resolution of the image (number of pixels on a side). 
        emin : float, optional
            The minimum energy of the photons to put in the image, in keV.
        emax : float, optional
            The maximum energy of the photons to put in the image, in keV.
        overwrite : boolean, optional
            Set to True to overwrite a previous file.
        """
        fov = parse_value(fov, "arcmin")

        if emin is None:
            mask_emin = np.ones(self.num_events, dtype='bool')
        else:
            mask_emin = self["eobs"].d > emin
        if emax is None:
            mask_emax = np.ones(self.num_events, dtype='bool')
        else:
            mask_emax = self["eobs"].d < emax

        mask = np.logical_and(mask_emin, mask_emax)

        dtheta = fov.to("deg").v/nx

        xbins = np.linspace(0.5, float(nx)+0.5, nx+1, endpoint=True)
        ybins = np.linspace(0.5, float(nx)+0.5, nx+1, endpoint=True)

        wcs = pywcs.WCS(naxis=2)
        wcs.wcs.crpix = [0.5*(nx+1)]*2
        wcs.wcs.crval = self.parameters["sky_center"].d
        wcs.wcs.cdelt = [-dtheta, dtheta]
        wcs.wcs.ctype = ["RA---TAN","DEC--TAN"]
        wcs.wcs.cunit = ["deg"]*2

        xx, yy = wcs.wcs_world2pix(self["xsky"].d, self["ysky"].d, 1)

        H, xedges, yedges = np.histogram2d(xx[mask], yy[mask],
                                           bins=[xbins, ybins])

        if parallel_capable:
            H = comm.comm.reduce(H, root=0)

        if comm.rank == 0:

            hdu = pyfits.PrimaryHDU(H.T)

            hdu.header["MTYPE1"] = "EQPOS"
            hdu.header["MFORM1"] = "RA,DEC"
            hdu.header["CTYPE1"] = "RA---TAN"
            hdu.header["CTYPE2"] = "DEC--TAN"
            hdu.header["CRPIX1"] = 0.5*(nx+1)
            hdu.header["CRPIX2"] = 0.5*(nx+1)
            hdu.header["CRVAL1"] = float(self.parameters["sky_center"][0])
            hdu.header["CRVAL2"] = float(self.parameters["sky_center"][1])
            hdu.header["CUNIT1"] = "deg"
            hdu.header["CUNIT2"] = "deg"
            hdu.header["CDELT1"] = -dtheta
            hdu.header["CDELT2"] = dtheta
            hdu.header["EXPOSURE"] = float(self.parameters["exp_time"])

            hdu.writeto(imagefile, overwrite=overwrite)

        comm.barrier()

    def write_spectrum(self, specfile, emin, emax, nchan, overwrite=False):
        r"""
        Bin event energies into a spectrum and write it to a FITS binary table. 
        This is for an *unconvolved* spectrum.

        Parameters
        ----------
        specfile : string
            The name of the FITS file to be written.
        emin : float
            The minimum energy of the spectral bins in keV.
        emax : float
            The maximum energy of the spectral bins in keV.
        nchan : integer
            The number of channels.
        overwrite : boolean, optional
            Set to True to overwrite a previous file.
        """
        espec = self["eobs"].d
        spec, ee = np.histogram(espec, bins=nchan, range=(emin, emax))
        bins = 0.5*(ee[1:]+ee[:-1])

        if parallel_capable:
            spec = comm.comm.reduce(spec, root=0)

        if comm.rank == 0:

            col1 = pyfits.Column(name='CHANNEL', format='1J', array=np.arange(nchan).astype('int32')+1)
            col2 = pyfits.Column(name='ENERGY', format='1D', array=bins.astype("float64"))
            col3 = pyfits.Column(name='COUNTS', format='1J', array=spec.astype("int32"))
            col4 = pyfits.Column(name='COUNT_RATE', format='1D', array=spec/float(self.parameters["exp_time"]))

            coldefs = pyfits.ColDefs([col1, col2, col3, col4])

            tbhdu = pyfits.BinTableHDU.from_columns(coldefs)
            tbhdu.name = "SPECTRUM"

            tbhdu.header["DETCHANS"] = spec.shape[0]
            tbhdu.header["TOTCTS"] = spec.sum()
            tbhdu.header["EXPOSURE"] = float(self.parameters["exp_time"])
            tbhdu.header["LIVETIME"] = float(self.parameters["exp_time"])
            tbhdu.header["CONTENT"] = "pi"
            tbhdu.header["HDUCLASS"] = "OGIP"
            tbhdu.header["HDUCLAS1"] = "SPECTRUM"
            tbhdu.header["HDUCLAS2"] = "TOTAL"
            tbhdu.header["HDUCLAS3"] = "TYPE:I"
            tbhdu.header["HDUCLAS4"] = "COUNT"
            tbhdu.header["HDUVERS"] = "1.1.0"
            tbhdu.header["HDUVERS1"] = "1.1.0"
            tbhdu.header["CHANTYPE"] = "pi"
            tbhdu.header["BACKFILE"] = "none"
            tbhdu.header["CORRFILE"] = "none"
            tbhdu.header["POISSERR"] = True
            tbhdu.header["RESPFILE"] = "none"
            tbhdu.header["ANCRFILE"] = "none"
            tbhdu.header["MISSION"] = "none"
            tbhdu.header["TELESCOP"] = "none"
            tbhdu.header["INSTRUME"] = "none"
            tbhdu.header["AREASCAL"] = 1.0
            tbhdu.header["CORRSCAL"] = 0.0
            tbhdu.header["BACKSCAL"] = 1.0

            hdulist = pyfits.HDUList([pyfits.PrimaryHDU(), tbhdu])

            hdulist.writeto(specfile, overwrite=overwrite)

        comm.barrier()


class MultiEventList(object):

    def __init__(self, event_lists):
        self.event_lists = event_lists
        self.num_lists = len(event_lists)

    @classmethod
    def from_h5_files(cls, basename):
        import glob
        event_lists = []
        fns = glob.glob("{}.[0-9][0-9].h5".format(basename))
        fns.sort()
        for fn in fns:
            events = EventList.from_file(fn)
            event_lists.append(events)
        return cls(event_lists)

    def write_simput_catalog(self, prefix, emin=None, emax=None, overwrite=False):

        if comm.rank == 0:

            mylog.info("Writing SIMPUT catalog file %s_simput.fits." % prefix)

            for i, events in enumerate(self.event_lists):

                if i == 0:
                    append = False
                else:
                    append = True

                all_events = communicate_events(events.events)

                phlist_prefix = "%s.%02d" % (prefix, i)

                mylog.info("Writing SIMPUT photon list file %s_phlist.fits." % phlist_prefix)

                flux, xsky, ysky, eobs = _handle_simput(all_events, 
                                                        self.num_lists*events.parameters["exp_time"],
                                                        events.parameters["area"], emin, emax)

                write_photon_list(prefix, phlist_prefix, flux, xsky, ysky, eobs,
                                  overwrite=overwrite, append=append)

        comm.barrier()

    def write_h5_files(self, basename):
        for i, events in enumerate(self.event_lists):
            events.write_h5_file("%s.%02d.h5" % (basename, i))
