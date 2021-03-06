# Licensed under a 3-clause BSD style license - see LICENSE.rst
from __future__ import absolute_import, division, print_function

from warnings import warn
import collections
import socket
import json

import numpy as np
from .. import units as u
from ..units.quantity import QuantityInfoBase
from ..extern import six
from ..extern.six.moves import urllib
from ..utils.exceptions import AstropyUserWarning
from ..utils.compat.numpycompat import NUMPY_LT_1_12
from ..utils.compat.numpy import broadcast_to
from .angles import Longitude, Latitude
from .representation import CartesianRepresentation
from .errors import UnknownSiteException
from ..utils import data, deprecated

try:
    # Not guaranteed available at setup time.
    from .. import _erfa as erfa
except ImportError:
    if not _ASTROPY_SETUP_:
        raise

__all__ = ['EarthLocation']

GeodeticLocation = collections.namedtuple('GeodeticLocation', ['lon', 'lat', 'height'])

# Available ellipsoids (defined in erfam.h, with numbers exposed in erfa).
ELLIPSOIDS = ('WGS84', 'GRS80', 'WGS72')

OMEGA_EARTH = u.Quantity(7.292115855306589e-5, 1./u.s)
"""
Rotational velocity of Earth. In UT1 seconds, this would be 2 pi / (24 * 3600),
but we need the value in SI seconds.
See Explanatory Supplement to the Astronomical Almanac, ed. P. Kenneth Seidelmann (1992),
University Science Books.
"""


def _check_ellipsoid(ellipsoid=None, default='WGS84'):
    if ellipsoid is None:
        ellipsoid = default
    if ellipsoid not in ELLIPSOIDS:
        raise ValueError('Ellipsoid {0} not among known ones ({1})'
                         .format(ellipsoid, ELLIPSOIDS))
    return ellipsoid


def _get_json_result(url, err_str):
    # need to do this here to prevent a series of complicated circular imports
    from .name_resolve import NameResolveError
    try:
        # Retrieve JSON response from Google maps API
        resp = urllib.request.urlopen(url, timeout=data.conf.remote_timeout)
        resp_data = json.loads(resp.read().decode('utf8'))

    except urllib.error.URLError as e:
        # This catches a timeout error, see:
        #   http://stackoverflow.com/questions/2712524/handling-urllib2s-timeout-python
        if isinstance(e.reason, socket.timeout):
            raise NameResolveError(err_str.format(msg="connection timed out"))
        else:
            raise NameResolveError(err_str.format(msg=e.reason))

    except socket.timeout:
        # There are some cases where urllib2 does not catch socket.timeout
        # especially while receiving response data on an already previously
        # working request
        raise NameResolveError(err_str.format(msg="connection timed out"))

    results = resp_data.get('results', [])

    if not results:
        raise NameResolveError(err_str.format(msg="no results returned"))

    if resp_data.get('status', None) != 'OK':
        raise NameResolveError(err_str.format(msg="unknown failure with Google maps API"))

    return results


class EarthLocationInfo(QuantityInfoBase):
    """
    Container for meta information like name, description, format.  This is
    required when the object is used as a mixin column within a table, but can
    be used as a general way to store meta information.
    """
    _represent_as_dict_attrs = ('x', 'y', 'z', 'ellipsoid')

    def _construct_from_dict(self, map):
        # Need to pop ellipsoid off and update post-instantiation.  This is
        # on the to-fix list in #4261.
        ellipsoid = map.pop('ellipsoid')
        out = self._parent_cls(**map)
        out.ellipsoid = ellipsoid
        return out

    def new_like(self, cols, length, metadata_conflicts='warn', name=None):
        """
        Return a new EarthLocation instance which is consistent with the
        input ``cols`` and has ``length`` rows.

        This is intended for creating an empty column object whose elements can
        be set in-place for table operations like join or vstack.

        Parameters
        ----------
        cols : list
            List of input columns
        length : int
            Length of the output column object
        metadata_conflicts : str ('warn'|'error'|'silent')
            How to handle metadata conflicts
        name : str
            Output column name

        Returns
        -------
        col : EarthLocation (or subclass)
            Empty instance of this class consistent with ``cols``
        """
        # Very similar to QuantityInfo.new_like, but the creation of the
        # map is different enough that this needs its own rouinte.
        # Get merged info attributes shape, dtype, format, description.
        attrs = self.merge_cols_attributes(cols, metadata_conflicts, name,
                                           ('meta', 'format', 'description'))
        # The above raises an error if the dtypes do not match, but returns
        # just the string representation, which is not useful, so remove.
        attrs.pop('dtype')
        # Make empty EarthLocation using the dtype and unit of the last column.
        # Use zeros so we do not get problems for possible conversion to
        # geodetic coordinates.
        shape = (length,) + attrs.pop('shape')
        data = u.Quantity(np.zeros(shape=shape, dtype=cols[0].dtype),
                          unit=cols[0].unit, copy=False)
        # Get arguments needed to reconstruct class
        map = {key: (data[key] if key in 'xyz' else getattr(cols[-1], key))
               for key in self._represent_as_dict_attrs}
        out = self._construct_from_dict(map)
        # Set remaining info attributes
        for attr, value in attrs.items():
            setattr(out.info, attr, value)

        return out


class EarthLocation(u.Quantity):
    """
    Location on the Earth.

    Initialization is first attempted assuming geocentric (x, y, z) coordinates
    are given; if that fails, another attempt is made assuming geodetic
    coordinates (longitude, latitude, height above a reference ellipsoid).
    When using the geodetic forms, Longitudes are measured increasing to the
    east, so west longitudes are negative. Internally, the coordinates are
    stored as geocentric.

    To ensure a specific type of coordinates is used, use the corresponding
    class methods (`from_geocentric` and `from_geodetic`) or initialize the
    arguments with names (``x``, ``y``, ``z`` for geocentric; ``lon``, ``lat``,
    ``height`` for geodetic).  See the class methods for details.


    Notes
    -----
    This class fits into the coordinates transformation framework in that it
    encodes a position on the `~astropy.coordinates.ITRS` frame.  To get a
    proper `~astropy.coordinates.ITRS` object from this object, use the ``itrs``
    property.
    """

    _ellipsoid = 'WGS84'
    _location_dtype = np.dtype({'names': ['x', 'y', 'z'],
                                'formats': [np.float64]*3})
    _array_dtype = np.dtype((np.float64, (3,)))

    info = EarthLocationInfo()

    def __new__(cls, *args, **kwargs):
        # TODO: needs copy argument and better dealing with inputs.
        if (len(args) == 1 and len(kwargs) == 0 and
                isinstance(args[0], EarthLocation)):
            return args[0].copy()
        try:
            self = cls.from_geocentric(*args, **kwargs)
        except (u.UnitsError, TypeError) as exc_geocentric:
            try:
                self = cls.from_geodetic(*args, **kwargs)
            except Exception as exc_geodetic:
                raise TypeError('Coordinates could not be parsed as either '
                                'geocentric or geodetic, with respective '
                                'exceptions "{0}" and "{1}"'
                                .format(exc_geocentric, exc_geodetic))
        return self

    @classmethod
    def from_geocentric(cls, x, y, z, unit=None):
        """
        Location on Earth, initialized from geocentric coordinates.

        Parameters
        ----------
        x, y, z : `~astropy.units.Quantity` or array-like
            Cartesian coordinates.  If not quantities, ``unit`` should be given.
        unit : `~astropy.units.UnitBase` object or None
            Physical unit of the coordinate values.  If ``x``, ``y``, and/or
            ``z`` are quantities, they will be converted to this unit.

        Raises
        ------
        astropy.units.UnitsError
            If the units on ``x``, ``y``, and ``z`` do not match or an invalid
            unit is given.
        ValueError
            If the shapes of ``x``, ``y``, and ``z`` do not match.
        TypeError
            If ``x`` is not a `~astropy.units.Quantity` and no unit is given.
        """
        if unit is None:
            try:
                unit = x.unit
            except AttributeError:
                raise TypeError("Geocentric coordinates should be Quantities "
                                "unless an explicit unit is given.")
        else:
            unit = u.Unit(unit)

        if unit.physical_type != 'length':
            raise u.UnitsError("Geocentric coordinates should be in "
                               "units of length.")

        try:
            x = u.Quantity(x, unit, copy=False)
            y = u.Quantity(y, unit, copy=False)
            z = u.Quantity(z, unit, copy=False)
        except u.UnitsError:
            raise u.UnitsError("Geocentric coordinate units should all be "
                               "consistent.")

        x, y, z = np.broadcast_arrays(x, y, z)
        struc = np.empty(x.shape, cls._location_dtype)
        struc['x'], struc['y'], struc['z'] = x, y, z
        return super(EarthLocation, cls).__new__(cls, struc, unit, copy=False)

    @classmethod
    def from_geodetic(cls, lon, lat, height=0., ellipsoid=None):
        """
        Location on Earth, initialized from geodetic coordinates.

        Parameters
        ----------
        lon : `~astropy.coordinates.Longitude` or float
            Earth East longitude.  Can be anything that initialises an
            `~astropy.coordinates.Angle` object (if float, in degrees).
        lat : `~astropy.coordinates.Latitude` or float
            Earth latitude.  Can be anything that initialises an
            `~astropy.coordinates.Latitude` object (if float, in degrees).
        height : `~astropy.units.Quantity` or float, optional
            Height above reference ellipsoid (if float, in meters; default: 0).
        ellipsoid : str, optional
            Name of the reference ellipsoid to use (default: 'WGS84').
            Available ellipsoids are:  'WGS84', 'GRS80', 'WGS72'.

        Raises
        ------
        astropy.units.UnitsError
            If the units on ``lon`` and ``lat`` are inconsistent with angular
            ones, or that on ``height`` with a length.
        ValueError
            If ``lon``, ``lat``, and ``height`` do not have the same shape, or
            if ``ellipsoid`` is not recognized as among the ones implemented.

        Notes
        -----
        For the conversion to geocentric coordinates, the ERFA routine
        ``gd2gc`` is used.  See https://github.com/liberfa/erfa
        """
        ellipsoid = _check_ellipsoid(ellipsoid, default=cls._ellipsoid)
        lon = Longitude(lon, u.degree, wrap_angle=180*u.degree, copy=False)
        lat = Latitude(lat, u.degree, copy=False)
        # don't convert to m by default, so we can use the height unit below.
        if not isinstance(height, u.Quantity):
            height = u.Quantity(height, u.m, copy=False)
        # convert to float in units required for erfa routine, and ensure
        # all broadcast to same shape, and are at least 1-dimensional.
        _lon, _lat, _height = np.broadcast_arrays(lon.to_value(u.radian),
                                                  lat.to_value(u.radian),
                                                  height.to_value(u.m))
        # get geocentric coordinates. Have to give one-dimensional array.
        xyz = erfa.gd2gc(getattr(erfa, ellipsoid), _lon.ravel(),
                                 _lat.ravel(), _height.ravel())
        self = xyz.view(cls._location_dtype, cls).reshape(_lon.shape)
        self._unit = u.meter
        self._ellipsoid = ellipsoid
        return self.to(height.unit)

    @classmethod
    def of_site(cls, site_name):
        """
        Return an object of this class for a known observatory/site by name.

        This is intended as a quick convenience function to get basic site
        information, not a fully-featured exhaustive registry of observatories
        and all their properties.

        .. note::
            When this function is called, it will attempt to download site
            information from the astropy data server. If you would like a site
            to be added, issue a pull request to the
            `astropy-data repository <https://github.com/astropy/astropy-data>`_ .
            If a site cannot be found in the registry (i.e., an internet
            connection is not available), it will fall back on a built-in list,
            In the future, this bundled list might include a version-controlled
            list of canonical observatories extracted from the online version,
            but it currently only contains the Greenwich Royal Observatory as an
            example case.


        Parameters
        ----------
        site_name : str
            Name of the observatory (case-insensitive).

        Returns
        -------
        site : This class (a `~astropy.coordinates.EarthLocation` or subclass)
            The location of the observatory.

        See Also
        --------
        get_site_names : the list of sites that this function can access
        """
        registry = cls._get_site_registry()
        try:
            el = registry[site_name]
        except UnknownSiteException as e:
            raise UnknownSiteException(e.site, 'EarthLocation.get_site_names', close_names=e.close_names)

        if cls is el.__class__:
            return el
        else:
            newel = cls.from_geodetic(*el.to_geodetic())
            newel.info.name = el.info.name
            return newel

    @classmethod
    def of_address(cls, address, get_height=False):
        """
        Return an object of this class for a given address by querying the Google
        maps geocoding API.

        This is intended as a quick convenience function to get fast access to
        locations. In the background, this just issues a query to the Google maps
        geocoding API. It is not meant to be abused! Google uses IP-based query
        limiting and will ban your IP if you send more than a few thousand queries
        per hour [1]_.

        .. warning::
            If the query returns more than one location (e.g., searching on
            ``address='springfield'``), this function will use the **first** returned
            location.

        Parameters
        ----------
        address : str
            The address to get the location for. As per the Google maps API, this
            can be a fully specified street address (e.g., 123 Main St., New York,
            NY) or a city name (e.g., Danbury, CT), or etc.
        get_height : bool (optional)
            Use the retrieved location to perform a second query to the Google maps
            elevation API to retrieve the height of the input address [2]_.

        Returns
        -------
        location : This class (a `~astropy.coordinates.EarthLocation` or subclass)
            The location of the input address.

        References
        ----------
        .. [1] https://developers.google.com/maps/documentation/geocoding/intro
        .. [2] https://developers.google.com/maps/documentation/elevation/intro

        """

        pars = urllib.parse.urlencode({'address': address})
        geo_url = "https://maps.googleapis.com/maps/api/geocode/json?{0}".format(pars)

        # get longitude and latitude location
        err_str = ("Unable to retrieve coordinates for address '{address}'; {{msg}}"
                   .format(address=address))
        geo_result = _get_json_result(geo_url, err_str=err_str)
        loc = geo_result[0]['geometry']['location']

        if get_height:
            pars = {'locations': '{lat:.8f},{lng:.8f}'.format(lat=loc['lat'],
                                                              lng=loc['lng'])}
            pars = urllib.parse.urlencode(pars)
            ele_url = "https://maps.googleapis.com/maps/api/elevation/json?{0}".format(pars)

            err_str = ("Unable to retrieve elevation for address '{address}'; {{msg}}"
                       .format(address=address))
            ele_result = _get_json_result(ele_url, err_str=err_str)
            height = ele_result[0]['elevation']*u.meter

        else:
            height = 0.

        return cls.from_geodetic(lon=loc['lng']*u.degree,
                                 lat=loc['lat']*u.degree,
                                 height=height)

    @classmethod
    def get_site_names(cls):
        """
        Get list of names of observatories for use with
        `~astropy.coordinates.EarthLocation.of_site`.

        .. note::
            When this function is called, it will first attempt to
            download site information from the astropy data server.  If it
            cannot (i.e., an internet connection is not available), it will fall
            back on the list included with astropy (which is a limited and dated
            set of sites).  If you think a site should be added, issue a pull
            request to the
            `astropy-data repository <https://github.com/astropy/astropy-data>`_ .


        Returns
        -------
        names : list of str
            List of valid observatory names

        See Also
        --------
        of_site : Gets the actual location object for one of the sites names
                  this returns.
        """
        return cls._get_site_registry().names

    @classmethod
    def _get_site_registry(cls, force_download=False, force_builtin=False):
        """
        Gets the site registry.  The first time this either downloads or loads
        from the data file packaged with astropy.  Subsequent calls will use the
        cached version unless explicitly overridden.

        Parameters
        ----------
        force_download : bool or str
            If not False, force replacement of the cached registry with a
            downloaded version. If a str, that will be used as the URL to
            download from (if just True, the default URL will be used).
        force_builtin : bool
            If True, load from the data file bundled with astropy and set the
            cache to that.

        returns
        -------
        reg : astropy.coordinates.sites.SiteRegistry
        """
        if force_builtin and force_download:
            raise ValueError('Cannot have both force_builtin and force_download True')

        if force_builtin:
            reg = cls._site_registry = get_builtin_sites()
        else:
            reg = getattr(cls, '_site_registry', None)
            if force_download or not reg:
                try:
                    if isinstance(force_download, six.string_types):
                        reg = get_downloaded_sites(force_download)
                    else:
                        reg = get_downloaded_sites()
                except (six.moves.urllib.error.URLError, IOError):
                    # In Python 2.7 the IOError raised by @remote_data stays as
                    # is, while in Python 3.6 the IOError gets converted to a
                    # URLError, so we catch IOError above too, but this can be
                    # removed once we don't support Python 2.7 anymore.
                    if force_download:
                        raise
                    msg = ('Could not access the online site list. Falling '
                           'back on the built-in version, which is rather '
                           'limited. If you want to retry the download, do '
                           '{0}._get_site_registry(force_download=True)')
                    warn(AstropyUserWarning(msg.format(cls.__name__)))
                    reg = get_builtin_sites()
                cls._site_registry = reg

        return reg

    @property
    def ellipsoid(self):
        """The default ellipsoid used to convert to geodetic coordinates."""
        return self._ellipsoid

    @ellipsoid.setter
    def ellipsoid(self, ellipsoid):
        self._ellipsoid = _check_ellipsoid(ellipsoid)

    @property
    def geodetic(self):
        """Convert to geodetic coordinates for the default ellipsoid."""
        return self.to_geodetic()

    def to_geodetic(self, ellipsoid=None):
        """Convert to geodetic coordinates.

        Parameters
        ----------
        ellipsoid : str, optional
            Reference ellipsoid to use.  Default is the one the coordinates
            were initialized with.  Available are: 'WGS84', 'GRS80', 'WGS72'

        Returns
        -------
        (lon, lat, height) : tuple
            The tuple contains instances of `~astropy.coordinates.Longitude`,
            `~astropy.coordinates.Latitude`, and `~astropy.units.Quantity`

        Raises
        ------
        ValueError
            if ``ellipsoid`` is not recognized as among the ones implemented.

        Notes
        -----
        For the conversion to geodetic coordinates, the ERFA routine
        ``gc2gd`` is used.  See https://github.com/liberfa/erfa
        """
        ellipsoid = _check_ellipsoid(ellipsoid, default=self.ellipsoid)
        self_array = self.to(u.meter).view(self._array_dtype, np.ndarray)
        lon, lat, height = erfa.gc2gd(getattr(erfa, ellipsoid), self_array)
        return GeodeticLocation(
            Longitude(lon * u.radian, u.degree,
                      wrap_angle=180.*u.degree, copy=False),
            Latitude(lat * u.radian, u.degree, copy=False),
            u.Quantity(height * u.meter, self.unit, copy=False))

    @property
    @deprecated('2.0', alternative='`lon`', obj_type='property')
    def longitude(self):
        """Longitude of the location, for the default ellipsoid."""
        return self.geodetic[0]

    @property
    def lon(self):
        """Longitude of the location, for the default ellipsoid."""
        return self.geodetic[0]

    @property
    @deprecated('2.0', alternative='`lat`', obj_type='property')
    def latitude(self):
        """Latitude of the location, for the default ellipsoid."""
        return self.geodetic[1]

    @property
    def lat(self):
        """Longitude of the location, for the default ellipsoid."""
        return self.geodetic[1]

    @property
    def height(self):
        """Height of the location, for the default ellipsoid."""
        return self.geodetic[2]

    # mostly for symmetry with geodetic and to_geodetic.
    @property
    def geocentric(self):
        """Convert to a tuple with X, Y, and Z as quantities"""
        return self.to_geocentric()

    def to_geocentric(self):
        """Convert to a tuple with X, Y, and Z as quantities"""
        return (self.x, self.y, self.z)

    def get_itrs(self, obstime=None):
        """
        Generates an `~astropy.coordinates.ITRS` object with the location of
        this object at the requested ``obstime``.

        Parameters
        ----------
        obstime : `~astropy.time.Time` or None
            The ``obstime`` to apply to the new `~astropy.coordinates.ITRS`, or
            if None, the default ``obstime`` will be used.

        Returns
        -------
        itrs : `~astropy.coordinates.ITRS`
            The new object in the ITRS frame
        """
        # Broadcast for a single position at multiple times, but don't attempt
        # to be more general here.
        if obstime and self.size == 1 and obstime.size > 1:
            self = broadcast_to(self, obstime.shape, subok=True)

        # do this here to prevent a series of complicated circular imports
        from .builtin_frames import ITRS
        return ITRS(x=self.x, y=self.y, z=self.z, obstime=obstime)

    itrs = property(get_itrs, doc="""An `~astropy.coordinates.ITRS` object  with
                                     for the location of this object at the
                                     default ``obstime``.""")

    def get_gcrs_posvel(self, obstime):
        """
        Calculate the GCRS position and velocity of this object at the
        requested ``obstime``.

        Parameters
        ----------
        obstime : `~astropy.time.Time`
            The ``obstime`` to calculate the GCRS position/velocity at.

        Returns
        --------
        obsgeoloc : `~astropy.coordinates.CartesianRepresentation`
            The GCRS position of the object
        obsgeovel : `~astropy.coordinates.CartesianRepresentation`
            The GCRS velocity of the object
        """
        # do this here to prevent a series of complicated circular imports
        from .builtin_frames import GCRS

        itrs = self.get_itrs(obstime)
        geocentric_frame = GCRS(obstime=obstime)
        # GCRS position
        obsgeoloc = itrs.transform_to(geocentric_frame).cartesian
        vel_x = -OMEGA_EARTH * obsgeoloc.y
        vel_y = OMEGA_EARTH * obsgeoloc.x
        vel_z = 0. * vel_x.unit
        obsgeovel = CartesianRepresentation(vel_x, vel_y, vel_z)
        return obsgeoloc, obsgeovel

    @property
    def x(self):
        """The X component of the geocentric coordinates."""
        return self['x']

    @property
    def y(self):
        """The Y component of the geocentric coordinates."""
        return self['y']

    @property
    def z(self):
        """The Z component of the geocentric coordinates."""
        return self['z']

    def __getitem__(self, item):
        result = super(EarthLocation, self).__getitem__(item)
        if result.dtype is self.dtype:
            return result.view(self.__class__)
        else:
            return result.view(u.Quantity)

    def __array_finalize__(self, obj):
        super(EarthLocation, self).__array_finalize__(obj)
        if hasattr(obj, '_ellipsoid'):
            self._ellipsoid = obj._ellipsoid

    def __len__(self):
        if self.shape == ():
            raise IndexError('0-d EarthLocation arrays cannot be indexed')
        else:
            return super(EarthLocation, self).__len__()

    def _to_value(self, unit, equivalencies=[]):
        """Helper method for to and to_value."""
        # Conversion to another unit in both ``to`` and ``to_value`` goes
        # via this routine. To make the regular quantity routines work, we
        # temporarily turn the structured array into a regular one.
        array_view = self.view(self._array_dtype, np.ndarray)
        if equivalencies == []:
            equivalencies = self._equivalencies
        new_array = self.unit.to(unit, array_view, equivalencies=equivalencies)
        return new_array.view(self.dtype).reshape(self.shape)

    if NUMPY_LT_1_12:
        def __repr__(self):
            # Use the numpy >=1.12 way to format structured arrays.
            from .representation import _array2string
            prefixstr = '<' + self.__class__.__name__ + ' '
            arrstr = _array2string(self.view(np.ndarray), prefix=prefixstr)
            return '{0}{1}{2:s}>'.format(prefixstr, arrstr, self._unitstr)


# need to do this here at the bottom to avoid circular dependencies
from .sites import get_builtin_sites, get_downloaded_sites
