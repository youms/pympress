# -*- coding: utf-8 -*-
#
#       media_overlays/base.py
#
#       Copyright 2015 Cimbali <me@cimba.li>
#
#       Vaguely inspired from:
#       gtk example/widget for VLC Python bindings
#       Copyright (C) 2009-2010 the VideoLAN team
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
:mod:`pympress.media_overlays.base` -- base widget to play videos with an unspecified backend
---------------------------------------------------------------------------------------------
"""

import logging
logger = logging.getLogger(__name__)

import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gio

from pympress import document, builder


class VideoOverlay(builder.Builder):
    """ Simple Video widget.

    All do_X() functions are meant to be called from the main thread, through e.g. :func:`~GLib.idle_add`,
    for thread-safety in the handling of video backends.

    Args:
        container (:class:`~Gtk.Overlay`): The container with the slide, at the top of which we add the movie area
        page_type (:class:`~pympress.document.PdfPage`): the part of the page to display
        action_map (:class:`~Gio.ActionMap`): the action map that contains the actions for this media
        media (:class:`~pympress.document.Media`): the object defining the properties of the video such as position etc.
    """
    #: :class:`~Gtk.Overlay` that is the parent of the VideoOverlay widget.
    parent = None
    #: `str` what a video does at its end when the PDF does not ask it to repeat: 'hide', 'freeze' or 'loop'
    #: (`video_end` in the `media` section of the configuration)
    end_action = 'hide'
    #: :class:`~Gtk.VBox` that contains all the elements to be overlaid.
    media_overlay = None
    #: A :class:`~Gtk.HBox` containing a toolbar with buttons and :attr:`~progress` the progress bar
    toolbar = None
    #: :class:`~Gtk.Scale` that is the progress bar in the controls toolbar - if we have one.
    progress = None
    #: :class:`~Gtk.DrawingArea` where the media is rendered.
    movie_zone = None
    #: `tuple` containing the left/top/right/bottom coordinates of the drawing area in the PDF page
    relative_rect = None
    #: `tuple` containing the left/top/right/bottom coordinates of the drawing area in the visible slide
    rect = None
    #: `bool` that tracks whether we should play automatically
    autoplay = False
    #: `bool` that tracks whether we should play after we finished playing
    repeat = False
    #: `str` representing the mime type of the media file
    media_type = ''
    #: `float` giving the initial starting position for playback
    start_pos = 0.
    #: `float` giving the end position for playback, if any
    end_pos = None
    #: `float` representing the last know timestamp at which the progress bar updated
    last_timestamp = 0.

    #: `bool` that tracks whether the user is dragging the position
    dragging_position = False
    #: `bool` that tracks whether the playback was paused when the user started dragging the position
    dragging_paused = False
    #: Format of the video time, defaults to m:ss, changed to m:ss / m:ss when the max time is known
    time_format = '{:01}:{:02}'
    #: `float` holding the max time in s
    maxval = 1

    #: :class:`~Gio.ActionMap` containing the actios for this video overlay
    action_map = None

    def __init__(self, container, page_type, action_map, media):
        super(VideoOverlay, self).__init__()

        self.parent = container
        self.relative_rect = (media.x1, media.y1, media.x2, media.y2)
        self.update_margins_for_page(page_type)

        self.load_ui('media_overlay')
        self.toolbar.set_visible(media.show_controls)

        self.connect_signals(self)

        # medias, here the actions are scoped to the current widget
        self.action_map = action_map
        self.media_overlay.insert_action_group('media', self.action_map)
        if media.type:
            self.media_type = media.type
        else:
            content_type, uncertain = Gio.content_type_guess(media.filename.as_uri())
            self.media_type = Gio.content_type_get_mime_type(content_type)
        self.start_pos = media.start_pos
        self.end_pos = float('inf') if media.duration == 0 else media.start_pos + media.duration
        self._set_file(media.filename)

        self.autoplay = media.autoplay
        self.repeat = media.repeat
        # TODO: handle poster


    def handle_embed(self, mapped_widget):
        """ Handler to embed the video player in the window, connected to the :attr:`~.Gtk.Widget.signals.map` signal.
        """
        return False


    def format_millis(self, sc, prog):
        """ Callback to format the current timestamp (in milliseconds) as minutes:seconds.

        Args:
            sc (:class:`~Gtk.Scale`): The scale whose position we are formatting
            prog (`float`): The position of the :class:`~Gtk.Scale`, i.e. the number of seconds elapsed
        """
        return self.time_format.format(*divmod(int(round(prog)), 60))


    def update_range(self, max_time):
        """ Update the toolbar slider size.

        Args:
            max_time (`float`): The maximum time in this video in s
        """
        self.maxval = max_time
        self.progress.set_range(0, self.maxval)
        self.progress.set_increments(min(5., self.maxval / 10.), min(60., self.maxval / 10.))
        sec = round(self.maxval) if self.maxval > .5 else 1.
        self.time_format = '{{:01}}:{{:02}} / {:01}:{:02}'.format(*divmod(int(sec), 60))


    def update_progress(self, time):
        """ Update the toolbar slider to the current time.

        Args:
            time (`float`): The time in this video in s
        """
        self.progress.set_value(time)


    def progress_moved(self, rng, sc, val):
        """ Callback to update the position of the video when the user moved the progress bar.

        Args:
            rng (:class:`~Gtk.Range`): The range corresponding to the scale whose position we are formatting
            sc (:class:`~Gtk.Scale`): The scale whose position we are updating
            val (`float`): The position of the :class:`~Gtk.Scale`, which is the number of seconds elapsed in the video
        """
        return self.action_map.lookup_action('set_time').activate(GLib.Variant.new_double(val))


    def play_pause(self, *args):
        """ Callback to toggle play/pausing from clicking on the DrawingArea
        """
        return self.action_map.lookup_action('pause').activate()


    def handle_end(self):
        """ End of the stream reached.

        Restart if looping, otherwise follow the `video_end` setting: hide the overlay, keep the last frame on screen,
        or loop.
        """
        if self.repeat or self.end_action == 'loop':
            self.action_map.lookup_action('set_time').activate(GLib.Variant.new_double(self.start_pos))
        elif self.end_action == 'freeze':
            # leave the player at its end: the video output keeps showing the last frame until the slide changes
            pass
        else:
            self.action_map.lookup_action('stop').activate()


    def update_margins_for_page(self, page_type):
        """ Recalculate the margins around the media in the event of a page type change.

        Arguments:
            page_type (:class:`~pympress.document.PdfPage`): the part of the page to display
        """
        left, top, right, bot = page_type.to_screen(*self.relative_rect)

        # Some configurations generate incorrect media positions. Assume no one intentionally puts media on notes pages.
        if page_type == document.PdfPage.RIGHT and -1 <= left < right <= 0:
            left, right = left + 1, right + 1
            logger.warning('Shifting media from LEFT notes page to RIGHT content page')

        if page_type == document.PdfPage.TOP and 1 <= top < bot <= 2:
            top, bot = top - 1, bot - 1
            logger.warning('Shifting media from BOTTOM notes page to TOP content page')

        self.rect = left, top, right, bot
        if min(self.rect) < 0 or max(self.rect) > 1:
            logger.warning('Negative margin(s) clipped to 0 (might alter the aspect ratio?): ' +
                           'LTRB = {}'.format(self.rect))


    #: Current zoom scale applied to the slide (>= 1.0)
    zoom_scale = 1.0
    #: Current zoom shift applied to the slide (x, y in normalized slide space)
    zoom_shift = (0.0, 0.0)
    #: Normalized crop fractions (left, right, top, bottom) relative to the media stream (0..1)
    crop_fractions = (0.0, 0.0, 0.0, 0.0)
    #: `bool` whether pointer events go through this overlay to the slide below it
    pass_through = False

    def set_pass_through(self, pass_through):
        """ Let pointer events go through the overlay to the slide below, or catch them again.

        This lets the user draw the zoom selection over a video.

        Args:
            pass_through (`bool`): whether pointer events go through the overlay
        """
        self.pass_through = pass_through
        if self.is_shown():
            self.parent.set_overlay_pass_through(self.media_overlay, pass_through)
        self.apply_window_pass_through()


    def apply_window_pass_through(self):
        """ Apply :attr:`pass_through` to the window of the widget that draws the video, which catches events itself.
        """
        widget = getattr(self, 'video_widget', None) or self.movie_zone
        window = widget.get_window() if widget is not None else None
        if window is not None:
            window.set_pass_through(self.pass_through)


    def set_zoom(self, scale, shift):
        """ Apply zoom transformation to this overlay.

        Args:
            scale (float): Cairo zoom factor (>= 1.0)
            shift (tuple of float): (shift_x, shift_y) in normalized slide coordinates
        """
        self.zoom_scale = scale
        self.zoom_shift = shift
        self.resize()


    def apply_crop(self):
        """ Backend hook to apply crop_fractions to video sink/pipeline. Subclasses override.
        """
        pass


    def resize(self):
        """ Adjust the position, size, and crop margins of the media overlay under current zoom.
        """
        if not self.is_shown():
            logger.debug('VideoOverlay.resize: not shown (parent=%s)', self.media_overlay.get_parent())
            return

        pw, ph = self.parent.get_allocated_width(), self.parent.get_allocated_height()
        if pw <= 1 or ph <= 1:
            logger.debug('VideoOverlay.resize: allocation <= 1 (%dx%d)', pw, ph)
            return

        left, top, right, bot = self.rect
        scale = self.zoom_scale
        shift_x, shift_y = self.zoom_shift

        # Zoomed coordinates in slide screen space
        scr_left = scale * left + shift_x
        scr_right = scale * right + shift_x
        scr_top = scale * top + shift_y
        scr_bot = scale * bot + shift_y

        # Clamp to visible viewport [0, 1] x [0, 1]
        vis_left = max(0.0, scr_left)
        vis_right = min(1.0, scr_right)
        vis_top = max(0.0, scr_top)
        vis_bot = min(1.0, scr_bot)

        # If completely outside visible slide area
        if vis_left >= vis_right or vis_top >= vis_bot:
            self.media_overlay.set_visible(False)
            self.crop_fractions = (0.0, 0.0, 0.0, 0.0)
            self.apply_crop()
            return

        self.media_overlay.set_visible(True)

        # Margins on the GTK overlay container (always >= 0)
        self.media_overlay.props.margin_left = int(round(pw * vis_left))
        self.media_overlay.props.margin_right = int(round(pw * (1.0 - vis_right)))
        self.media_overlay.props.margin_top = int(round(ph * vis_top))
        self.media_overlay.props.margin_bottom = int(round(ph * (1.0 - vis_bot)))

        # Normalized crop fractions inside the media stream
        media_w = scr_right - scr_left
        media_h = scr_bot - scr_top
        crop_left = max(0.0, (vis_left - scr_left) / media_w) if media_w > 0 else 0.0
        crop_right = max(0.0, (scr_right - vis_right) / media_w) if media_w > 0 else 0.0
        crop_top = max(0.0, (vis_top - scr_top) / media_h) if media_h > 0 else 0.0
        crop_bottom = max(0.0, (scr_bot - vis_bot) / media_h) if media_h > 0 else 0.0

        self.crop_fractions = (crop_left, crop_right, crop_top, crop_bottom)
        logger.debug('VideoOverlay.resize: margins=(%d, %d, %d, %d), crop=(%.3f, %.3f, %.3f, %.3f)',
                     self.media_overlay.props.margin_left, self.media_overlay.props.margin_right,
                     self.media_overlay.props.margin_top, self.media_overlay.props.margin_bottom,
                     *self.crop_fractions)
        self.apply_crop()


    def is_shown(self):
        """ Returns whether the media overlay is currently added to the overlays, or hidden.

        Returns:
            `bool`: `True` iff the overlay is currently displayed.
        """
        return self.media_overlay.get_parent() is not None


    def is_playing(self):
        """ Returns whether the media is currently playing (and not paused).

        Returns:
            `bool`: `True` iff the media is playing.
        """
        raise NotImplementedError


    def do_stop(self):
        """ Stops playing in the backend player.
        """
        raise NotImplementedError


    def _set_file(self, filepath):
        """ Sets the media file to be played by the widget.

        Args:
            filepath (`pathlib.Path`): The path to the media file path
        """
        raise NotImplementedError


    def show(self):
        """ Bring the widget to the top of the overlays if necessary.
        """
        if min(self.rect) < 0 or max(self.rect) > 1:
            logger.warning('Negative margin(s) clipped to 0 (might alter the aspect ratio?): ' +
                           'LTRB = {}'.format(self.rect))

        if not self.media_overlay.get_parent():
            self.parent.add_overlay(self.media_overlay)
            self.parent.reorder_overlay(self.media_overlay, 2)
            self.parent.set_overlay_pass_through(self.media_overlay, self.pass_through)
            self.resize()
            self.parent.queue_draw()
        self.media_overlay.show()
        self.apply_window_pass_through()


    def do_hide(self, *args):
        """ Remove widget from overlays. Needs to be called via :func:`~GLib.idle_add`.

        Returns:
            `bool`: `True` iff this function should be run again (:func:`~GLib.idle_add` convention)
        """
        self.do_stop()
        self.media_overlay.hide()

        if self.media_overlay.get_parent():
            self.parent.remove(self.media_overlay)
        self.parent.queue_draw()
        return False


    def do_play(self):
        """ Start playing the media file.

        Should run on the main thread to ensure we avoid reentrency problems.

        Returns:
            `bool`: `True` iff this function should be run again (:meth:`~GLib.idle_add` convention)
        """
        raise NotImplementedError


    def do_play_pause(self):
        """ Toggle pause mode of the media.

        Should run on the main thread to ensure we avoid reentrency problems.

        Returns:
            `bool`: `True` iff this function should be run again (:meth:`~GLib.idle_add` convention)
        """
        raise NotImplementedError


    def do_set_time(self, t):
        """ Set the player at time t.

        Should run on the main thread to ensure we avoid vlc plugins' reentrency problems.

        Args:
            t (`float`): the timestamp, in s

        Returns:
            `bool`: `True` iff this function should be run again (:meth:`~GLib.idle_add` convention)
        """
        raise NotImplementedError
