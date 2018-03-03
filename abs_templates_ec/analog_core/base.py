# -*- coding: utf-8 -*-

"""This module defines AnalogBase, a base template class for generic analog layout topologies."""

import abc
from itertools import chain
from typing import TYPE_CHECKING, List, Union, Optional, Dict, Any, Set, Tuple
import bisect

from bag.math import lcm
from bag.util.interval import IntervalSet
from bag.util.search import BinaryIterator
from bag.layout.template import TemplateBase
from bag.layout.routing import TrackID, WireArray
from bag.layout.util import BBox
from bag.layout.objects import Instance

from ..analog_mos.core import MOSTech
from ..analog_mos.mos import AnalogMOSBase, AnalogMOSExt
from ..analog_mos.substrate import AnalogSubstrate
from ..analog_mos.edge import AnalogEdge, AnalogEndRow
from ..analog_mos.conn import AnalogMOSConn, AnalogMOSDecap, AnalogMOSDummy, AnalogSubstrateConn

if TYPE_CHECKING:
    from bag.layout.template import TemplateDB
    from bag.layout.routing import RoutingGrid


class AnalogBaseInfo(object):
    """A class that provides information to assist in AnalogBase layout calculations.

    Parameters
    ----------
    grid : RoutingGrid
        the RoutingGrid object.
    lch : float
        the channel length of AnalogBase, in meters.
    guard_ring_nf : int
        guard ring width in number of fingers.  0 to disable.
    top_layer : Optional[int]
        the AnalogBase top layer ID.
    end_mode : int
        right/left/top/bottom end mode flag.  This is a 4-bit integer.  If bit 0 (LSB) is 1, then
        we assume there are no blocks abutting the bottom.  If bit 1 is 1, we assume there are no
        blocks abutting the top.  bit 2 and bit 3 (MSB) corresponds to left and right, respectively.
        The default value is 15, which means we assume this AnalogBase is surrounded by empty
        spaces.
    min_fg_sep : int
        minimum number of separation fingers.
    fg_tot : Optional[int]
        number of fingers in a row.
    """

    def __init__(self, grid, lch, guard_ring_nf, top_layer=None, end_mode=15, min_fg_sep=0,
                 fg_tot=None, **kwargs):
        # type: (RoutingGrid, float, int, Optional[int], int, int, Optional[int], **kwargs) -> None
        tech_params = grid.tech_info.tech_params
        self._tech_cls = tech_params['layout']['mos_tech_class']  # type: MOSTech

        # update RoutingGrid
        lch_unit = int(round(lch / grid.layout_unit / grid.resolution))
        self.grid = grid.copy()
        self._lch_unit = lch_unit
        self.mconn_port_layer = self._tech_cls.get_mos_conn_layer()
        self.dum_port_layer = self._tech_cls.get_dum_conn_layer()
        vm_space, vm_width = self._tech_cls.get_mos_conn_track_info(lch_unit)
        dum_space, dum_width = self._tech_cls.get_dum_conn_track_info(lch_unit)
        self.grid.add_new_layer(self.mconn_port_layer, vm_space, vm_width, 'y', override=True,
                                unit_mode=True)
        self.grid.add_new_layer(self.dum_port_layer, dum_space, dum_width, 'y', override=True,
                                unit_mode=True)
        self.grid.update_block_pitch()

        # initialize parameters
        self.guard_ring_nf = guard_ring_nf
        if top_layer is None:
            top_layer = self.mconn_port_layer + 1
        self.top_layer = top_layer
        self.end_mode = end_mode
        self._place_kwargs = kwargs
        self._min_fg_sep = max(min_fg_sep, self._tech_cls.get_min_fg_sep(lch_unit))
        self.min_fg_decap = self._tech_cls.get_min_fg_decap(lch_unit)
        self.num_fg_per_sd = self._tech_cls.get_num_fingers_per_sd(lch_unit)
        self._sd_pitch_unit = self._tech_cls.get_sd_pitch(lch_unit)

        self._fg_tot = None
        self._sd_xc_unit = None
        self.set_fg_tot(fg_tot)

    @property
    def vertical_pitch_unit(self):
        half_blk_y = self._place_kwargs.get('half_blk_y', True)
        blk_pitch = self.grid.get_block_size(self.top_layer, unit_mode=True,
                                             half_blk_y=half_blk_y)[1]
        return lcm([blk_pitch, self._tech_cls.get_mos_pitch(unit_mode=True)])

    @property
    def sd_pitch(self):
        return self._sd_pitch_unit * self.grid.resolution

    @property
    def sd_pitch_unit(self):
        return self._sd_pitch_unit

    @property
    def min_fg_sep(self):
        return self._min_fg_sep

    @property
    def abut_analog_mos(self):
        return self._tech_cls.abut_analog_mos()

    @min_fg_sep.setter
    def min_fg_sep(self, new_val):
        min_fg_sep_tech = self._tech_cls.get_min_fg_sep(self._lch_unit)
        if new_val < min_fg_sep_tech:
            raise ValueError('min_fg_sep = %d must be less than %d' % (new_val, min_fg_sep_tech))
        self._min_fg_sep = new_val

    @property
    def fg_tot(self):
        return self._fg_tot

    def set_fg_tot(self, new_fg_tot):
        if new_fg_tot is not None:
            self._fg_tot = new_fg_tot
            place_info = self.get_placement_info(new_fg_tot)
            left_margin = place_info.edge_margins[0]
            self._sd_xc_unit = left_margin + place_info.edge_widths[0]
            self.grid.set_track_offset(self.mconn_port_layer, left_margin, unit_mode=True)
            self.grid.set_track_offset(self.dum_port_layer, left_margin, unit_mode=True)
        else:
            self._fg_tot = None
            self._sd_xc_unit = None

    @property
    def sd_xc_unit(self):
        return self._sd_xc_unit

    def get_fg_sep_from_hm_space(self, hm_width, round_even=True):
        # type: (int) -> int
        hm_layer = self.mconn_port_layer + 1
        sp = self.grid.get_line_end_space(hm_layer, hm_width, unit_mode=True)  # type: int
        _, via_ext = self.grid.get_via_extensions(hm_layer - 1, 1, hm_width, unit_mode=True)
        vm_width = self.grid.get_track_width(hm_layer - 1, 1, unit_mode=True)  # type: int
        sp0 = self.sd_pitch_unit - vm_width - 2 * via_ext  # type: int
        ans = max(0, -(-(sp - sp0) // self.sd_pitch_unit)) + 1
        if round_even and ans % 2 == 1:
            ans += 1
        return ans

    def get_placement_info(self, fg_tot):
        left_end = (self.end_mode & 4) != 0
        right_end = (self.end_mode & 8) != 0
        return self._tech_cls.get_placement_info(self.grid, self.top_layer, fg_tot, self._lch_unit,
                                                 self.guard_ring_nf, left_end, right_end, False,
                                                 **self._place_kwargs)

    def get_total_width(self, fg_tot):
        # type: (int) -> int
        """Returns the width of the AnalogMosBase in resolution units.

        Parameters
        ----------
        fg_tot : int
            number of fingers.

        Returns
        -------
        tot_width : int
            the AnalogMosBase width in resolution units.
        """

        return self.get_placement_info(fg_tot).tot_width

    def get_core_width(self, fg_tot):
        # type: (int) -> int
        """Returns the core width of the AnalogMosBase in resolution units.

        Parameters
        ----------
        fg_tot : int
            number of fingers.

        Returns
        -------
        core_width : int
            the AnalogMosBase core width in resolution units.
        """

        return self.get_placement_info(fg_tot).core_width

    def coord_to_col(self, coord, unit_mode=False, mode=0):
        """Convert the given X coordinate to transistor column index.

        Find the left source/drain index closest to the given coordinate.

        Parameters
        ----------
        coord : Union[float, int]
            the X coordinate.
        unit_mode : bool
            True to if coordinate is given in resolution units.
        mode : int
            rounding mode.
        Returns
        -------
        col_idx : int
            the left source/drain index closest to the given coordinate.
        """
        if self.fg_tot is None:
            raise ValueError('fg_tot is undefined')

        res = self.grid.resolution
        if not unit_mode:
            coord = int(round(coord / res))

        diff = coord - self._sd_xc_unit
        pitch = self._sd_pitch_unit
        if mode == 0:
            q = (diff + pitch // 2) // pitch
        elif mode < 0:
            q = diff // pitch
        else:
            q = -(-diff // pitch)

        return q

    def col_to_coord(self, col_idx, unit_mode=False):
        """Convert the given transistor column index to X coordinate.

        Parameters
        ----------
        col_idx : int
            the transistor index.  0 is left-most transistor.
        unit_mode : bool
            True to return coordinate in resolution units.

        Returns
        -------
        xcoord : float
            X coordinate of the left source/drain center of the given transistor.
        """
        if self.fg_tot is None:
            raise ValueError('fg_tot is undefined')

        coord = self._sd_xc_unit + col_idx * self._sd_pitch_unit
        if unit_mode:
            return coord
        return coord * self.grid.resolution

    def track_to_col_intv(self, layer_id, tr_idx, width=1):
        # type: (int, Union[float, int], int) -> Tuple[int, int]
        """Returns the smallest column interval that covers the given vertical track."""
        if self.fg_tot is None:
            raise ValueError('fg_tot is undefined')

        lower, upper = self.grid.get_wire_bounds(layer_id, tr_idx, width=width, unit_mode=True)

        lower_col_idx = (lower - self._sd_xc_unit) // self._sd_pitch_unit  # type: int
        upper_col_idx = -(-(upper - self._sd_xc_unit) // self._sd_pitch_unit)  # type: int
        return lower_col_idx, upper_col_idx

    def get_center_tracks(self, layer_id, num_tracks, col_intv, width=1, space=0):
        # type: (int, int, Tuple[int, int], int, Union[float, int]) -> float
        """Return tracks that center on the given column interval.

        Parameters
        ----------
        layer_id : int
            the vertical layer ID.
        num_tracks : int
            number of tracks
        col_intv : Tuple[int, int]
            the column interval.
        width : int
            width of each track.
        space : Union[float, int]
            space between tracks.

        Returns
        -------
        track_id : float
            leftmost track ID of the center tracks.
        """
        x0_unit = self.col_to_coord(col_intv[0], unit_mode=True)
        x1_unit = self.col_to_coord(col_intv[1], unit_mode=True)
        # find track number with coordinate strictly larger than x0
        t_start = self.grid.find_next_track(layer_id, x0_unit, half_track=True, mode=1,
                                            unit_mode=True)
        t_stop = self.grid.find_next_track(layer_id, x1_unit, half_track=True, mode=-1,
                                           unit_mode=True)
        ntracks = int(t_stop - t_start + 1)
        tot_tracks = num_tracks * width + (num_tracks - 1) * space
        if ntracks < tot_tracks:
            raise ValueError('There are only %d tracks in column interval [%d, %d)'
                             % (ntracks, col_intv[0], col_intv[1]))

        ans = t_start + (ntracks - tot_tracks + width - 1) / 2
        return ans

    def num_tracks_to_fingers(self, layer_id, num_tracks, col_idx, even=True, fg_margin=0):
        """Returns the minimum number of fingers needed to span given number of tracks.

        Returns the smallest N such that the transistor interval [col_idx, col_idx + N)
        contains num_tracks wires on routing layer layer_id.

        Parameters
        ----------
        layer_id : int
            the vertical layer ID.
        num_tracks : int
            number of tracks
        col_idx : int
            the starting column index.
        even : bool
            True to return even integers.
        fg_margin : int
            Ad this many fingers on both sides of tracks to act as margin.

        Returns
        -------
        min_fg : int
            minimum number of fingers needed to span the given number of tracks.
        """
        x0 = self.col_to_coord(col_idx, unit_mode=True)
        x1 = self.col_to_coord(col_idx + fg_margin, unit_mode=True)
        # find track number with coordinate strictly larger than x0
        t_start = self.grid.find_next_track(layer_id, x1, half_track=True, mode=1, unit_mode=True)
        # find coordinate of last track
        xlast = self.grid.track_to_coord(layer_id, t_start + num_tracks - 1, unit_mode=True)
        xlast += self.grid.get_track_width(layer_id, 1, unit_mode=True) // 2

        # divide by source/drain pitch
        q, r = divmod(xlast - x0, self._sd_pitch_unit)
        if r > 0:
            q += 1
        q += fg_margin
        if even and q % 2 == 1:
            q += 1
        return q


class AnalogBase(TemplateBase, metaclass=abc.ABCMeta):
    """The amplifier abstract template class

    An amplifier template consists of rows of pmos or nmos capped by substrate contacts.
    drain/source connections are mostly vertical, and gate connections are horizontal.  extension
    rows may be inserted to allow more space for gate/output connections.

    each row starts and ends with dummy transistors, and two transistors are always separated
    by separators.  Currently source sharing (e.g. diff pair) and inter-digitation are not
    supported.  All transistors have the same channel length.

    To use this class, draw_base() must be the first function called.

    Parameters
    ----------
    temp_db : TemplateDB
        the template database.
    lib_name : str
        the layout library name.
    params : Dict[str, Any]
        the parameter values.
    used_names : Set[str]
        a set of already used cell names.
    **kwargs
        dictionary of optional parameters.  See documentation of
        :class:`bag.layout.template.TemplateBase` for details.
    """

    def __init__(self, temp_db, lib_name, params, used_names, **kwargs):
        # type: (TemplateDB, str, Dict[str, Any], Set[str], **kwargs) -> None
        TemplateBase.__init__(self, temp_db, lib_name, params, used_names, **kwargs)

        tech_params = self.grid.tech_info.tech_params
        self._tech_cls = tech_params['layout']['mos_tech_class']  # type: MOSTech

        # initialize parameters
        # layout information parameters
        self._lch = None
        self._w_list = None
        self._th_list = None
        self._orient_list = None
        self._fg_tot = None
        self._sd_yc_list = None
        self._mos_kwargs_list = None
        self._layout_info = None
        self._sub_parity = 0
        self._sub_integ_htr = False
        self._top_sub_bndy = None
        self._bot_sub_bndy = None
        self._sub_bndx = None
        self._dum_conn_pitch = self._tech_cls.get_dum_conn_pitch()
        if self._dum_conn_pitch != 1 and self._dum_conn_pitch != 2:
            raise ValueError('Current only support dum_conn_pitch = 1 or 2, '
                             'but it is %d' % self._dum_conn_pitch)

        # transistor usage/automatic dummy parameters
        self._n_intvs = None  # type: List[IntervalSet]
        self._p_intvs = None  # type: List[IntervalSet]
        self._capn_intvs = None
        self._capp_intvs = None
        self._capp_wires = {-1: [], 1: []}
        self._capn_wires = {-1: [], 1: []}
        self._n_netmap = None
        self._p_netmap = None

        # track calculation parameters
        self._ridx_lookup = None
        self._gtr_intv = None
        self._dstr_intv = None
        self._wire_info = None
        self._tr_manager = None

        # substrate parameters
        self._ntap_list = None
        self._ptap_list = None
        self._ptap_exports = None
        self._ntap_exports = None
        self._gr_vdd_warrs = None
        self._gr_vss_warrs = None

    @classmethod
    def get_mos_conn_layer(cls, tech_info):
        tech_cls = tech_info.tech_params['layout']['mos_tech_class']
        return tech_cls.get_mos_conn_layer()

    @property
    def mos_conn_layer(self):
        """Returns the MOSFET connection layer ID."""
        return self._tech_cls.get_mos_conn_layer()

    @property
    def dum_conn_layer(self):
        """Returns the dummy connection layer ID."""
        return self._tech_cls.get_dum_conn_layer()

    @property
    def floating_dummy(self):
        """Returns True if floating dummy connection is OK."""
        return self._tech_cls.floating_dummy()

    @property
    def abut_analog_mos(self):
        """Returns True if abutting mos connection is OK."""
        return self._tech_cls.abut_analog_mos()

    @property
    def layout_info(self):
        # type: () -> AnalogBaseInfo
        return self._layout_info

    @property
    def num_fg_per_sd(self):
        return self._layout_info.num_fg_per_sd

    @property
    def min_fg_sep(self):
        """Returns the minimum number of separator fingers.
        """
        return self._layout_info.min_fg_sep

    @property
    def min_fg_decap(self):
        """Returns the minimum number of decap fingers.
        """
        return self._layout_info.min_fg_decap

    @property
    def sd_pitch(self):
        """Returns the transistor source/drain pitch."""
        return self._layout_info.sd_pitch

    @property
    def sd_pitch_unit(self):
        """Returns the transistor source/drain pitch."""
        return self._layout_info.sd_pitch_unit

    def set_layout_info(self, layout_info):
        # type: (AnalogBaseInfo) -> None
        """Sets the layout information object associated with this class.

        NOTE: this method should only be used when sub-classing AnalogBase.
        """
        self._layout_info = layout_info
        self.grid = layout_info.grid

    def _find_row_index(self, mos_type, row_idx):
        ridx_list = self._ridx_lookup[mos_type]
        if row_idx < 0 or row_idx >= len(ridx_list):
            # error checking
            raise ValueError('%s row with index = %d not found' % (mos_type, row_idx))
        return ridx_list[row_idx]

    def get_num_tracks(self, mos_type, row_idx, tr_type):
        """Get number of tracks of the given type on the given row.

        Parameters
        ----------
        mos_type : string
            the row type, one of 'nch', 'pch', 'ntap', or 'ptap'
        row_idx : int
            the row index.  0 is the bottom-most row.
        tr_type : string
            the type of the track.  Either 'g' or 'ds'.

        Returns
        -------
        num_tracks : int
            number of tracks.
        """
        row_idx = self._find_row_index(mos_type, row_idx)
        if tr_type == 'g':
            tr_intv = self._gtr_intv[row_idx]
        else:
            tr_intv = self._dstr_intv[row_idx]

        return int(tr_intv[1] - tr_intv[0])

    def get_track_index(self, mos_type, row_idx, tr_type, tr_idx):
        """Convert relative track index to absolute track index.

        Parameters
        ----------
        mos_type : string
            the row type, one of 'nch', 'pch', 'ntap', or 'ptap'.
        row_idx : int
            the center row index.  0 is the bottom-most row.
        tr_type : str
            the type of the track.  Either 'g' or 'ds'.
        tr_idx : float
            the relative track index.

        Returns
        -------
        abs_tr_idx : float
            the absolute track index.
        """
        row_idx = self._find_row_index(mos_type, row_idx)
        if tr_type == 'g':
            tr_intv = self._gtr_intv[row_idx]
        else:
            tr_intv = self._dstr_intv[row_idx]

        # error checking
        ntr = int(tr_intv[1] - tr_intv[0])
        if tr_idx >= ntr:
            raise ValueError('track_index %d out of bounds: [0, %d)' % (tr_idx, ntr))

        if self._orient_list[row_idx] == 'R0':
            return tr_intv[0] + tr_idx
        else:
            return tr_intv[1] - 1 - tr_idx

    def make_track_id(self, mos_type, row_idx, tr_type, tr_idx, width=1,
                      num=1, pitch=0.0):
        """Make TrackID representing the given relative index

        Parameters
        ----------
        mos_type : string
            the row type, one of 'nch', 'pch', 'ntap', or 'ptap'.
        row_idx : int
            the center row index.  0 is the bottom-most row.
        tr_type : str
            the type of the track.  Either 'g' or 'ds'.
        tr_idx : float
            the relative track index.
        width : int
            track width in number of tracks.
        num : int
            number of tracks in this array.
        pitch : float
            pitch between adjacent tracks, in number of track pitches.

        Returns
        -------
        tr_id : :class:`~bag.layout.routing.TrackID`
            TrackID representing the specified track.
        """
        tid = self.get_track_index(mos_type, row_idx, tr_type, tr_idx)
        return TrackID(self.mos_conn_layer + 1, tid, width=width, num=num, pitch=pitch)

    def get_wire_id(self, mos_type, row_idx, tr_type, wire_idx=0, wire_name=''):
        # type: (str, int, str, int, str) -> TrackID
        """Returns the TrackID representing the given wire.

        Parameters
        ----------
        mos_type : str
            the row type, one of 'nch', 'pch', 'ntap', or 'ptap'.
        row_idx : int
            the center row index.  0 is the bottom-most row.
        tr_type : str
            the type of the track.  Either 'g' or 'ds'.
        wire_idx : int
            the wire index.  If wire_name is not given, returns the (wire_idx)th wire.
            If wire_name is given, returns the (wire_idx)th wire with the given name.
        wire_name : str
            name of the wire.

        Returns
        -------
        tr_id : TrackID
            TrackID representing the specified track.
        """
        if self._tr_manager is None:
            raise ValueError('draw_base() is not called with wire information.')

        row_idx = self._find_row_index(mos_type, row_idx)
        info_idx = 0 if tr_type == 'g' else 1
        name_list, loc_list = self._wire_info[row_idx][info_idx]
        hm_layer = self.mos_conn_layer + 1
        if wire_name:
            idx = -1
            for j in range(wire_idx + 1):
                idx = name_list.index(wire_name, idx + 1)
            cur_name = wire_name
            cur_loc = loc_list[idx]
        else:
            cur_name = name_list[wire_idx]
            cur_loc = loc_list[wire_idx]

        cur_width = self._tr_manager.get_width(hm_layer, cur_name)
        return TrackID(hm_layer, cur_loc, width=cur_width)

    def connect_to_substrate(self, sub_type, warr_list, inner=False, both=False):
        # type: (str, Union[WireArray, List[WireArray]], bool, bool) -> None
        """Connect the given transistor wires to substrate.

        Parameters
        ----------
        sub_type : str
            substrate type.  Either 'ptap' or 'ntap'.
        warr_list : Union[WireArray, List[WireArray]]
            list of WireArrays to connect to supply.
        inner : bool
            True to connect to inner substrate.
        both : bool
            True to connect to both substrates
        """
        if isinstance(warr_list, WireArray):
            warr_list = [warr_list]
        wire_yb, wire_yt = None, None
        port_name = 'VDD' if sub_type == 'ntap' else 'VSS'

        if both:
            # set inner to True if both is True
            inner = True

        # get wire upper/lower Y coordinate and record used supply tracks
        sub_port_id_list = [tid for warr in warr_list for tid in warr.track_id]
        if sub_type == 'ptap':
            if inner:
                if len(self._ptap_list) != 2:
                    raise ValueError('Inner substrate does not exist.')
                port = self._ptap_list[1].get_port(port_name)
                self._ptap_exports[1].update(sub_port_id_list)
                wire_yt = port.get_bounding_box(self.grid, self.mos_conn_layer).top
            if not inner or both:
                port = self._ptap_list[0].get_port(port_name)
                self._ptap_exports[0].update(sub_port_id_list)
                wire_yb = port.get_bounding_box(self.grid, self.mos_conn_layer).bottom
        elif sub_type == 'ntap':
            if inner:
                if len(self._ntap_list) != 2:
                    raise ValueError('Inner substrate does not exist.')
                port = self._ntap_list[0].get_port(port_name)
                self._ntap_exports[0].update(sub_port_id_list)
                wire_yb = port.get_bounding_box(self.grid, self.mos_conn_layer).bottom
            if not inner or both:
                port = self._ntap_list[-1].get_port(port_name)
                self._ntap_exports[-1].update(sub_port_id_list)
                wire_yt = port.get_bounding_box(self.grid, self.mos_conn_layer).top
        else:
            raise ValueError('Invalid substrate type: %s' % sub_type)

        self.connect_wires(warr_list, lower=wire_yb, upper=wire_yt)

    @staticmethod
    def _register_dummy_info(dum_tran_info, dum_key, dum_fg):
        """Register dummy transistor information."""
        cur_fg = dum_tran_info.get(dum_key, 0)
        dum_tran_info[dum_key] = cur_fg + dum_fg

    def get_sch_dummy_info(self, col_start=0, col_stop=None):
        # type: (int, Optional[int]) -> List[Tuple[Tuple[Any], int]]
        """Returns a list of all dummies in the given range.

        Parameters
        ----------
        col_start : int
            the starting column index, inclusive.
        col_stop : Optional[int]
            the stopping column index, exclusive.  Use None for the last column.
        """
        if self.floating_dummy:
            # no dummy transistors in this technology.
            return []

        if col_stop is None:
            col_stop = self._fg_tot

        # get dummy transistor intervals
        total_intv = (0, self._fg_tot)
        p_intvs = [intv_set.get_complement(total_intv) for intv_set in self._p_intvs]
        n_intvs = [intv_set.get_complement(total_intv) for intv_set in self._n_intvs]

        # record dummies
        dum_info = {}
        for mos_type, intvs, cap_intvs in (('pch', p_intvs, self._capp_intvs),
                                           ('nch', n_intvs, self._capn_intvs)):
            for row_idx, (intv_set, cap_intv_set) in enumerate(zip(intvs, cap_intvs)):
                if mos_type == 'pch':
                    net_map = self._p_netmap[row_idx]
                else:
                    net_map = self._n_netmap[row_idx]

                ridx = self._ridx_lookup[mos_type][row_idx]
                w = self._w_list[ridx]
                th = self._th_list[ridx]

                # substrate decap transistors from dummies
                temp_intv = intv_set.copy()
                for intv in cap_intv_set:
                    temp_intv.subtract(intv)

                for start, stop in temp_intv:
                    # limit dummies to those in range
                    start = max(start, col_start)
                    stop = min(stop, col_stop)
                    tot_dum_fg = stop - start
                    if tot_dum_fg > 0:
                        # get left/right net names
                        net_left = net_map[start]
                        net_right = net_map[stop]

                        if tot_dum_fg == 1:
                            if not net_right:
                                # makes sure source net is supply if possible
                                net_left, net_right = net_right, net_left
                            dum_key = (mos_type, w, self._lch, th, net_left, net_right)
                            self._register_dummy_info(dum_info, dum_key, 1)
                        else:
                            if net_left:
                                dum_key = (mos_type, w, self._lch, th, '', net_left)
                                self._register_dummy_info(dum_info, dum_key, 1)
                                tot_dum_fg -= 1
                            if net_right:
                                dum_key = (mos_type, w, self._lch, th, '', net_right)
                                self._register_dummy_info(dum_info, dum_key, 1)
                                tot_dum_fg -= 1
                            if tot_dum_fg > 0:
                                dum_key = (mos_type, w, self._lch, th, '', '')
                                self._register_dummy_info(dum_info, dum_key, tot_dum_fg)

        # return final result, sort by keys so that we get a consistent output.
        # Good for using as identifier.
        result = []
        for key in sorted(dum_info.keys()):
            result.append((key, dum_info[key]))
        return result

    def _draw_dummy_sep_conn(self, mos_type, row_idx, start, stop, dum_htr_list):
        """Draw dummy/separator connection.

        Parameters
        ----------
        mos_type : string
            the row type, one of 'nch', 'pch', 'ntap', or 'ptap'.
        row_idx : int
            the center row index.  0 is the bottom-most row.
        start : int
            starting column index, inclusive.  0 is the left-most transistor.
        stop : int
            stopping column index, exclusive.
        dum_htr_list : List[int]
            list of dummy half-track indices to export.

        Returns
        -------
        use_htr : List[int]
            Used dummy half tracks.
        yb : int
            dummy port bottom Y coordinate, in resolution units.
        yt : int
            dummy port top Y coordinate, in resolution units.
        """
        # get orientation, width, and source/drain center
        ridx = self._ridx_lookup[mos_type][row_idx]
        orient = self._orient_list[ridx]
        mos_kwargs = self._mos_kwargs_list[ridx].copy()
        w = self._w_list[ridx]
        xc, yc = self._layout_info.sd_xc_unit, self._sd_yc_list[ridx]
        xc += start * self.sd_pitch_unit
        fg = stop - start

        layout_info = self._layout_info
        dum_layer = self.dum_conn_layer
        xl = layout_info.col_to_coord(start, unit_mode=True)
        xr = layout_info.col_to_coord(stop, unit_mode=True)
        htr0 = int(1 + 2 * self.grid.coord_to_track(dum_layer, xl, unit_mode=True))
        htr1 = int(1 + 2 * self.grid.coord_to_track(dum_layer, xr, unit_mode=True))

        edge_mode = 0
        if start > 0:
            htr0 += 1
        else:
            edge_mode += 1
        if stop == self._fg_tot:
            htr1 += 1
            edge_mode += 2

        # get track indices to export
        used_htr = []
        dum_tr_list = []
        tr_offset = self.grid.coord_to_track(dum_layer, xc, unit_mode=True) + 0.5
        for v in dum_htr_list:
            if v >= htr1:
                break
            elif v >= htr0:
                used_htr.append(v)
                dum_tr_list.append((v - 1) / 2 - tr_offset)

        # setup parameter list
        loc = xc, yc
        lch_unit = int(round(self._lch / self.grid.layout_unit / self.grid.resolution))
        mos_kwargs['source_parity'] = start % self._tech_cls.get_mos_conn_modulus(lch_unit)
        params = dict(
            lch=self._lch,
            w=w,
            fg=fg,
            edge_mode=edge_mode,
            gate_tracks=dum_tr_list,
            options=mos_kwargs,
        )
        conn_master = self.new_template(params=params, temp_cls=AnalogMOSDummy)
        conn_inst = self.add_instance(conn_master, loc=loc, orient=orient, unit_mode=True)

        if dum_tr_list:
            warr = conn_inst.get_port().get_pins(dum_layer)[0]
            res = self.grid.resolution
            yb = int(round(warr.lower / res))
            yt = int(round(warr.upper / res))
        else:
            yb = yt = yc
        return used_htr, yb, yt

    def mos_conn_track_used(self, tidx, margin=0):
        col_start, col_stop = self.layout_info.track_to_col_intv(self.mos_conn_layer, tidx)
        col_intv = col_start - margin, col_stop + margin
        for intv_set in chain(self._p_intvs, self._n_intvs):
            if intv_set.has_overlap(col_intv):
                return True
        return False

    def draw_mos_decap(self, mos_type, row_idx, col_idx, fg, gate_ext_mode, export_gate=False,
                       inner=False, **kwargs):
        # type: (str, int, int, int, int, bool, bool, **kwargs) -> Dict[str, WireArray]
        """Draw decap connection."""
        # mark transistors as connected
        val = -1 if inner else 1
        if mos_type == 'pch':
            val *= -1
            net_map = self._p_netmap[row_idx]
            intv_set = self._p_intvs[row_idx]
            cap_intv_set = self._capp_intvs[row_idx]
            wires_dict = self._capp_wires
        else:
            net_map = self._n_netmap[row_idx]
            intv_set = self._n_intvs[row_idx]
            cap_intv_set = self._capn_intvs[row_idx]
            wires_dict = self._capn_wires

        intv = col_idx, col_idx + fg
        if not export_gate:
            # add to cap_intv_set, since we can route dummies over it
            if intv_set.has_overlap(intv) or not cap_intv_set.add(intv):
                msg = 'Cannot connect %s row %d [%d, %d); some are already connected.'
                raise ValueError(msg % (mos_type, row_idx, intv[0], intv[1]))
        else:
            # add to normal intv set.
            if cap_intv_set.has_overlap(intv) or not intv_set.add(intv):
                msg = 'Cannot connect %s row %d [%d, %d); some are already connected.'
                raise ValueError(msg % (mos_type, row_idx, intv[0], intv[1]))
            net_map[intv[0]] = net_map[intv[1]] = ''

        ridx = self._ridx_lookup[mos_type][row_idx]
        orient = self._orient_list[ridx]
        w = self._w_list[ridx]
        xc, yc = self._layout_info.sd_xc_unit, self._sd_yc_list[ridx]
        xc += col_idx * self.sd_pitch_unit

        loc = xc, yc
        conn_params = dict(
            lch=self._lch,
            w=w,
            fg=fg,
            gate_ext_mode=gate_ext_mode,
            export_gate=export_gate,
        )

        if 'sdir' in kwargs and 'ddir' in kwargs:
            if orient == 'MX':
                # flip source/drain directions
                kwargs['sdir'] = 2 - kwargs['sdir']
                kwargs['ddir'] = 2 - kwargs['ddir']

        conn_params.update(kwargs)

        conn_master = self.new_template(params=conn_params, temp_cls=AnalogMOSDecap)
        inst = self.add_instance(conn_master, loc=loc, orient=orient, unit_mode=True)
        wires_dict[val].extend(inst.get_all_port_pins('supply'))
        if export_gate:
            return {'g': inst.get_all_port_pins('g')[0]}
        else:
            return {}

    def draw_mos_conn(self, mos_type, row_idx, col_idx, fg, sdir, ddir,
                      s_net='', d_net='', **kwargs):
        # type: (str, int, int, int, int, int, str, str, **kwargs) -> Dict[str, WireArray]
        """Draw transistor connection.

        Parameters
        ----------
        mos_type : str
            the row type, one of 'nch', 'pch', 'ntap', or 'ptap'.
        row_idx : int
            the center row index.  0 is the bottom-most row.
        col_idx : int
            the left-most transistor index.  0 is the left-most transistor.
        fg : int
            number of fingers.
        sdir : int
            source connection direction.  0 for down, 1 for middle, 2 for up.
        ddir : int
            drain connection direction.  0 for down, 1 for middle, 2 for up.
        s_net : str
            the source net name.  Defaults to empty string, which means the supply.
        d_net : str
            the drain net name.  Defaults to empty string, which means the supply.
        **kwargs :
            optional arguments for AnalogMosConn.
        Returns
        -------
        ports : Dict[str, WireArray]
            a dictionary of ports as WireArrays.  The keys are 'g', 'd', and 's'.
        """
        # sanity checking
        if not isinstance(fg, int):
            raise ValueError('number of fingers must be integer.')
        if not isinstance(row_idx, int):
            raise ValueError('row_idx must be integer.')
        if not isinstance(col_idx, int):
            raise ValueError('col_idx must be integer')

        # mark transistors as connected
        if mos_type == 'pch':
            net_map = self._p_netmap[row_idx]
            intv_set = self._p_intvs[row_idx]
            cap_intv_set = self._capp_intvs[row_idx]
        else:
            net_map = self._n_netmap[row_idx]
            intv_set = self._n_intvs[row_idx]
            cap_intv_set = self._capn_intvs[row_idx]

        if not self.abut_analog_mos:
            # check if we are abutting other transistors when we can't
            left_check = col_idx - 1, col_idx
            right_check = col_idx + fg, col_idx + fg + 1
            overlap_intv = None
            if intv_set.has_overlap(left_check):
                overlap_intv = left_check
            elif intv_set.has_overlap(right_check):
                overlap_intv = right_check

            if overlap_intv is not None:
                msg = ('Cannot abut transistors in this technology.  '
                       '%s row %d [%d, %d) is already used.')
                raise ValueError(msg % (mos_type, row_idx, overlap_intv[0], overlap_intv[1]))

        intv = col_idx, col_idx + fg
        if cap_intv_set.has_overlap(intv) or not intv_set.add(intv):
            msg = 'Cannot connect %s row %d [%d, %d); some are already connected.'
            raise ValueError(msg % (mos_type, row_idx, intv[0], intv[1]))

        net_map[intv[0]] = s_net
        net_map[intv[1]] = s_net if fg % 2 == 0 else d_net

        sd_pitch = self.sd_pitch_unit
        ridx = self._ridx_lookup[mos_type][row_idx]
        orient = self._orient_list[ridx]
        mos_kwargs = self._mos_kwargs_list[ridx].copy()
        w = self._w_list[ridx]
        xc, yc = self._layout_info.sd_xc_unit, self._sd_yc_list[ridx]
        xc += col_idx * sd_pitch

        if orient == 'MX':
            # flip source/drain directions
            sdir = 2 - sdir
            ddir = 2 - ddir

        loc = xc, yc
        mos_kwargs.update(kwargs)
        lch_unit = int(round(self._lch / self.grid.layout_unit / self.grid.resolution))
        mos_kwargs['source_parity'] = col_idx % self._tech_cls.get_mos_conn_modulus(lch_unit)
        conn_params = dict(
            lch=self._lch,
            w=w,
            fg=fg,
            sdir=sdir,
            ddir=ddir,
            options=mos_kwargs,
        )
        conn_params.update(kwargs)

        conn_master = self.new_template(params=conn_params, temp_cls=AnalogMOSConn)
        conn_inst = self.add_instance(conn_master, loc=loc, orient=orient, unit_mode=True)

        return {key: conn_inst.get_port(key).get_pins(self.mos_conn_layer)[0]
                for key in conn_inst.port_names_iter()}

    def get_substrate_box(self, bottom=True):
        # type: (bool) -> Tuple[Optional[BBox], Optional[BBox]]
        """Returns the substrate tap bounding box."""
        if bottom:
            (imp_yb, imp_yt), (thres_yb, thres_yt) = self._bot_sub_bndy
        else:
            (imp_yb, imp_yt), (thres_yb, thres_yt) = self._top_sub_bndy

        xl, xr = self._sub_bndx
        if xl is None or xr is None:
            return None, None

        res = self.grid.resolution
        if imp_yb is None or imp_yt is None:
            imp_box = None
        else:
            imp_box = BBox(xl, imp_yb, xr, imp_yt, res, unit_mode=True)
        if thres_yb is None or thres_yt is None:
            thres_box = None
        else:
            thres_box = BBox(xl, thres_yb, xr, thres_yt, res, unit_mode=True)

        return imp_box, thres_box

    def _make_masters(self, fg_tot, mos_type, lch, bot_sub_w, top_sub_w, w_list, th_list,
                      g_tracks, ds_tracks, orientations, mos_kwargs, row_offset,
                      guard_ring_nf, wire_names):

        # error checking + set default values.
        num_tran = len(w_list)
        if num_tran != len(th_list):
            raise ValueError('transistor type %s width/threshold list length mismatch.' % mos_type)
        if not g_tracks:
            g_tracks = [0] * num_tran
        elif num_tran != len(g_tracks):
            raise ValueError('transistor type %s width/g_tracks list length mismatch.' % mos_type)
        if not ds_tracks:
            ds_tracks = [0] * num_tran
        elif num_tran != len(ds_tracks):
            raise ValueError('transistor type %s width/ds_tracks list length mismatch.' % mos_type)
        if not orientations:
            default_orient = 'R0' if mos_type == 'nch' else 'MX'
            orientations = [default_orient] * num_tran
        elif num_tran != len(orientations):
            raise ValueError('transistor type %s width/orientations '
                             'list length mismatch.' % mos_type)
        if not mos_kwargs:
            mos_kwargs = [{}] * num_tran
        elif num_tran != len(mos_kwargs):
            raise ValueError('transistor type %s width/kwargs list length mismatch.' % mos_type)
        if wire_names is not None:
            wire_names = wire_names[mos_type]
            if num_tran != len(wire_names):
                raise ValueError('transistor type %s wire names list length mismatch.' % mos_type)
        else:
            wire_names = [None] * num_tran

        if not w_list:
            # do nothing
            return [], [], [], [], [], []

        sub_type = 'ptap' if mos_type == 'nch' else 'ntap'
        master_list = []
        place_info_list = []
        wname_list = []
        w_list_final = []
        th_list_final = []
        # make bottom substrate
        if bot_sub_w > 0:
            sub_params = dict(
                lch=lch,
                fg=fg_tot,
                w=bot_sub_w,
                sub_type=sub_type,
                threshold=th_list[0],
                top_layer=None,
                options=dict(guard_ring_nf=guard_ring_nf, integ_htr=self._sub_integ_htr),
            )
            master = self.new_template(params=sub_params, temp_cls=AnalogSubstrate)
            master_list.append(master)
            place_info_list.append(((0, 0), (0, 0),
                                    master.array_box.get_interval('y', unit_mode=True),
                                    master.bound_box.height_unit, 'R0', master.get_ext_bot_info(),
                                    master.get_ext_top_info(), -1, -1))
            self._ridx_lookup[sub_type].append(row_offset)
            row_offset += 1
            w_list_final.append(bot_sub_w)
            th_list_final.append(th_list[0])

        # make transistors
        for w, th, gtr, dstr, orient, mkwargs, wnames in zip(w_list, th_list, g_tracks, ds_tracks,
                                                             orientations, mos_kwargs, wire_names):
            if gtr < 0 or dstr < 0:
                raise ValueError('number of gate/drain/source tracks cannot be negative.')
            params = dict(
                lch=lch,
                fg=fg_tot,
                w=w,
                mos_type=mos_type,
                threshold=th,
                options=mkwargs,
            )
            master = self.new_template(params=params, temp_cls=AnalogMOSBase)
            master_list.append(master)
            height = master.bound_box.height_unit
            g_conn_y = master.get_g_conn_y()
            d_conn_y = master.get_d_conn_y()
            arr_y = master.array_box.get_interval('y', unit_mode=True)
            ext_bot_info = master.get_ext_bot_info()
            ext_top_info = master.get_ext_top_info()
            if orient == 'R0':
                bot_conn_y = g_conn_y
                top_conn_y = d_conn_y
                nbot, ntop = gtr, dstr
            else:
                bot_conn_y = height - d_conn_y[1], height - d_conn_y[0]
                top_conn_y = height - g_conn_y[1], height - g_conn_y[0]
                arr_y = height - arr_y[1], height - arr_y[0]
                ext_bot_info, ext_top_info = ext_top_info, ext_bot_info
                nbot, ntop = dstr, gtr

            if wnames is None:
                place_info_list.append((bot_conn_y, top_conn_y, arr_y, height, orient,
                                        ext_bot_info, ext_top_info, nbot, ntop))
            else:
                if orient == 'R0':
                    bot_wires = wnames['g']
                    top_wires = wnames['ds']
                else:
                    bot_wires = wnames['ds']
                    top_wires = wnames['g']
                wname_list.extend(bot_wires)
                wname_list.extend(top_wires)
                place_info_list.append((bot_conn_y, top_conn_y, arr_y, height, orient,
                                        ext_bot_info, ext_top_info, bot_wires, top_wires))

            self._ridx_lookup[mos_type].append(row_offset)
            row_offset += 1
            w_list_final.append(w)
            th_list_final.append(th)

        # make top substrate
        if top_sub_w > 0:
            sub_params = dict(
                lch=lch,
                w=top_sub_w,
                fg=fg_tot,
                sub_type=sub_type,
                threshold=th_list[-1],
                top_layer=None,
                options=dict(guard_ring_nf=guard_ring_nf, integ_htr=self._sub_integ_htr),
            )
            master = self.new_template(params=sub_params, temp_cls=AnalogSubstrate)
            master_list.append(master)
            arr_y = master.array_box.get_interval('y', unit_mode=True)
            height = master.bound_box.height_unit
            arr_y = height - arr_y[1], height - arr_y[0]
            place_info_list.append(((0, 0), (0, 0), arr_y, height, 'MX', master.get_ext_top_info(),
                                    master.get_ext_bot_info(), -1, -1))
            self._ridx_lookup[sub_type].append(row_offset)
            w_list_final.append(top_sub_w)
            th_list_final.append(th_list[-1])

        mos_kwargs = [{}] + mos_kwargs + [{}]
        return place_info_list, master_list, mos_kwargs, w_list_final, th_list_final, wname_list

    def _place_helper(self, bot_ext_w, place_info_list, lch_unit, fg_tot, hm_layer, vm_le_sp,
                      conn_delta, mos_pitch, tot_pitch, dy, tr_manager, wname_list,
                      guard_ring_nf, min_height):
        tcls = self._tech_cls
        grid = self.grid
        ext_options = dict(guard_ring_nf=guard_ring_nf)
        # place bottom substrate at dy
        widx = 0
        bot_wire_loc = []
        top_wire_loc = []
        y_cur = dy
        tr_next = grid.find_next_track(hm_layer, y_cur, half_track=True, mode=2, unit_mode=True)
        y_list = []
        ext_info_list = []
        bot_tr_intv = []
        top_tr_intv = []
        num_master = len(place_info_list)
        for idx, (bot_conn_y, top_conn_y, arr_y, height, orient, _, bot_ext_info,
                  btr_info, ttr_info) in enumerate(place_info_list):
            # step 1: place current master
            y_list.append(y_cur)
            y_top_cur = y_cur + height
            # step 2: find how many tracks current block uses
            if btr_info == -1:
                # substrate.  A substrate block only use tracks within its array bounding box.
                yarr_bot = y_cur + arr_y[0]
                yarr_top = y_cur + arr_y[1]
                tr_bot = grid.find_next_track(hm_layer, yarr_bot, half_track=True,
                                              mode=2, unit_mode=True)
                tr_top = grid.find_next_track(hm_layer, yarr_top, half_track=True,
                                              mode=-2, unit_mode=True) + 1
                cur_ntr_test = int(2 * (tr_top - tr_bot))
                if cur_ntr_test % 2 == 1:
                    # if not symmetric, R0 substrate supply track is rounded to bottom,
                    # MX substrate supply track is rounded to top.
                    if orient == 'MX':
                        tr_bot += 0.5
                cur_ntr = cur_ntr_test // 2
                if orient == 'R0':
                    top_tr_intv.append((tr_bot, tr_bot + cur_ntr))
                    bot_tr_intv.append((tr_top, tr_top))
                else:
                    bot_tr_intv.append((tr_bot, tr_bot + cur_ntr))
                    top_tr_intv.append((tr_top, tr_top))
                bot_wire_loc.append(None)
                top_wire_loc.append(None)
                cur_top_ntr = cur_ntr
                tr_next = tr_top
            else:
                # transistor.  find bottom/top connection Y coordinates
                by = y_cur + bot_conn_y[0], y_cur + bot_conn_y[1]
                ty = y_cur + top_conn_y[0], y_cur + top_conn_y[1]
                # get track intervals
                tmp_result = self._place_helper_get_tr_intv(tr_next, hm_layer, by, ty, conn_delta,
                                                            vm_le_sp, btr_info, ttr_info,
                                                            tr_manager, widx, wname_list)
                bintv, tintv, tr_next, cur_top_ntr, bot_loc, top_loc, widx = tmp_result
                bot_tr_intv.append(bintv)
                top_tr_intv.append(tintv)
                bot_wire_loc.append((btr_info, bot_loc))
                top_wire_loc.append((ttr_info, top_loc))

            y_next_min = y_top_cur
            # step 3: compute extension to next master and location of next master
            if idx != num_master - 1:
                # step 3A: figure out minimum extension width
                top_ext_info = place_info_list[idx + 1][5]
                ext_w_list = tcls.get_valid_extension_widths(lch_unit, top_ext_info, bot_ext_info,
                                                             guard_ring_nf=guard_ring_nf)
                min_ext_w = ext_w_list[0]
                if idx == 0:
                    # make sure first extension width is at least bot_ext_w
                    min_ext_w = max(min_ext_w, bot_ext_w)
                # update y_next_min
                y_next_min += min_ext_w * mos_pitch
                next_btr_info = place_info_list[idx + 1][7]
                if next_btr_info == -1:
                    # next row is substrate
                    if idx + 1 == num_master - 1 and cur_top_ntr > 0:
                        # last substrate, place out of current top tracks
                        y_tr_last_top = grid.get_wire_bounds(hm_layer, tr_next - 1,
                                                             unit_mode=True)[1]
                        y_next = max(y_next_min, -(-y_tr_last_top // mos_pitch) * mos_pitch)
                    else:
                        # guard ring substrate, place as close to current block as possible
                        y_next = y_next_min
                else:
                    # next row is transistor.  Get number of bottom tracks of next row
                    if (wname_list is not None) and next_btr_info:
                        next_bot_ntr = tr_manager.place_wires(hm_layer, next_btr_info,
                                                              start_idx=tr_next)[0]
                    elif wname_list is None:
                        next_bot_ntr = next_btr_info
                    else:
                        next_bot_ntr = 0

                    if next_bot_ntr > 0:
                        # if next row has bottom tracks,
                        # make sure the next row is placed high enough
                        y_bot_tr_last_mid = grid.track_to_coord(hm_layer,
                                                                tr_next + next_bot_ntr - 1,
                                                                unit_mode=True)
                        byt = place_info_list[idx + 1][0][1]
                        y_tmp = -(-(y_bot_tr_last_mid - byt + conn_delta) // mos_pitch) * mos_pitch
                        y_next = max(y_next_min, y_tmp)
                    else:
                        # otherwise, place next row as close to this row as possible.
                        y_next = y_next_min

                # if next row is the last row, need to round to tot_pitch
                if idx + 1 == num_master - 1:
                    # this is the last block.  Place it such that the overall
                    # height is multiples of tot_pitch.
                    next_height = place_info_list[idx + 1][3]
                    y_top_min = max(y_next + next_height, min_height)
                    y_top = -(-y_top_min // tot_pitch) * tot_pitch
                    y_next = y_top - next_height
                    # make sure we both have valid extension width and last block is on tot_pitch.
                    # Iterate until we get it
                    ext_w = (y_next - y_top_cur) // mos_pitch
                    while ext_w < ext_w_list[-1] and ext_w not in ext_w_list:
                        # find next extension block
                        ext_w = ext_w_list[bisect.bisect_left(ext_w_list, ext_w)]
                        # update y_next
                        y_next = y_top_cur + ext_w * mos_pitch
                        # place last block such that it is on tot_pitch
                        y_top_min = y_next + next_height
                        y_top = -(-y_top_min // tot_pitch) * tot_pitch
                        y_next = y_top - next_height
                        # recalculate ext_w
                        ext_w = (y_next - y_top_cur) // mos_pitch
                else:
                    # make sure ext_w is a valid width
                    ext_w = (y_next - y_top_cur) // mos_pitch
                    if ext_w < ext_w_list[-1] and ext_w not in ext_w_list:
                        ext_w = ext_w_list[bisect.bisect_left(ext_w_list, ext_w)]
                        # update y_next
                        y_next = y_top_cur + ext_w * mos_pitch

                ext_params = dict(
                    lch=self._lch,
                    w=ext_w,
                    fg=fg_tot,
                    top_ext_info=top_ext_info,
                    bot_ext_info=bot_ext_info,
                    options=ext_options,
                )
                ext_info_list.append((ext_w, ext_params))
                # step 3D: update y_cur
                y_cur = y_next

        # return placement result.
        return y_list, ext_info_list, tr_next, bot_tr_intv, top_tr_intv, bot_wire_loc, top_wire_loc

    def _place_helper_get_tr_intv(self, tr_next, hm_layer, by, ty, conn_delta, vm_le_sp, btr_info,
                                  ttr_info, tr_manager, widx, wname_list):
        byb, byt = by
        tyb, tyt = ty
        bnd_b_bot = self.grid.coord_to_nearest_track(hm_layer, byb + conn_delta, half_track=True,
                                                     mode=1, unit_mode=True)
        bnd_b_top = self.grid.coord_to_nearest_track(hm_layer, byt - conn_delta, half_track=True,
                                                     mode=-1, unit_mode=True)
        bnd_t_bot = self.grid.coord_to_nearest_track(hm_layer, tyb + conn_delta, half_track=True,
                                                     mode=1, unit_mode=True)
        bnd_t_top = self.grid.coord_to_nearest_track(hm_layer, tyt - conn_delta, half_track=True,
                                                     mode=-1, unit_mode=True)
        if wname_list is None:
            # bottom b track
            bnd_b_bot = min(bnd_b_bot, bnd_b_top + 1 - btr_info)
            # bottom t track
            bnd_t_bot = max(bnd_t_bot, bnd_b_top + 1)
            # top ds track
            bnd_t_top = max(bnd_t_top, bnd_t_bot + ttr_info - 1)
            # compute lowest DRC clean track upper row can use
            tr_t_top_y = self.grid.track_to_coord(hm_layer, bnd_t_top, unit_mode=True)
            tr_next_y = max(tyt + vm_le_sp + conn_delta,
                            tr_t_top_y + 2 * conn_delta + vm_le_sp)
            tr_next = self.grid.coord_to_nearest_track(hm_layer, tr_next_y, half_track=True,
                                                       mode=1, unit_mode=True)
            cur_top_ntr = bnd_t_top + 1 - bnd_t_bot
            bot_loc = top_loc = []
        else:
            # compute bottom/top wire locations
            sp_betw = 0
            first_bot_tr = tr_next
            if btr_info:
                bot_ntr, bot_loc = tr_manager.place_wires(hm_layer, btr_info, start_idx=tr_next)
                widx += len(btr_info)
                if widx < len(wname_list):
                    sp_betw = tr_manager.get_space(hm_layer, (wname_list[widx - 1],
                                                              wname_list[widx]))
                    tr_next += bot_ntr + sp_betw
                else:
                    tr_next += bot_ntr
            else:
                bot_ntr = 0
                bot_loc = []
            if ttr_info:
                tr_next = max(tr_next, bnd_t_bot)
                first_top_tr = tr_next
                cur_top_ntr, top_loc = tr_manager.place_wires(hm_layer, ttr_info, start_idx=tr_next)
                widx += len(ttr_info)
                if widx < len(wname_list):
                    sp_next = tr_manager.get_space(hm_layer, (wname_list[widx - 1],
                                                              wname_list[widx]))
                    tr_next += cur_top_ntr + sp_next
                else:
                    tr_next += cur_top_ntr
            else:
                tr_next = max(tr_next, bnd_t_top + 1)
                first_top_tr = tr_next
                cur_top_ntr = 0
                top_loc = []

            # check if it's possible to move the bottom tracks up to reduce series resistance
            if bot_ntr > 0:
                new_bot_first = min(first_top_tr - sp_betw - bot_ntr, bnd_b_top + 1 - bot_ntr)
                if new_bot_first > first_bot_tr:
                    _, bot_loc = tr_manager.place_wires(hm_layer, btr_info, start_idx=new_bot_first)

        return ((bnd_b_bot, bnd_b_top + 1), (bnd_t_bot, bnd_t_top + 1),
                tr_next, cur_top_ntr, bot_loc, top_loc, widx)

    def _place(self, fg_tot, place_info_list, master_list, guard_ring_nf, top_layer,
               left_end, right_end, bot_end, top_end, tr_manager, wname_list, min_height):
        """
        Placement strategy: make overall block match mos_pitch and horizontal track pitch, try to
        center everything between the top and bottom substrates.
        """
        # find total pitch of the analog base.
        dum_layer = self.dum_conn_layer
        mconn_layer = self.mos_conn_layer
        hm_layer = mconn_layer + 1
        mos_pitch = self._tech_cls.get_mos_pitch(unit_mode=True)
        tot_pitch = self._layout_info.vertical_pitch_unit
        vm_le_sp = self.grid.get_line_end_space(hm_layer - 1, 1, unit_mode=True)
        via_ext = self.grid.get_via_extensions(hm_layer - 1, 1, 1, unit_mode=True)[0]
        hm_w = self.grid.get_track_width(hm_layer, 1, unit_mode=True)
        conn_delta = via_ext + hm_w // 2
        lch_unit = int(round(self._lch / self.grid.layout_unit / self.grid.resolution))

        # make end rows
        bot_end_params = dict(
            lch=self._lch,
            fg=fg_tot,
            sub_type=master_list[0].params['sub_type'],
            threshold=master_list[0].params['threshold'],
            is_end=bot_end,
            top_layer=top_layer,
        )
        bot_end_master = self.new_template(params=bot_end_params, temp_cls=AnalogEndRow)
        top_end_params = dict(
            lch=self._lch,
            fg=fg_tot,
            sub_type=master_list[-1].params['sub_type'],
            threshold=master_list[-1].params['threshold'],
            is_end=top_end,
            top_layer=top_layer,
        )
        top_end_master = self.new_template(params=top_end_params, temp_cls=AnalogEndRow)
        # compute Y coordinate shift from adding end row
        dy = bot_end_master.array_box.height_unit
        h_top = top_end_master.array_box.height_unit
        min_height -= h_top

        # find bot_ext_w such that we place blocks as close to center as possible,
        # use binary search to shorten search.
        # run first iteration out of the while loop to get minimum bottom extension.
        tmp_result = self._place_helper(0, place_info_list, lch_unit, fg_tot, hm_layer, vm_le_sp,
                                        conn_delta, mos_pitch, tot_pitch, dy, tr_manager,
                                        wname_list, guard_ring_nf, min_height)
        _, ext_list, tot_ntr, _, _, _, _ = tmp_result
        ext_first, ext_last = ext_list[0][0], ext_list[-1][0]
        print('ext_w0 = %d, ext_wend=%d, tot_ntr=%d' % (ext_first, ext_last, tot_ntr))
        tot_ntr_best = tot_ntr
        bot_ext_w_iter = BinaryIterator(ext_first, None)
        bot_ext_w_iter.save_info(tmp_result)
        bot_ext_w_iter.up()
        if ext_first < ext_last:
            while bot_ext_w_iter.has_next():
                bot_ext_w = bot_ext_w_iter.get_next()
                tmp_result = self._place_helper(bot_ext_w, place_info_list, lch_unit, fg_tot,
                                                hm_layer, vm_le_sp, conn_delta, mos_pitch,
                                                tot_pitch, dy, tr_manager, wname_list,
                                                guard_ring_nf, min_height)
                _, ext_list, tot_ntr, _, _, _, _ = tmp_result
                ext_first, ext_last = ext_list[0][0], ext_list[-1][0]
                print('ext_w0 = %d, ext_wend=%d, tot_ntr=%d' % (ext_first, ext_last, tot_ntr))

                if tot_ntr > tot_ntr_best:
                    bot_ext_w_iter.down()
                else:
                    tot_ntr_best = tot_ntr
                    if ext_first == ext_last:
                        bot_ext_w_iter.save_info(tmp_result)
                        break
                    elif ext_first < ext_last:
                        bot_ext_w_iter.save_info(tmp_result)
                        bot_ext_w_iter.up()
                    else:
                        bot_ext_w_iter.down()

        (y_list, ext_list, tot_ntr, bot_tr_intv, top_tr_intv,
         bot_wire_loc, top_wire_loc) = bot_ext_w_iter.get_last_save_info()
        ext_first, ext_last = ext_list[0][0], ext_list[-1][0]
        print('final: ext_w0 = %d, ext_wend=%d, tot_ntr=%d' % (ext_first, ext_last, tot_ntr))

        # at this point we've found the optimal placement.  Place instances
        place_info = self._layout_info.get_placement_info(fg_tot)
        edgel_x0 = place_info.edge_margins[0]
        arr_box_x = place_info.arr_box_x
        tot_width = place_info.tot_width

        array_box = BBox.get_invalid_bbox()
        top_bound_box = BBox.get_invalid_bbox()
        self._gtr_intv = []
        self._dstr_intv = []
        self._wire_info = []
        self._tr_manager = tr_manager
        ext_list.append((0, None))
        gr_vss_warrs = []
        gr_vdd_warrs = []
        gr_vss_dum_warrs = []
        gr_vdd_dum_warrs = []
        # add end rows to list
        y_list.insert(0, 0)
        y_list.append(y_list[-1] + master_list[-1].array_box.height_unit)
        ext_list.insert(0, (0, None))
        ext_list.append((0, None))
        master_list.insert(0, bot_end_master)
        master_list.append(top_end_master)
        orient_list = ['R0']
        orient_list.extend(self._orient_list)
        orient_list.append('MX')
        bot_wire_loc.insert(0, None)
        top_wire_loc.insert(0, None)
        bot_wire_loc.append(None)
        top_wire_loc.append(None)
        # draw
        sub_y_list = []
        for row_idx, (ybot, ext_info, master, orient, bw_info, tw_info) in \
                enumerate(zip(y_list, ext_list, master_list, orient_list,
                              bot_wire_loc, top_wire_loc)):
            if master.is_empty and master.bound_box.height_unit == 0:
                continue

            if row_idx != 0 and row_idx != len(master_list) - 1:
                # get gate/drain/source interval
                if orient == 'R0':
                    gtr_intv = bot_tr_intv[row_idx - 1]
                    dtr_intv = top_tr_intv[row_idx - 1]
                    gw_info = bw_info
                    dw_info = tw_info
                else:
                    gtr_intv = top_tr_intv[row_idx - 1]
                    dtr_intv = bot_tr_intv[row_idx - 1]
                    gw_info = tw_info
                    dw_info = bw_info
                self._gtr_intv.append(gtr_intv)
                self._dstr_intv.append(dtr_intv)
                self._wire_info.append((gw_info, dw_info))

            edge_layout_info = master.get_edge_layout_info()
            edgel_params = dict(
                is_end=left_end,
                guard_ring_nf=guard_ring_nf,
                name_id=master.get_layout_basename(),
                layout_info=edge_layout_info,
                adj_blk_info=master.get_left_edge_info(),
            )
            edge_inst_list = []
            edgel_master = self.new_template(params=edgel_params, temp_cls=AnalogEdge)
            edgel_width = edgel_master.bound_box.width_unit

            yo = ybot if orient == 'R0' else ybot + master.bound_box.height_unit
            inst_loc = (edgel_x0 + edgel_width, yo)
            inst = self.add_instance(master, loc=inst_loc, orient=orient, unit_mode=True)
            # record substrate Y coordinates
            if hasattr(master, 'sub_ysep'):
                y_imp, y_thres = master.sub_ysep
                if orient == 'R0':
                    if y_imp is not None:
                        y_imp += yo
                    if y_thres is not None:
                        y_thres += yo
                else:
                    if y_imp is not None:
                        y_imp = yo - y_imp
                    if y_thres is not None:
                        y_thres = yo - y_thres
                sub_y_list.append((y_imp, y_thres))

            if edgel_master.is_empty:
                array_box = array_box.merge(inst.array_box)
                top_bound_box = top_bound_box.merge(inst.bound_box)
            else:
                edgel = self.add_instance(edgel_master, loc=(edgel_x0, yo),
                                          orient=orient, unit_mode=True)
                array_box = array_box.merge(edgel.array_box)
                top_bound_box = top_bound_box.merge(edgel.bound_box)
                edge_inst_list.append(edgel)

            if isinstance(master, AnalogSubstrate):
                conn_layout_info = edge_layout_info.copy()
                conn_layout_info['fg'] = fg_tot
                sub_parity = self._sub_parity if row_idx == 0 or row_idx == len(y_list) - 1 else 0
                conn_params = dict(
                    layout_info=conn_layout_info,
                    layout_name=master.get_layout_basename() + '_subconn',
                    is_laygo=False,
                    options=dict(sub_parity=sub_parity),
                )
                conn_master = self.new_template(params=conn_params, temp_cls=AnalogSubstrateConn)
                conn_inst = self.add_instance(conn_master, loc=inst_loc,
                                              orient=orient, unit_mode=True)
                sub_type = master.params['sub_type']
                # save substrate instance
                if sub_type == 'ptap':
                    self._ptap_list.append(conn_inst)
                    self._ptap_exports.append(set())
                elif sub_type == 'ntap':
                    self._ntap_list.append(conn_inst)
                    self._ntap_exports.append(set())

            if not isinstance(master, AnalogEndRow):
                sd_yc = inst.translate_master_location((0, master.get_sd_yc()), unit_mode=True)[1]
                self._sd_yc_list.append(sd_yc)

            if orient == 'R0':
                orient_r = 'MY'
            else:
                orient_r = 'R180'
            edger_params = dict(
                is_end=right_end,
                guard_ring_nf=guard_ring_nf,
                name_id=master.get_layout_basename(),
                layout_info=edge_layout_info,
                adj_blk_info=master.get_right_edge_info(),
            )
            edger_master = self.new_template(params=edger_params, temp_cls=AnalogEdge)
            edger_width = edger_master.bound_box.width_unit
            edger_xo = inst.array_box.right_unit + edger_width
            if not edger_master.is_empty:
                edger = self.add_instance(edger_master, loc=(edger_xo, yo),
                                          orient=orient_r, unit_mode=True)
                array_box = array_box.merge(edger.array_box)
                top_bound_box = top_bound_box.merge(edger.bound_box)
                edge_inst_list.append(edger)

            if ext_info[1] is not None:
                ext_master = self.new_template(params=ext_info[1], temp_cls=AnalogMOSExt)
                ext_edge_layout_info = ext_master.get_edge_layout_info()
                ext_edgel_params = dict(
                    is_end=left_end,
                    guard_ring_nf=guard_ring_nf,
                    name_id=ext_master.get_layout_basename(),
                    layout_info=ext_edge_layout_info,
                    adj_blk_info=ext_master.get_left_edge_info(),
                )
                ext_edgel_master = self.new_template(params=ext_edgel_params, temp_cls=AnalogEdge)
                ext_edger_params = dict(
                    is_end=right_end,
                    guard_ring_nf=guard_ring_nf,
                    name_id=ext_master.get_layout_basename(),
                    layout_info=ext_edge_layout_info,
                    adj_blk_info=ext_master.get_right_edge_info(),
                )
                ext_edger_master = self.new_template(params=ext_edger_params, temp_cls=AnalogEdge)

                yo = inst.array_box.top_unit
                # record substrate Y coordinate in extension block
                y_imp, y_thres = ext_master.sub_ysep
                if y_imp is not None:
                    y_imp += yo
                if y_thres is not None:
                    y_thres += yo
                sub_y_list.append((y_imp, y_thres))
                if not ext_master.is_empty:
                    self.add_instance(ext_master, loc=(inst_loc[0], yo), unit_mode=True)
                if not ext_edgel_master.is_empty:
                    edgel = self.add_instance(ext_edgel_master, loc=(edgel_x0, yo), unit_mode=True)
                    edge_inst_list.append(edgel)
                if not ext_edger_master.is_empty:
                    edger = self.add_instance(ext_edger_master, loc=(edger_xo, yo),
                                              orient='MY', unit_mode=True)
                    edge_inst_list.append(edger)

            # gather guard ring ports
            for inst in edge_inst_list:
                if inst.has_port('VDD'):
                    gr_vdd_warrs.extend(inst.get_all_port_pins('VDD', layer=mconn_layer))
                    gr_vdd_dum_warrs.extend(inst.get_all_port_pins('VDD', layer=dum_layer))
                if inst.has_port('VSS'):
                    gr_vss_warrs.extend(inst.get_all_port_pins('VSS', layer=mconn_layer))
                    gr_vss_dum_warrs.extend(inst.get_all_port_pins('VSS', layer=dum_layer))

        # get top/bottom substrate Y boundaries
        self._bot_sub_bndy = ((sub_y_list[0][0], sub_y_list[1][0]),
                              (sub_y_list[0][1], sub_y_list[1][1]))
        self._top_sub_bndy = ((sub_y_list[-2][0], sub_y_list[-1][0]),
                              (sub_y_list[-2][1], sub_y_list[-1][1]))

        # get left/right substrate coordinates
        tot_imp_box = BBox.get_invalid_bbox()
        for lay in self.grid.tech_info.get_implant_layers('ptap'):
            tot_imp_box = tot_imp_box.merge(self.get_rect_bbox(lay))
        for lay in self.grid.tech_info.get_implant_layers('ntap'):
            tot_imp_box = tot_imp_box.merge(self.get_rect_bbox(lay))

        if not tot_imp_box.is_physical():
            self._sub_bndx = None, None
        else:
            self._sub_bndx = tot_imp_box.left_unit, tot_imp_box.right_unit

        # connect body guard rings together
        self._gr_vdd_warrs = self.connect_wires(gr_vdd_warrs)
        self._gr_vss_warrs = self.connect_wires(gr_vss_warrs)
        self.connect_wires(gr_vdd_dum_warrs)
        self.connect_wires(gr_vss_dum_warrs)

        # set array box/size/draw PR boundary
        self.array_box = BBox(arr_box_x[0], array_box.bottom_unit, arr_box_x[1], array_box.top_unit,
                              array_box.resolution, unit_mode=True)
        bound_box = BBox(0, top_bound_box.bottom_unit, tot_width, top_bound_box.top_unit,
                         top_bound_box.resolution, unit_mode=True)
        if top_layer <= self.mos_conn_layer + 1:
            self.prim_bound_box = bound_box
            self.prim_top_layer = top_layer
        else:
            self.set_size_from_bound_box(top_layer, bound_box)

        self.add_cell_boundary(self.bound_box)

    def draw_base(self,  # type: AnalogBase
                  lch,  # type: float
                  fg_tot,  # type: int
                  ptap_w,  # type: Union[float, int]
                  ntap_w,  # type: Union[float, int]
                  nw_list,  # type: List[Union[float, int]]
                  nth_list,  # type: List[str]
                  pw_list,  # type: List[Union[float, int]]
                  pth_list,  # type: List[str]
                  ng_tracks=None,  # type: Optional[List[int]]
                  nds_tracks=None,  # type: Optional[List[int]]
                  pg_tracks=None,  # type: Optional[List[int]]
                  pds_tracks=None,  # type: Optional[List[int]]
                  n_orientations=None,  # type: Optional[List[str]]
                  p_orientations=None,  # type: Optional[List[str]]
                  guard_ring_nf=0,  # type: int
                  n_kwargs=None,  # type: Optional[List[Dict[str, Any]]]
                  p_kwargs=None,  # type: Optional[List[Dict[str, Any]]]
                  pgr_w=None,  # type: Optional[Union[float, int]]
                  ngr_w=None,  # type: Optional[Union[float, int]]
                  min_fg_sep=0,  # type: int
                  end_mode=15,  # type: int
                  top_layer=None,  # type: Optional[int]
                  sub_parity=0,  # type: int
                  **kwargs
                  ):
        # type: (...) -> None
        """Draw the analog base.

        This method must be called first.

        Parameters
        ----------
        lch : float
            the transistor channel length, in meters
        fg_tot : int
            total number of fingers for each row.
        ptap_w : Union[float, int]
            pwell substrate contact width.
        ntap_w : Union[float, int]
            nwell substrate contact width.
        nw_list : List[Union[float, int]]
            a list of nmos width for each row, from bottom to top.
        nth_list: List[str]
            a list of nmos threshold flavor for each row, from bottom to top.
        pw_list : List[Union[float, int]]
            a list of pmos width for each row, from bottom to top.
        pth_list : List[str]
            a list of pmos threshold flavor for each row, from bottom to top.
        ng_tracks : Optional[List[int]]
            number of nmos gate tracks per row, from bottom to top.  Defaults to 1.
        nds_tracks : Optional[List[int]]
            number of nmos drain/source tracks per row, from bottom to top.  Defaults to 1.
        pg_tracks : Optional[List[int]]
            number of pmos gate tracks per row, from bottom to top.  Defaults to 1.
        pds_tracks : Optional[List[int]]
            number of pmos drain/source tracks per row, from bottom to top.  Defaults to 1.
        n_orientations : Optional[List[str]]
            orientation of each nmos row. Defaults to all 'R0'.
        p_orientations : Optional[List[str]]
            orientation of each pmos row.  Defaults to all 'MX'.
        guard_ring_nf : int
            width of guard ring in number of fingers.  0 to disable guard ring.
        n_kwargs : Optional[Dict[str, Any]]
            Optional keyword arguments for each nmos row.
        p_kwargs : Optional[Dict[str, Any]]
            Optional keyword arguments for each pmos row.
        pgr_w : Optional[Union[float, int]]
            pwell guard ring substrate contact width.
        ngr_w : Optional[Union[float, int]]
            nwell guard ring substrate contact width.
        min_fg_sep : int
            minimum number of fingers between different transistors.
        end_mode : int
            right/left/top/bottom end mode flag.  This is a 4-bit integer.  If bit 0 (LSB) is 1,
            then we assume there are no blocks abutting the bottom.  If bit 1 is 1, we assume there
            are no blocks abutting the top.  bit 2 and bit 3 (MSB) corresponds to left and right,
            respectively.
            The default value is 15, which means we assume this AnalogBase is surrounded by empty
            spaces.
        top_layer : Optional[int]
            The top metal layer this block will use.  Defaults to the horizontal layer above mos
            connection layer.  If the top metal layer is equal to the default layer, then this
            AnalogBase will be a primitive template; self.size will be None, and only the height is
            quantized. If the top metal layer is above the default layer, then this AnalogBase will
            be a standard template, and both width and height will be quantized according to the
            block size.
        sub_parity : str
            the substrate orientation.  When set properly, this flag makes sure that you can
            overlap substrate with adjacent AnalogBase cells.  Should be either 0 or 1.
        **kwargs :
            Other optional arguments.  Currently supports:

            sub_integ_htr : bool
                True if substrate row must contain integer number of horizontal tracks.  Defaults
                to False.
            half_blk_x : bool
                True to allow half-block width.  Defaults to True.
            half_blk_y : bool
                True to allow half-block height.  Defaults to True.
            wire_names : Dict[str, List[Dict[str, List[str]]]]
                specify gate/drain wire types instead of specifying number of tracks.
            tr_manager : TrackManager
                the TrackManager used to plac wires.
            min_height : int
                the minimum height, in resolution units.
        """
        if 'gds_space' in kwargs:
            print('WARNING: gds_space parameter is no longer supported '
                  'by draw_base() of AnalogBase.')

        sub_integ_htr = kwargs.get('sub_integ_htr', False)
        half_blk_x = kwargs.get('half_blk_x', True)
        half_blk_y = kwargs.get('half_blk_y', True)
        tr_manager = kwargs.get('tr_manager', None)
        wire_names = kwargs.get('wire_names', None)
        min_height = kwargs.get('min_height', 0)

        numn = len(nw_list)
        nump = len(pw_list)
        # error checking
        if numn == 0 and nump == 0:
            raise ValueError('Cannot make empty AnalogBase.')
        if ntap_w <= 0 or ptap_w <= 0:
            raise ValueError('ntap/ptap widths must be positive')

        # make AnalogBaseInfo object.  Also update routing grid.
        if self._layout_info is None:
            self.set_layout_info(AnalogBaseInfo(self.grid, lch, guard_ring_nf, top_layer=top_layer,
                                                end_mode=end_mode, min_fg_sep=min_fg_sep,
                                                fg_tot=fg_tot, half_blk_x=half_blk_x,
                                                half_blk_y=half_blk_y))

        # initialize private attributes.
        self._lch = lch
        self._w_list = []
        self._th_list = []
        self._fg_tot = fg_tot
        self._sd_yc_list = []
        self._mos_kwargs_list = []
        self._sub_parity = sub_parity
        self._sub_integ_htr = sub_integ_htr

        self._n_intvs = [IntervalSet() for _ in range(numn)]
        self._p_intvs = [IntervalSet() for _ in range(nump)]
        self._capn_intvs = [IntervalSet() for _ in range(numn)]
        self._capp_intvs = [IntervalSet() for _ in range(nump)]
        self._n_netmap = [[''] * (fg_tot + 1) for _ in range(numn)]
        self._p_netmap = [[''] * (fg_tot + 1) for _ in range(nump)]

        self._ridx_lookup = dict(nch=[], pch=[], ntap=[], ptap=[])

        self._ntap_list = []
        self._ptap_list = []
        self._ptap_exports = []
        self._ntap_exports = []

        if pgr_w is None:
            pgr_w = ntap_w
        if ngr_w is None:
            ngr_w = ptap_w

        if guard_ring_nf == 0:
            ngr_w = 0 if pw_list else ptap_w
            pgr_w = 0 if nw_list else ntap_w

        # place transistor blocks
        wire_list = []
        master_list = []
        place_info_list = []
        bot_sub_end = end_mode % 2
        top_sub_end = (end_mode & 2) >> 1
        left_end = (end_mode & 4) >> 2
        right_end = (end_mode & 8) >> 3
        top_layer = self._layout_info.top_layer
        # make NMOS substrate/transistor masters.
        tmp_result = self._make_masters(fg_tot, 'nch', self._lch, ptap_w, ngr_w, nw_list, nth_list,
                                        ng_tracks, nds_tracks, n_orientations, n_kwargs, 0,
                                        guard_ring_nf, wire_names)
        pinfo_list, m_list, n_kwargs, nw_list, nth_list, wname_list = tmp_result
        master_list.extend(m_list)
        place_info_list.extend(pinfo_list)
        self._mos_kwargs_list.extend(n_kwargs)
        self._w_list.extend(nw_list)
        self._th_list.extend(nth_list)
        wire_list.extend(wname_list)
        # make PMOS substrate/transistor masters.
        tmp_result = self._make_masters(fg_tot, 'pch', self._lch, pgr_w, ntap_w, pw_list, pth_list,
                                        pg_tracks, pds_tracks, p_orientations, p_kwargs,
                                        len(m_list), guard_ring_nf, wire_names)
        pinfo_list, m_list, p_kwargs, pw_list, pth_list, wname_list = tmp_result

        master_list.extend(m_list)
        place_info_list.extend(pinfo_list)
        self._mos_kwargs_list.extend(p_kwargs)
        self._w_list.extend(pw_list)
        self._th_list.extend(pth_list)
        wire_list.extend(wname_list)
        self._orient_list = [item[4] for item in place_info_list]

        # place masters according to track specifications.  Try to center transistors
        if wire_names is None:
            wire_list = None
        self._place(fg_tot, place_info_list, master_list, guard_ring_nf, top_layer,
                    left_end != 0, right_end != 0, bot_sub_end != 0, top_sub_end != 0,
                    tr_manager, wire_list, min_height)

        # draw device blockages
        self.grid.tech_info.draw_device_blockage(self)

    def _connect_substrate(self,  # type: AnalogBase
                           sub_type,  # type: str
                           sub_list,  # type: List[Instance]
                           row_idx_list,  # type: List[int]
                           lower=None,  # type: Optional[Union[float, int]]
                           upper=None,  # type: Optional[Union[float, int]]
                           sup_wires=None,  # type: Optional[Union[WireArray, List[WireArray]]]
                           sup_margin=0,  # type: int
                           unit_mode=False  # type: bool
                           ):
        """Connect all given substrates to horizontal tracks

        Parameters
        ----------
        sub_type : str
            substrate type.  Either 'ptap' or 'ntap'.
        sub_list : List[Instance]
            list of substrates to connect.
        row_idx_list : List[int]
            list of substrate row indices.
        lower : Optional[Union[float, int]]
            lower supply track coordinates.
        upper : Optional[Union[float, int]]
            upper supply track coordinates.
        sup_wires : Optional[Union[WireArray, List[WireArray]]]
            If given, will connect these horizontal wires to supply on mconn layer.
        sup_margin : int
            supply wires mconn layer connection horizontal margin in number of tracks.
        unit_mode : bool
            True if lower/upper is specified in resolution units.

        Returns
        -------
        track_buses : list[bag.layout.routing.WireArray]
            list of substrate tracks buses.
        """
        port_name = 'VDD' if sub_type == 'ntap' else 'VSS'

        if sup_wires is not None and isinstance(sup_wires, WireArray):
            sup_wires = [sup_wires]
        else:
            pass

        sub_warr_list = []
        hm_layer = self.mos_conn_layer + 1
        for row_idx, subinst in zip(row_idx_list, sub_list):
            # Create substrate TrackID
            sub_row_idx = self._find_row_index(sub_type, row_idx)
            dtr_intv = self._dstr_intv[sub_row_idx]
            ntr = int(dtr_intv[1] - dtr_intv[0])
            sub_w = self.grid.get_max_track_width(hm_layer, 1, ntr, half_end_space=False)
            track_id = TrackID(hm_layer, dtr_intv[0] + (ntr - 1) / 2, width=sub_w)

            # get all wires to connect to supply.
            warr_iter_list = [subinst.get_port(port_name).get_pins(self.mos_conn_layer)]
            if port_name == 'VDD':
                warr_iter_list.append(self._gr_vdd_warrs)
            else:
                warr_iter_list.append(self._gr_vss_warrs)

            warr_list = list(chain(*warr_iter_list))
            track_warr = self.connect_to_tracks(warr_list, track_id, track_lower=lower,
                                                track_upper=upper, unit_mode=unit_mode)
            sub_warr_list.append(track_warr)
            if sup_wires is not None:
                wlower, wupper = warr_list[0].lower, warr_list[0].upper
                for conn_warr in sup_wires:
                    if conn_warr.layer_id != hm_layer:
                        raise ValueError('vdd/vss wires must be on layer %d' % hm_layer)
                    tmin, tmax = self.grid.get_overlap_tracks(hm_layer - 1, conn_warr.lower,
                                                              conn_warr.upper, half_track=True)
                    new_warr_list = []
                    for warr in warr_list:
                        for tid in warr.track_id:
                            if tid > tmax:
                                break
                            elif tmin <= tid:
                                if not self.mos_conn_track_used(tid, margin=sup_margin):
                                    warr_tmp = WireArray(TrackID(hm_layer - 1, tid), lower=wlower,
                                                         upper=wupper)
                                    new_warr_list.append(warr_tmp)
                    self.connect_to_tracks(new_warr_list, conn_warr.track_id)

        return sub_warr_list

    def fill_dummy(self,  # type: AnalogBase
                   lower=None,  # type: Optional[Union[float, int]]
                   upper=None,  # type: Optional[Union[float, int]]
                   vdd_warrs=None,  # type: Optional[Union[WireArray, List[WireArray]]]
                   vss_warrs=None,  # type: Optional[Union[WireArray, List[WireArray]]]
                   sup_margin=0,  # type: int
                   unit_mode=False  # type: bool
                   ):
        # type: (...) -> Tuple[List[WireArray], List[WireArray]]
        """Draw dummy/separator on all unused transistors.

        This method should be called last.

        Parameters
        ----------
        lower : Optional[Union[float, int]]
            lower coordinate for the supply tracks.
        upper : Optional[Union[float, int]]
            upper coordinate for the supply tracks.
        vdd_warrs : Optional[Union[WireArray, List[WireArray]]]
            vdd wires to be connected.
        vss_warrs : Optional[Union[WireArray, List[WireArray]]]
            vss wires to be connected.
        sup_margin : int
            vdd/vss wires mos conn layer margin in number of tracks.
        unit_mode : bool
            True if lower/upper are specified in resolution units.

        Returns
        -------
        ptap_wire_arrs : List[WireArray]
            list of P-tap substrate WireArrays.
        ntap_wire_arrs : List[WireArray]
            list of N-tap substrate WireArrays.
        """
        # invert PMOS/NMOS IntervalSet to get unconnected dummies
        total_intv = (0, self._fg_tot)
        p_intvs = [intv_set.get_complement(total_intv) for intv_set in self._p_intvs]
        n_intvs = [intv_set.get_complement(total_intv) for intv_set in self._n_intvs]

        # connect NMOS dummies
        top_tracks = None
        top_sub_inst = None
        if self._ptap_list:
            bot_sub_inst = self._ptap_list[0]
            bot_tracks = self._ptap_exports[0]
            if len(self._ptap_list) > 1:
                top_sub_inst = self._ptap_list[1]
                top_tracks = self._ptap_exports[1]
            self._fill_dummy_helper('nch', n_intvs, self._capn_intvs, self._capn_wires,
                                    bot_sub_inst, top_sub_inst, bot_tracks,
                                    top_tracks, not self._ntap_list)

        # connect PMOS dummies
        bot_tracks = None
        bot_sub_inst = None
        if self._ntap_list:
            top_sub_inst = self._ntap_list[-1]
            top_tracks = self._ntap_exports[-1]
            if len(self._ntap_list) > 1:
                bot_sub_inst = self._ntap_list[0]
                bot_tracks = self._ntap_exports[0]
            self._fill_dummy_helper('pch', p_intvs, self._capp_intvs, self._capp_wires,
                                    bot_sub_inst, top_sub_inst, bot_tracks,
                                    top_tracks, not self._ptap_list)

        # connect NMOS substrates to horizontal tracks.
        if not self._ntap_list:
            # connect both substrates if NMOS only
            ptap_wire_arrs = self._connect_substrate('ptap', self._ptap_list,
                                                     list(range(len(self._ptap_list))),
                                                     lower=lower, upper=upper, sup_wires=vss_warrs,
                                                     sup_margin=sup_margin, unit_mode=unit_mode)
        elif self._ptap_list:
            # NMOS exists, only connect bottom substrate to upper level metal
            ptap_wire_arrs = self._connect_substrate('ptap', self._ptap_list[:1], [0],
                                                     lower=lower, upper=upper, sup_wires=vss_warrs,
                                                     sup_margin=sup_margin, unit_mode=unit_mode)
        else:
            ptap_wire_arrs = []

        # connect PMOS substrates to horizontal tracks.
        if not self._ptap_list:
            # connect both substrates if PMOS only
            ntap_wire_arrs = self._connect_substrate('ntap', self._ntap_list,
                                                     list(range(len(self._ntap_list))),
                                                     lower=lower, upper=upper, sup_wires=vdd_warrs,
                                                     sup_margin=sup_margin, unit_mode=unit_mode)
        elif self._ntap_list:
            # PMOS exists, only connect top substrate to upper level metal
            ntap_wire_arrs = self._connect_substrate('ntap', self._ntap_list[-1:],
                                                     [len(self._ntap_list) - 1],
                                                     lower=lower, upper=upper, sup_wires=vdd_warrs,
                                                     sup_margin=sup_margin, unit_mode=unit_mode)
        else:
            ntap_wire_arrs = []

        return ptap_wire_arrs, ntap_wire_arrs

    def _fill_dummy_helper(self,  # type: AnalogBase
                           mos_type,  # type: str
                           intv_set_list,  # type: List[IntervalSet]
                           cap_intv_set_list,  # type: List[IntervalSet]
                           cap_wires_dict,  # type: Dict[int, List[WireArray]]
                           bot_sub_inst,  # type: Optional[Instance]
                           top_sub_inst,  # type: Optional[Instance]
                           bot_tracks,  # type: List[int]
                           top_tracks,  # type: List[int]
                           export_both  # type: bool
                           ):
        # type: (...) -> None
        """Helper function for figuring out how to connect all dummies to supplies.

        Parameters
        ----------
        mos_type: str
            the transistor type.  Either 'pch' or 'nch'.
        intv_set_list : List[IntervalSet]
            list of used transistor finger intervals on each transistor row.  Index 0 is bottom row.
        cap_intv_set_list : List[IntervalSet]
            list of used decap transistor finger intervals on each transistor row.
            Index 0 is bottom row.
        cap_wires_dict : Dict[int, List[WireArray]]
            dictionary from substrate ID to decap wires that need to connect to that substrate.
            bottom substrate has ID of 1, and top substrate has ID of -1.
        bot_sub_inst : Optional[Instance]
            the bottom substrate instance.
        top_sub_inst : Optional[Instance]
            the top substrate instance.
        bot_tracks : List[int]
            list of port track indices that needs to be exported on bottom substrate.
        top_tracks : List[int]
            list of port track indices that needs to be exported on top substrate.
        export_both : bool
            True if both bottom and top substrate should draw port on mos_conn_layer.
        """
        num_rows = len(intv_set_list)
        bot_conn = top_conn = []

        # step 1: find dummy connection intervals to bottom/top substrates
        num_sub = 0
        if bot_sub_inst is not None:
            num_sub += 1
            bot_conn = self._get_dummy_connections(intv_set_list)
        if top_sub_inst is not None:
            num_sub += 1
            top_conn = self._get_dummy_connections(intv_set_list[::-1])

        # steo 2: make list of dummy transistor intervals and unused dummy track intervals
        unconnected_intv_list = []
        dum_tran_intv_list = []
        # subtract cap interval sets.
        for intv_set, cap_intv_set in zip(intv_set_list, cap_intv_set_list):
            unconnected_intv_list.append(intv_set.copy())
            temp_intv = intv_set.copy()
            for intv in cap_intv_set:
                temp_intv.subtract(intv)
            dum_tran_intv_list.append(temp_intv)

        # step 3: determine if there are tracks that can connect both substrates and all dummies
        if num_sub == 2:
            # we have both top and bottom substrate, so we can connect all dummies together
            all_conn_set = bot_conn[-1]
            del bot_conn[-1]
            del top_conn[-1]

            # remove all intervals connected by all_conn_list.
            for all_conn_intv in all_conn_set:
                for intv_set in unconnected_intv_list:
                    intv_set.remove_all_overlaps(all_conn_intv)
        else:
            all_conn_set = None

        # step 4: select dummy tracks
        bot_dum_only = top_dum_only = False
        if mos_type == 'nch':
            # for NMOS, prioritize connection to bottom substrate.
            port_name = 'VSS'
            bot_dhtr = self._select_dummy_connections(bot_conn, unconnected_intv_list, all_conn_set)
            top_dhtr = self._select_dummy_connections(top_conn, unconnected_intv_list[::-1],
                                                      all_conn_set)
            top_dum_only = not export_both
        else:
            # for PMOS, prioritize connection to top substrate.
            port_name = 'VDD'
            top_dhtr = self._select_dummy_connections(top_conn, unconnected_intv_list[::-1],
                                                      all_conn_set)
            bot_dhtr = self._select_dummy_connections(bot_conn, unconnected_intv_list, all_conn_set)
            bot_dum_only = not export_both

        # step 5: create dictionary from dummy half-track index to Y coordinates
        res = self.grid.resolution
        dum_y_table = {}
        bot_dum_tracks = []
        if bot_sub_inst is not None:
            bot_sub_port = bot_sub_inst.get_port(port_name)
            sub_yb = bot_sub_port.get_bounding_box(self.grid, self.dum_conn_layer).bottom_unit
            for htr in bot_dhtr[0]:
                dum_y_table[htr] = [sub_yb, sub_yb]
            for warr in cap_wires_dict[1]:
                lower, upper = int(round(warr.lower / res)), int(round(warr.upper / res))
                for tid in warr.track_id:
                    htr = int(2 * tid + 1)
                    if htr in dum_y_table:
                        dum_y = dum_y_table[htr]
                        dum_y[0] = min(dum_y[0], lower)
                        dum_y[1] = max(dum_y[1], upper)
                    else:
                        dum_y_table[htr] = [sub_yb, upper]
                    if tid not in bot_dum_tracks:
                        bot_dum_tracks.append(tid)

        top_dum_tracks = []
        if top_sub_inst is not None:
            top_sub_port = top_sub_inst.get_port(port_name)
            sub_yt = top_sub_port.get_bounding_box(self.grid, self.dum_conn_layer).top_unit
            for htr in top_dhtr[0]:
                if htr in dum_y_table:
                    dum_y_table[htr][1] = sub_yt
                else:
                    dum_y_table[htr] = [sub_yt, sub_yt]
            for warr in cap_wires_dict[-1]:
                lower, upper = int(round(warr.lower / res)), int(round(warr.upper / res))
                for tid in warr.track_id:
                    htr = int(2 * tid + 1)
                    if htr in dum_y_table:
                        dum_y = dum_y_table[htr]
                        dum_y[0] = min(dum_y[0], lower)
                        dum_y[1] = max(dum_y[1], upper)
                    else:
                        dum_y_table[htr] = [lower, sub_yt]
                    if tid not in top_dum_tracks:
                        top_dum_tracks.append(tid)

        # step 6: draw dummy connections
        for ridx, dum_tran_intv in enumerate(dum_tran_intv_list):
            bot_dist = ridx
            top_dist = num_rows - 1 - ridx
            dum_htr = []
            if bot_dist < len(bot_dhtr):
                dum_htr.extend(bot_dhtr[bot_dist])
            if top_dist < len(top_dhtr):
                dum_htr.extend(top_dhtr[top_dist])
            dum_htr.sort()

            for start, stop in dum_tran_intv:
                used_tracks, yb, yt = self._draw_dummy_sep_conn(mos_type, ridx, start,
                                                                stop, dum_htr)
                for htr in used_tracks:
                    dum_y = dum_y_table[htr]
                    dum_y[0] = min(dum_y[0], yb)
                    dum_y[1] = max(dum_y[1], yt)

        # step 7: draw dummy tracks to substrates
        dum_layer = self.dum_conn_layer
        for htr, dum_y in dum_y_table.items():
            dum_yb, dum_yt = dum_y
            if dum_yt > dum_yb:
                self.add_wires(dum_layer, (htr - 1) / 2, dum_y[0], dum_y[1], unit_mode=True)

        # update substrate master to only export necessary wires
        if bot_sub_inst is not None:
            bot_dum_tracks.extend((htr - 1) / 2 for htr in bot_dhtr[0])
            bot_dum_tracks.sort()
            self._export_supplies(bot_dum_tracks, bot_tracks, bot_sub_inst, bot_dum_only)
        if top_sub_inst is not None:
            top_dum_tracks.extend((htr - 1) / 2 for htr in top_dhtr[0])
            top_dum_tracks.sort()
            self._export_supplies(top_dum_tracks, top_tracks, top_sub_inst, top_dum_only)

    def _select_dummy_connections(self,  # type: AnalogBase
                                  conn_list,  # type: List[IntervalSet]
                                  unconnected,  # type: List[IntervalSet]
                                  all_conn_intv_set,  # type: Optional[IntervalSet]
                                  ):
        # type: (...) -> List[List[int]]
        """Helper method for selecting dummy tracks to connect dummies.

        First, look at the tracks that connect the most rows of dummy.  Try to use
        as many of these tracks as possible while making sure they at least connect one
        unconnected dummy.  When done, repeat on dummy tracks that connect fewer rows.

        Parameters
        ----------
        conn_list : List[IntervalSet]
            list of dummy finger intervals.  conn_list[x] contains dummy finger intervals that
            connects exactly x+1 rows.
        unconnected : List[IntervalSet]
            list of unconnected dummy finger intervals on each row.
        all_conn_intv_set : Optional[IntervalSet]
            dummy finger intervals that connect all rows.

        Returns
        -------
        dum_tracks_list : List[List[int]]
            dum_tracks_list[x] contains dummy half-track indices to draw on row X.
        """
        # step 1: find dummy tracks that connect all rows and both substrates
        if all_conn_intv_set is not None:
            dum_tracks = []
            for intv in all_conn_intv_set:
                dum_tracks.extend(self._fg_intv_to_dum_tracks(intv))
            dum_tracks_list = [dum_tracks]
        else:
            dum_tracks_list = [[]]

        # step 2: find dummy tracks that connects fewer rows
        for idx in range(len(conn_list) - 1, -1, -1):
            conn_intvs = conn_list[idx]
            cur_select_list = []
            # select finger intervals
            for intv in conn_intvs:
                select = False
                for j in range(idx + 1):
                    dummy_intv_set = unconnected[j]
                    if dummy_intv_set.has_overlap(intv):
                        select = True
                        break
                if select:
                    cur_select_list.append(intv)
            # remove connected dummy intervals, and convert finger intervals to tracks
            dum_tracks = []
            for intv in cur_select_list:
                for j in range(idx + 1):
                    unconnected[j].remove_all_overlaps(intv)
                dum_tracks.extend(self._fg_intv_to_dum_tracks(intv))

            # merge with previously selected tracks
            dum_tracks.extend(dum_tracks_list[-1])
            dum_tracks.sort()
            dum_tracks_list.append(dum_tracks)

        # flip dum_tracks_list order
        dum_tracks_list.reverse()
        return dum_tracks_list

    def _fg_intv_to_dum_tracks(self, intv):
        # type: (Tuple[int, int]) -> List[int]
        """Given a dummy finger interval, convert to dummy half-tracks.

        Parameters
        ----------
        intv : Tuple[int, int]
            the dummy finger interval.

        Returns
        -------
        dum_tracks : List[int]
            list of dummy half-track indices.
        """
        layout_info = self._layout_info
        dum_layer = self.dum_conn_layer

        col0, col1 = intv
        xl = layout_info.col_to_coord(col0, unit_mode=True)
        xr = layout_info.col_to_coord(col1, unit_mode=True)
        htr0 = int(1 + 2 * self.grid.coord_to_track(dum_layer, xl, unit_mode=True))
        htr1 = int(1 + 2 * self.grid.coord_to_track(dum_layer, xr, unit_mode=True))

        htr_pitch = self._dum_conn_pitch * 2
        start, stop = htr0 + 2, htr1
        left_adj, right_adj = True, True
        if col0 == 0:
            start = htr0
            left_adj = False
        if col1 == self._fg_tot:
            stop = htr1 + 2
            right_adj = False

        # see if we can leave some space between signal and dummy track
        if left_adj and stop - start > 2:
            start += 2
            if not right_adj:
                num_pitch = (stop - 2 - start) // htr_pitch
                start = max(start, stop - 2 - num_pitch * htr_pitch)
        if right_adj and stop - start > 2:
            stop -= 2

        return list(range(start, stop, htr_pitch))

    @classmethod
    def _get_dummy_connections(cls, intv_set_list):
        # type: (List[IntervalSet]) -> List[IntervalSet]
        """Find all dummy tracks that connects one or more rows of dummies.

        Parameters
        ----------
        intv_set_list : List[IntervalSet]
            list of used transistor finger intervals on each transistor row.  Index 0 is bottom row.

        Returns
        -------
        conn_list : List[IntervalSet]
            list of dummy finger intervals.  conn_list[x] contains dummy finger intervals that
            connects exactly x+1 rows of dummies.
        """
        # populate conn_list, such that conn_list[x] contains intervals where you can connect
        # at least x+1 rows of dummies.
        conn_list = []
        for intv_set in intv_set_list:
            if not conn_list:
                conn_list.append(intv_set.copy())
            else:
                conn_list.append(intv_set.get_intersection(conn_list[-1]))

        # subtract adjacent Intervalsets in conn_list
        for idx in range(len(conn_list) - 1):
            cur_intvs, next_intvs = conn_list[idx], conn_list[idx + 1]
            for intv in next_intvs:
                cur_intvs.subtract(intv)

        return conn_list

    def _export_supplies(self, dum_tracks, port_tracks, sub_inst, dum_only):
        x0 = self._layout_info.sd_xc_unit
        dum_tr_offset = self.grid.coord_to_track(self.dum_conn_layer, x0, unit_mode=True) + 0.5
        mconn_tr_offset = self.grid.coord_to_track(self.mos_conn_layer, x0, unit_mode=True) + 0.5
        dum_tracks = [tr - dum_tr_offset for tr in dum_tracks]
        port_tracks = [tr - mconn_tr_offset for tr in port_tracks]
        sub_inst.new_master_with(dum_tracks=dum_tracks, port_tracks=port_tracks,
                                 dummy_only=dum_only)
