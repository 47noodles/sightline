# Icons

## Lucide (`lucide/*.svg`)

* Source: https://github.com/lucide-icons/lucide, `icons/`, release 1.61.0.
* Licence: ISC; some icons derive from Feather (MIT).  The full licence,
  including the list of Feather-derived icons, is in `lucide/LICENSE`.
* Unmodified files.  At run time `ui/icons.py` replaces `currentColor` with
  the theme's ink colour and may change the stroke width before rendering.

Files: arrow-left, chevrons-down, chevrons-up, circle-alert,
circle-question-mark, ellipsis, eye, eye-off, hash, info, keyboard, mic,
mic-off, moon, mouse-left, mouse-pointer-2, mouse-pointer-click,
mouse-right, move, pause, play, scan-eye, settings, timer, triangle-alert,
x, zoom-in.

Added for the settings window, wizards and practice room: arrow-right,
check, crosshair, minus, plus, refresh-cw, target.  These seven are a few lines each and
were written out from Lucide's published path data rather than copied from
the release archive; they are the same ISC-licensed icons.

## The Sightline mark

A ring with a solid centre (two bars when eye control is paused).  It is
drawn in code (`ui/icons.py`, `draw_mark`) at every size it is needed, so
there is no image file to keep in step.  Part of Sightline, Apache-2.0.
