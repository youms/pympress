# -*- coding: utf-8 -*-
#
#       media_overlays/gst.py
#
#       Copyright 2018 Cimbali <me@cimba.li>
#
#       This program is free software; you can redistribute it and/or modify
#       it under the terms of the GNU General Public License as published by
#       the Free Software Foundation; either version 2 of the License, or
#       (at your option) any later version.
#
#       This program is distributed in the hope that it will be useful,
#       but WITHOUT ANY WARRANTY; without even the implied warranty of
#       MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#       GNU General Public License for more details.
#
#       You should have received a copy of the GNU General Public License
#       along with this program; if not, write to the Free Software
#       Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston MA 02110-1301, USA.
#
"""
:mod:`pympress.media_overlays.gst` -- widget to play videos using Gstreamer's Gst
---------------------------------------------------------------------------------------
"""

import logging
import os
import ctypes
logger = logging.getLogger(__name__)

import gi
gi.require_version('Gst', '1.0')
gi.require_version('GstVideo', '1.0')
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gst, GstVideo, Gtk  # noqa: E402


from pympress import util
from pympress.media_overlays import base


#: Delay (ms) between the first frame reaching d3dvideosink and showing its window
D3D9_REVEAL_DELAY_MS = 30
#: Show the video window after this delay (ms) even if no frame arrived
D3D9_REVEAL_TIMEOUT_MS = 1500
#: Window class of the popups d3dvideosink draws into
POPUP_CLASS = 'PympressVideoPopup'

_win32 = None

def win32():
    """ user32 and gdk functions used for the video popups, with the popup window class registered (Windows only).

    Returns:
        `tuple`: (user32, gdk) :class:`~ctypes.CDLL` objects with argument types set
    """
    global _win32
    if _win32 is not None:
        return _win32

    user32 = ctypes.WinDLL('user32')
    kernel32 = ctypes.WinDLL('kernel32')
    user32.CreateWindowExW.restype = ctypes.c_void_p
    user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                                       ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
    user32.DestroyWindow.argtypes = [ctypes.c_void_p]
    user32.IsWindow.argtypes = [ctypes.c_void_p]
    user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.ClientToScreen.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.SetLayeredWindowAttributes.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_ubyte, ctypes.c_uint]
    user32.RegisterClassExW.argtypes = [ctypes.c_void_p]
    user32.RegisterClassExW.restype = ctypes.c_ushort
    kernel32.GetModuleHandleW.restype = ctypes.c_void_p
    kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]

    class WNDCLASSEXW(ctypes.Structure):
        _fields_ = [('cbSize', ctypes.c_uint), ('style', ctypes.c_uint), ('lpfnWndProc', ctypes.c_void_p),
                    ('cbClsExtra', ctypes.c_int), ('cbWndExtra', ctypes.c_int), ('hInstance', ctypes.c_void_p),
                    ('hIcon', ctypes.c_void_p), ('hCursor', ctypes.c_void_p), ('hbrBackground', ctypes.c_void_p),
                    ('lpszMenuName', ctypes.c_wchar_p), ('lpszClassName', ctypes.c_wchar_p),
                    ('hIconSm', ctypes.c_void_p)]

    # No background brush and the default window procedure: nothing ever paints the popup (a STATIC control painted
    # itself light grey when shown or uncovered, a white flash until the next video frame)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = ctypes.cast(user32.DefWindowProcW, ctypes.c_void_p).value
    wc.hInstance = kernel32.GetModuleHandleW(None)
    wc.lpszClassName = POPUP_CLASS
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        logger.warning('GStreamer: video popup window class not registered')

    gdk = ctypes.CDLL('libgdk-3-0.dll')
    gdk.gdk_win32_window_get_handle.restype = ctypes.c_void_p
    gdk.gdk_win32_window_get_handle.argtypes = [ctypes.c_void_p]
    _win32 = (user32, gdk)
    return _win32


class GstOverlay(base.VideoOverlay):
    """ Simple Gstramer widget.

    Wraps a simple gstreamer playbin.
    """

    #: A :class:`~Gst.Playbin` to be play videos
    playbin = None
    #: A :class:`~Gst.Base.Sink` to display video content
    sink = None

    #: `int` number of milliseconds between updates
    update_freq = 200

    #: :class:`~Gst.Element` capsfilter setting the size of the frames converted on the GPU, if any
    gpu_size_filter = None
    #: `tuple` of the video widget's size in device pixels, computed before playback
    gpu_target = None
    #: `tuple` of the size currently set on :attr:`gpu_size_filter`
    gpu_size = None
    #: `int` id of the tick callback that keeps the frame clock running during playback, if any
    frame_tick_id = None
    #: native clip dimensions and display aspect ratio from the decoder
    clip_w = None
    clip_h = None
    clip_dar = None
    #: pending timer id for debounced dynamic preview scaling
    preview_resize_timer_id = None
    #: whether playback has reached its end and is holding the last frame
    is_frozen = False

    #: `bool` whether this overlay draws with d3dvideosink in a popup window (PYMPRESS_GSTSINK=d3d9, content window)
    use_d3d9_sink = False
    #: popup window handle that d3dvideosink draws into, above the slide
    popup = None
    #: native handle of the toplevel that owns :attr:`popup`
    popup_owner = None
    #: window handle last given to d3dvideosink
    sink_hwnd = None
    #: `bool` whether the popup waits for the first frame before being shown
    reveal_pending = False
    #: `int` incremented to cancel a scheduled reveal
    reveal_gen = 0

    def __init__(self, *args, **kwargs):
        # Create GStreamer playbin
        self.playbin = Gst.ElementFactory.make('playbin', None)

        # Windows, PYMPRESS_GSTSINK=d3d9: the content window's video is drawn by d3dvideosink (Direct3D 9) in a popup
        # window owned by the content window and placed over the video area, so it is presented by the GPU off the GTK
        # main thread. Direct3D 9 output inside a GTK window got blended with what is behind the window; in a separate
        # popup it is shown as is. Only one d3dvideosink: all of them share one Direct3D 9 device and take turns at
        # the refresh rate, so the presenter window's preview stays on gtksink.
        container = args[0] if args else kwargs.get('container')
        self.use_d3d9_sink = (os.name == 'nt' and os.environ.get('PYMPRESS_GSTSINK', '').lower() == 'd3d9' and
                              container is not None and Gtk.Buildable.get_name(container) == 'c_overlay' and
                              Gst.ElementFactory.find('d3dvideosink') is not None)
        if self.use_d3d9_sink:
            self.sink = Gst.ElementFactory.make('d3dvideosink', None)
            self.sink.set_property('enable-navigation-events', False)
            # never open a window of its own: a stray window would show over the slides
            self.sink.set_property('create-render-window', False)
            self.sink.get_static_pad('sink').add_probe(Gst.PadProbeType.BUFFER, self.on_d3d9_buffer)
            # never shown: the slide stays visible under the popup
            self.video_widget = Gtk.DrawingArea()
        else:
            self.sink = Gst.ElementFactory.make('gtksink', None)
            self.video_widget = self.sink.props.widget
        self.playbin.set_property('video-sink', self.sink)
        self.setup_gpu_conversion()
        self.setup_crop_filter()

        super(GstOverlay, self).__init__(*args, **kwargs)

        self.media_overlay.remove(self.movie_zone)
        self.media_overlay.pack_start(self.video_widget, True, True, 0)
        self.media_overlay.reorder_child(self.video_widget, 0)
        self.video_widget.hide()
        if self.use_d3d9_sink:
            # follow the content window when it moves or changes size (e.g. fullscreen toggled)
            self.parent.connect('size-allocate', lambda *args: self.place_popup())
            # ... and when it moves without changing size (dragged as a window): size-allocate does not fire then, and
            # the popup would stay at the old place, showing the video over whatever is there
            toplevel = self.parent.get_toplevel()
            if toplevel is not None and toplevel.is_toplevel():
                toplevel.connect('configure-event', self.on_toplevel_configure)

        # Create bus to get events from GStreamer playin
        bus = self.playbin.get_bus()
        bus.add_signal_watch()
        bus.enable_sync_message_emission()
        bus.connect('message::eos', lambda *args: GLib.idle_add(self.handle_end))
        bus.connect('message::error', lambda _, msg: logger.error('{} {}'.format(*msg.parse_error())))
        bus.connect('message::state-changed', self.on_state_changed)
        bus.connect('sync-message::element', self.on_sync_element)
        bus.connect('message::duration-changed', lambda *args: GLib.idle_add(self.do_update_duration))


    def setup_gpu_conversion(self):
        """ Convert and scale the frames on the GPU (Direct3D 11) before they reach gtksink, if available.

        gtksink only takes BGRx or BGRA frames in system memory. Without this, playbin copies every decoded frame off
        the GPU and converts it on the CPU, then gtksink scales it to the widget with Cairo on the main thread, for
        each window. Here d3d11convert converts the colours (with the clip's colorimetry) and scales the frame to the
        widget's size, so the copy is smaller and gtksink draws it 1:1.
        """
        if Gst.ElementFactory.find('d3d11convert') is None or Gst.ElementFactory.find('d3d11download') is None:
            return

        try:
            video_filter = Gst.parse_bin_from_description(
                'd3d11convert name=gpuconvert add-borders=false ! '
                'capsfilter name=gpusize caps="video/x-raw(memory:D3D11Memory),format=BGRx" ! d3d11download', True
            )
        except GLib.Error as e:
            logger.warning('GPU conversion not set up: {}'.format(e))
            return

        flags = self.playbin.get_property('flags')
        for nick, fallback in (('deinterlace', 0x200), ('soft-colorbalance', 0x400)):
            flags = flags & ~util.introspect_flag_value(type(flags), nick, fallback)

        self.playbin.set_property('video-filter', video_filter)
        self.playbin.set_property('flags', flags)
        self.gpu_size_filter = video_filter.get_by_name('gpusize')
        video_filter.get_by_name('gpuconvert').get_static_pad('sink').add_probe(
            Gst.PadProbeType.EVENT_DOWNSTREAM, self.on_gpu_convert_event)
        if self.sink.find_property('widget') is not None:    # not every sink has a widget to follow
            self.sink.props.widget.connect('size-allocate', self.on_video_widget_allocated)
        # The size is set when the caps reach d3d11convert, before it negotiates. While playing it only grows (see
        # on_video_widget_allocated): gtksink can get stuck on the old size when the size shrinks mid-stream.


    #: `Gst.Element` the `videocrop` element that zooms into the video, or `None` if it is not available
    crop_filter = None
    #: `tuple` of `int` width and height of the frames that reach :attr:`crop_filter`, once known
    crop_input_size = None

    def setup_crop_filter(self):
        """ Add a `videocrop` element to the video filter, to follow the zoom on the slide.

        `videocrop` works on frames in system memory. Without a GPU conversion bin it is the playbin's video filter,
        with one it goes after `d3d11download` in that bin, so that the crop is the same code on every platform.
        """
        if Gst.ElementFactory.find('videocrop') is None:
            return

        crop = Gst.ElementFactory.make('videocrop', 'zoomcrop')
        video_filter = self.playbin.get_property('video-filter')
        if video_filter is None:
            self.playbin.set_property('video-filter', crop)
        elif isinstance(video_filter, Gst.Bin):
            ghost = video_filter.get_static_pad('src')
            last = ghost.get_target().get_parent_element()
            video_filter.add(crop)
            ghost.set_target(None)
            if not last.link(crop) or not ghost.set_target(crop.get_static_pad('src')):
                logger.warning('Zoom crop not set up: cannot add videocrop to the GPU conversion')
                return
        else:
            return

        self.crop_filter = crop
        crop.get_static_pad('sink').add_probe(Gst.PadProbeType.EVENT_DOWNSTREAM, self.on_crop_sink_event)


    def on_crop_sink_event(self, pad, info):
        """ Note the size of the frames that reach `videocrop`, and apply the pending crop for it.

        The crop is in pixels of the incoming frame, so a new size needs a new crop. Runs on the streaming thread, so
        GTK and pipeline calls are left to the main thread.
        """
        event = info.get_event()
        if event.type == Gst.EventType.CAPS:
            s = event.parse_caps().get_structure(0)
            ok_w, w = s.get_int('width')
            ok_h, h = s.get_int('height')
            if ok_w and ok_h:
                self.crop_input_size = (w, h)
                if any(self.crop_fractions):
                    GLib.idle_add(self.apply_crop)
        return Gst.PadProbeReturn.OK


    def apply_crop(self):
        """ Apply :attr:`crop_fractions` to `videocrop` and repaint a paused or frozen frame (main thread).
        """
        if self.crop_filter is None or self.crop_input_size is None:
            return False  # without a size the caps event will call us again

        # the crop is in pixels of the frame that videocrop receives, which may be scaled already
        w, h = self.crop_input_size
        left, right, top, bottom = (round(f * size) for f, size in zip(self.crop_fractions, (w, w, h, h)))
        # never crop the whole frame
        right = max(0, min(right, w - left - 2))
        bottom = max(0, min(bottom, h - top - 2))
        for name, value in (('left', left), ('right', right), ('top', top), ('bottom', bottom)):
            self.crop_filter.set_property(name, value)
        logger.debug('GStreamer: zoom crop left=%d right=%d top=%d bottom=%d on %dx%d', left, right, top, bottom, w, h)

        self.repaint_still_frame()
        return False


    def repaint_still_frame(self):
        """ Seek to the current position to draw the frame again, if the video is paused or frozen at its end.
        """
        ret, state, pending = self.playbin.get_state(0)
        is_actively_playing = (state == Gst.State.PLAYING and pending != Gst.State.PAUSED and not self.is_frozen)
        if is_actively_playing:
            return

        if self.is_frozen:
            self.playbin.set_state(Gst.State.PAUSED)
        ok_pos, pos = self.playbin.query_position(Gst.Format.TIME)
        ok_dur, dur = self.playbin.query_duration(Gst.Format.TIME)
        seek_pos = pos if ok_pos and pos > 0 else int(self.last_timestamp * Gst.SECOND)
        if ok_dur and dur > 0:
            # a position at the very end has no frame left to decode: stay inside the last frame
            seek_pos = min(seek_pos, max(0, dur - 5 * Gst.MSECOND))
        self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, seek_pos)


    def update_gpu_target(self):
        """ Compute the video widget's size in device pixels from the overlay margins (main thread).

        Runs before playback starts, when the widget is still hidden and not allocated.
        """
        if self.gpu_size_filter is None:
            return
        if self.use_d3d9_sink:
            # The popup gets frames at the size of the screen it is on, not of the window. The size is fixed when
            # playback starts, so a video started in a window would otherwise stay at the window's size (blurry) after
            # going fullscreen. d3dvideosink scales down to a smaller window on the GPU.
            gdk_window = self.parent.get_window()
            monitor = self.parent.get_display().get_monitor_at_window(gdk_window) if gdk_window is not None else None
            if monitor is not None:
                geometry, scale = monitor.get_geometry(), monitor.get_scale_factor()
                self.gpu_target = (geometry.width * scale, geometry.height * scale)
                return
        mo = self.media_overlay
        w = self.parent.get_allocated_width() - mo.props.margin_left - mo.props.margin_right
        h = self.parent.get_allocated_height() - mo.props.margin_top - mo.props.margin_bottom
        if self.toolbar.get_visible():
            h -= self.toolbar.get_preferred_height()[1]
        scale = self.parent.get_scale_factor()
        self.gpu_target = (int(w * scale), int(h * scale)) if w >= 16 and h >= 16 else None


    def on_gpu_convert_event(self, pad, info):
        """ Set the GPU output size when caps reach d3d11convert (streaming thread, no GTK calls).

        The size is the largest frame with the video's aspect ratio that fits the widget.
        """
        event = info.get_event()
        target = self.gpu_target
        if event.type != Gst.EventType.CAPS or target is None:
            return Gst.PadProbeReturn.OK

        s = event.parse_caps().get_structure(0)
        ok_w, w = s.get_int('width')
        ok_h, h = s.get_int('height')
        ok_par, par_n, par_d = s.get_fraction('pixel-aspect-ratio')
        if not ok_par or not par_n or not par_d:
            par_n, par_d = 1, 1
        if not ok_w or not ok_h or w <= 0 or h <= 0:
            return Gst.PadProbeReturn.OK

        self.clip_w = w
        self.clip_h = h
        dar = w * par_n / (h * par_d)
        self.clip_dar = dar

        aw, ah = target
        if aw / ah <= dar:
            size = (aw, min(ah, max(2, round(aw / dar))))
        else:
            size = (min(aw, max(2, round(ah * dar))), ah)

        if size != self.gpu_size:
            self.gpu_size = size
            self.gpu_size_filter.set_property('caps', Gst.Caps.from_string(
                'video/x-raw(memory:D3D11Memory),format=BGRx,width={},height={},pixel-aspect-ratio=1/1'.format(*size)
            ))
        return Gst.PadProbeReturn.OK


    def on_video_widget_allocated(self, widget, allocation):
        """ Handle widget allocation changes for dynamic preview sizing (main thread).

        Caps dimensions only ever increase to avoid gtksink shrink issues (gitlab issue #1082).
        """
        if self.gpu_size_filter is None or self.clip_dar is None:
            return

        scale = widget.get_scale_factor()
        aw = int(allocation.width * scale)
        ah = int(allocation.height * scale)
        dar = self.clip_dar
        if aw <= 16 or ah <= 16:
            return

        if aw / ah <= dar:
            target_w = aw
            target_h = min(ah, max(2, round(aw / dar)))
        else:
            target_w = min(aw, max(2, round(ah * dar)))
            target_h = ah

        # Cap at native clip size
        if target_w > self.clip_w or target_h > self.clip_h:
            target_w = self.clip_w if self.clip_w / dar <= self.clip_h else max(2, round(self.clip_h * dar))
            target_h = min(self.clip_h, max(2, round(target_w / dar)))

        target_w, target_h = int(target_w), int(target_h)
        current_w, current_h = self.gpu_size if self.gpu_size else (0, 0)

        # Growth only: update if either dimension exceeds the current frame by > 5%
        if target_w > current_w * 1.05 or target_h > current_h * 1.05:
            if self.preview_resize_timer_id is not None:
                GLib.source_remove(self.preview_resize_timer_id)
            self.preview_resize_timer_id = GLib.timeout_add(250, self.apply_dynamic_preview_size, target_w, target_h)


    def apply_dynamic_preview_size(self, target_w, target_h):
        """ Update the GPU scaling caps on the fly when the widget grows (main thread).
        """
        self.preview_resize_timer_id = None
        if self.gpu_size_filter is None or not self.is_shown():
            return False

        current_w, current_h = self.gpu_size if self.gpu_size else (0, 0)
        if target_w <= current_w and target_h <= current_h:
            return False

        target_w, target_h = int(target_w), int(target_h)
        self.gpu_size = (target_w, target_h)
        new_caps = Gst.Caps.from_string(
            'video/x-raw(memory:D3D11Memory),format=BGRx,width={},height={},pixel-aspect-ratio=1/1'.format(
                target_w, target_h)
        )
        self.gpu_size_filter.set_property('caps', new_caps)

        self.repaint_still_frame()

        return False


    def start_frame_tick(self):
        """ Keep GTK's frame clock running while the video plays (main thread, Windows only).

        When the frame clock only wakes up for gtksink's redraw requests, GTK on Windows misses about one refresh in
        four and drops those frames. A tick callback keeps the clock running on every refresh.
        """
        if os.name != 'nt' or self.use_d3d9_sink or self.frame_tick_id is not None:
            return
        self.frame_tick_id = self.video_widget.add_tick_callback(self.on_frame_tick)


    def on_frame_tick(self, widget, frame_clock):
        """ Frame clock tick: stop ticking once the player is no longer playing or about to play.

        Returns:
            `bool`: `True` to keep ticking (:meth:`~Gtk.Widget.add_tick_callback` convention)
        """
        ret, state, pending = self.playbin.get_state(0)
        if state == Gst.State.PLAYING or pending == Gst.State.PLAYING:
            return True
        self.frame_tick_id = None
        return False


    def stop_frame_tick(self):
        """ Remove the frame clock tick callback, if any (main thread).
        """
        if self.frame_tick_id is not None:
            self.video_widget.remove_tick_callback(self.frame_tick_id)
            self.frame_tick_id = None


    def popup_geometry(self):
        """ Where the video goes, in screen pixels, from the overlay margins (main thread).

        Returns:
            `tuple`: (owner window handle, x, y, width, height), or `None` if the content window is not ready
        """
        toplevel = self.parent.get_toplevel()
        top_window = toplevel.get_window() if toplevel is not None else None
        parent_window = self.parent.get_window()
        if top_window is None or parent_window is None:
            return None
        mo = self.media_overlay
        w = self.parent.get_allocated_width() - mo.props.margin_left - mo.props.margin_right
        h = self.parent.get_allocated_height() - mo.props.margin_top - mo.props.margin_bottom
        if self.toolbar.get_visible():
            h -= self.toolbar.get_preferred_height()[1]
        if w < 16 or h < 16:
            return None
        # position in the client area of the toplevel's native window: origin of the parent's GDK window relative to
        # the toplevel's, plus the parent's position in its GDK window, plus the margins
        x0, y0 = parent_window.get_origin()[1:]
        tx, ty = top_window.get_origin()[1:]
        if self.parent.get_has_window():
            px, py = 0, 0
        else:
            alloc = self.parent.get_allocation()
            px, py = alloc.x, alloc.y
        user32, gdk = win32()
        owner = gdk.gdk_win32_window_get_handle(hash(top_window))
        scale = self.parent.get_scale_factor()
        point = (ctypes.c_long * 2)((x0 - tx + px + mo.props.margin_left) * scale,
                                    (y0 - ty + py + mo.props.margin_top) * scale)
        user32.ClientToScreen(owner, point)
        return owner, point[0], point[1], w * scale, h * scale


    def on_toplevel_configure(self, widget, event):
        """ The content window moved or changed size: move the popup with it, once GTK has handled the change.

        Args:
            widget (:class:`~Gtk.Widget`): the content window
            event (:class:`~Gdk.EventConfigure`): the configure event

        Returns:
            `bool`: `False`, to let GTK handle the event as well
        """
        if self.popup is not None:
            GLib.idle_add(lambda: self.place_popup() and False)
        return False


    def place_popup(self):
        """ Create the (hidden) popup window if needed, or move it over the video area (main thread).

        Returns:
            `bool`: `True` iff the popup exists and is in place
        """
        if not self.use_d3d9_sink or (self.popup is None and not self.is_shown()):
            return False
        geometry = self.popup_geometry()
        if geometry is None:
            return False
        owner, x, y, w, h = geometry
        user32, gdk = win32()
        if self.popup is not None and (owner != self.popup_owner or not user32.IsWindow(self.popup)):
            self.destroy_popup()
        if self.popup is None:
            # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW | WS_EX_LAYERED | WS_EX_TRANSPARENT, WS_POPUP: no focus, no taskbar
            # entry, clicks go through, created hidden. Owned by the content window, so it stays above it and is
            # hidden when it is minimised. Layered, so it can be shown fully transparent while the sink starts.
            self.popup = user32.CreateWindowExW(0x08000000 | 0x80 | 0x80000 | 0x20, POPUP_CLASS, None, 0x80000000,
                                                x, y, w, h, owner, None, None, None)
            user32.SetLayeredWindowAttributes(self.popup, 0, 0, 2)    # LWA_ALPHA, fully transparent
            self.popup_owner = owner
        else:
            user32.SetWindowPos(self.popup, None, x, y, w, h, 0x0004 | 0x0010)   # SWP_NOZORDER | SWP_NOACTIVATE
        return bool(self.popup)


    def hide_popup(self):
        """ Hide the video popup and cancel a pending reveal (main thread).
        """
        self.reveal_gen += 1
        self.reveal_pending = False
        if self.popup is not None:
            user32 = win32()[0]
            user32.SetLayeredWindowAttributes(self.popup, 0, 0, 2)     # transparent first, then hidden
            user32.ShowWindow(self.popup, 0)                           # SW_HIDE


    def destroy_popup(self):
        """ Destroy the video popup window (main thread, sink stopped).
        """
        if self.popup is not None:
            win32()[0].DestroyWindow(self.popup)
            self.popup = None
            self.popup_owner = None
            # a new popup can get the same handle value: make sure it is handed over again
            self.sink_hwnd = None


    def dispose(self):
        """ Release the player and its popup window (called when the overlays are purged or Pympress exits).
        """
        if self.use_d3d9_sink:
            self.hide_popup()
            self.playbin.set_state(Gst.State.NULL)
            self.destroy_popup()


    def on_d3d9_buffer(self, pad, info):
        """ First frame reached d3dvideosink: show its popup a little later (streaming thread, no GTK calls).
        """
        if self.reveal_pending:
            self.reveal_pending = False
            GLib.timeout_add(D3D9_REVEAL_DELAY_MS, self.reveal_popup, self.reveal_gen)
        return Gst.PadProbeReturn.OK


    def reveal_popup(self, gen):
        """ Make the popup opaque once the sink has drawn a frame into it (main thread).

        Until then the slide, whose first frame should match the video, stays visible: d3dvideosink fills its window
        with grey before the first frame. The popup is already shown, fully transparent, so the frames are in it
        (Direct3D does not draw into a hidden window) and nothing has to paint at the reveal.

        Args:
            gen (`int`): the value of :attr:`reveal_gen` when the reveal was scheduled

        Returns:
            `bool`: `False` (:func:`~GLib.timeout_add` convention)
        """
        if gen == self.reveal_gen and self.popup is not None and self.is_shown():
            self.reveal_gen += 1
            self.reveal_pending = False
            win32()[0].SetLayeredWindowAttributes(self.popup, 0, 255, 2)
        return False


    def prepare_d3d9_play(self):
        """ Popup in place and shown fully transparent, sink attached, reveal armed (main thread, before PLAYING).

        Returns:
            `bool`: `True` iff playback can start
        """
        if not self.place_popup():
            logger.error('GStreamer: no popup window for d3dvideosink')
            return False
        self.hide_popup()
        win32()[0].ShowWindow(self.popup, 8)                           # SW_SHOWNA, still fully transparent
        # Only hand over a new popup: the sink forgets its window when it stops and prepares it again when it starts
        if self.sink_hwnd != self.popup:
            self.sink.set_window_handle(self.popup)
            self.sink_hwnd = self.popup
        self.reveal_pending = True
        GLib.timeout_add(D3D9_REVEAL_TIMEOUT_MS, self.reveal_popup, self.reveal_gen)
        return True


    def on_sync_element(self, bus, msg):
        """ Give d3dvideosink the popup if it asks for a window (streaming thread, no GTK calls).
        """
        if self.use_d3d9_sink and self.popup and GstVideo.is_video_overlay_prepare_window_handle_message(msg):
            msg.src.set_window_handle(self.popup)


    def is_playing(self):
        """ Returns whether the media is currently playing (and not paused).

        Returns:
            `bool`: `True` iff the media is playing.
        """
        return self.playbin.get_state(0).state == Gst.State.PLAYING


    def _set_file(self, filepath):
        """ Sets the media file to be played by the widget.

        Args:
            filepath (`pathlib.Path`): The path to the media file path
        """
        self.playbin.set_property('uri', filepath.as_uri())
        self.playbin.set_state(Gst.State.READY)


    def mute(self, value):
        """ Mutes or unmutes the player.

        Args:
            value (`bool`): `True` iff this player should be muted
        """
        flags = self.playbin.get_property('flags')
        # Fall back to the documented value if introspection fails,
        # see https://gstreamer.freedesktop.org/documentation/playback/playsink.html?gi-language=python#GstPlayFlags
        audio_flag = util.introspect_flag_value(type(flags), 'audio', 0x02)
        if value:
            flags = flags & ~audio_flag
        else:
            flags = flags | audio_flag
        self.playbin.set_property('flags', flags)
        return False


    def on_state_changed(self, bus, msg):
        """ Callback triggered by playbin state changes.

        Args:
            bus (:class:`~Gst.Bus`): the bus that we are connected to
            msg (:class:`~Gst.Message`): the "state-changed" message
        """
        if msg.src != self.playbin:
            # ignore the playbin's children
            return
        old, new, pending = msg.parse_state_changed()
        if old == Gst.State.READY and new == Gst.State.PAUSED:
            # the playbin goes from READY (= stopped) to PLAYING (via PAUSED)
            self.on_initial_play()


    def on_initial_play(self):
        """ Set starting position, start scrollbar updates, unhide overlay. """
        # set starting position, if needed
        if self.start_pos:
            self.do_set_time(self.start_pos)
        # ensure the scroll bar is updated
        GLib.idle_add(self.do_update_duration)
        GLib.timeout_add(self.update_freq, self.do_update_time)
        # ensure the overlay is visible (if needed)
        if not self.media_type.startswith('audio') and not self.use_d3d9_sink:
            self.video_widget.show()
            self.start_frame_tick()


    def do_update_duration(self, *args):
        """ Transmit the change of file duration to the UI to adjust the scroll bar.
        """
        changed, time_ns = self.playbin.query_duration(Gst.Format.TIME)
        self.update_range(max(0, time_ns) / 1e9)


    def do_update_time(self):
        """ Update the current position in the progress bar.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.timeout_add` convention)
        """
        changed, time_ns = self.playbin.query_position(Gst.Format.TIME)
        time = time_ns / 1e9
        self.update_progress(time)
        if self.last_timestamp <= self.end_pos <= time:
            self.handle_end()
        self.last_timestamp = time
        return self.playbin.get_state(0).state in {Gst.State.PLAYING, Gst.State.PAUSED}


    def handle_end(self):
        """ End of the stream reached: mark frozen and stop frame tick.
        """
        self.is_frozen = True
        self.stop_frame_tick()
        super(GstOverlay, self).handle_end()


    def do_play(self):
        """ Start playing the media file.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        self.is_frozen = False
        self.update_gpu_target()
        if self.use_d3d9_sink and not self.is_playing() and not self.prepare_d3d9_play():
            return False
        self.playbin.set_state(Gst.State.PLAYING)

        return False


    def do_play_pause(self):
        """ Toggle pause mode of the media.

        Should run on the main thread to ensure we avoid reentrency problems.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        self.is_frozen = False
        self.update_gpu_target()
        if (self.use_d3d9_sink and not self.is_playing() and self.playbin.get_state(0)[1] != Gst.State.PAUSED and
                not self.prepare_d3d9_play()):
            return False
        self.playbin.set_state(Gst.State.PLAYING if not self.is_playing() else Gst.State.PAUSED)
        self.start_frame_tick()

        return False


    def do_stop(self):
        """ Stops playing in the backend player.
        """
        if self.preview_resize_timer_id is not None:
            GLib.source_remove(self.preview_resize_timer_id)
            self.preview_resize_timer_id = None
        self.is_frozen = False
        self.stop_frame_tick()
        # hide the popup before stopping, so the slide shows instead of the sink's last frame
        if self.use_d3d9_sink:
            self.hide_popup()
        self.playbin.set_state(Gst.State.NULL)
        self.playbin.set_state(Gst.State.READY)
        self.video_widget.hide()

        return False


    def do_set_time(self, time):
        """ Set the player at time `~time`.

        Should run on the main thread to ensure we avoid reentrency problems.

        Args:
            time (`float`): the timestamp, in s

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        self.last_timestamp = time
        self.playbin.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH, time * Gst.SECOND)
        return False


    @classmethod
    def setup_backend(cls, gst_opts = []):
        """ Prepare/check the Gst backend.

        Returns:
            `str`: the version of Gst used by the backend
        """
        Gst.init(gst_opts)

        if Gst.ElementFactory.make('gtksink', None) is None:
            logger.warning('Can not create a gtksink. Check the gtk plugin for GStreamer is installed.')
            logger.warning('See https://github.com/Cimbali/pympress/issues/240')
            raise ValueError('Can not create a gtksink.')

        return Gst.version_string()
