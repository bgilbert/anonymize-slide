This fork is based on the original repo in https://github.com/bgilbert/anonymize-slide

Several users (markemus, r3m0chop and grenkoca) have updated the code to make it runnable with Python 3. Since non of their versions worked with my mrxs
files I decided to fork the version from markemus in which I could fix the issues with the mrxs files. Please note that all these files are currently in the
old mrxs format (with some of them being converted from the new into the old format). So far I did not test the new format.

Please note that the original readme below does not respect the current state of the script.



anonymize-slide
===============

This is a program to remove the slide label from whole-slide images in the
following formats:

 * Aperio SVS
 * Hamamatsu NDPI
 * 3DHISTECH MRXS

Slide files are modified **in place**, making this program both fast and
potentially destructive.  Do not run it on your only copy of a slide.

[Download](https://github.com/bgilbert/anonymize-slide/releases)

Examples
--------

Delete the label from `slide.mrxs`:

    anonymize-slide.py slide.mrxs

Delete the label from all NDPI files in the current directory:

    anonymize-slide.py *.ndpi

Requirements
------------

 * Python 2.6 or 2.7

License
-------

This program is distributed under the [GNU General Public License, version
2](COPYING).

No Warranty
-----------

This program is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
FITNESS FOR A PARTICULAR PURPOSE.  See the [license](COPYING) for more
details.
