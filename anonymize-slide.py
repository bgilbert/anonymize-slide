#!/usr/bin/python
#
#  anonymize-slide.py - Delete the label from a whole-slide image.
#
#  Copyright (c) 2012-2013 Carnegie Mellon University
#  Copyright (c) 2014      Benjamin Gilbert
#  All rights reserved.
#
#  This program is free software: you can redistribute it and/or modify
#  it under the terms of version 2 of the GNU General Public License as
#  published by the Free Software Foundation.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software
#  Foundation, Inc., 51 Franklin Street, Fifth Floor,
#  Boston, MA 02110-1301 USA.
#

from optparse import OptionParser
import string
import struct
import sys

PROG_DESCRIPTION = '''
Delete the slide label from an SVS or NDPI whole-slide image.
'''.strip()
PROG_VERSION = '1.0'
DEBUG = False

# TIFF types
ASCII = 2
SHORT = 3
LONG = 4
FLOAT = 11
DOUBLE = 12
LONG8 = 16

# TIFF tags
IMAGE_DESCRIPTION = 270
STRIP_OFFSETS = 273
STRIP_BYTE_COUNTS = 279
NDPI_MAGIC = 65420
NDPI_SOURCELENS = 65421

class UnrecognizedFile(Exception):
    pass


class TiffFile(file):
    def __init__(self, path):
        file.__init__(self, path, 'r+b')

        # Check header, decide endianness
        endian = self.read(2)
        if endian == 'II':
            self._fmt_prefix = '<'
        elif endian == 'MM':
            self._fmt_prefix = '>'
        else:
            raise UnrecognizedFile

        # Check TIFF version
        self._bigtiff = False
        version = self.read_fmt('H')
        if version == 42:
            pass
        elif version == 43:
            self._bigtiff = True
            magic2, reserved = self.read_fmt('HH')
            if magic2 != 8 or reserved != 0:
                raise UnrecognizedFile
        else:
            raise UnrecognizedFile

        # Read directories
        self.directories = []
        while True:
            in_pointer_offset = self.tell()
            directory_offset = self.read_fmt('Z')
            if directory_offset == 0:
                break
            self.seek(directory_offset)
            directory = TiffDirectory(self, len(self.directories),
                    in_pointer_offset)
            self.directories.append(directory)
        if not self.directories:
            raise IOError('No directories')

    def _convert_format(self, fmt):
        # Format strings can have special characters:
        # y: 16-bit   signed on little TIFF, 64-bit   signed on BigTIFF
        # Y: 16-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF
        # z: 32-bit   signed on little TIFF, 64-bit   signed on BigTIFF
        # Z: 32-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF
        if self._bigtiff:
            fmt = fmt.translate(string.maketrans('yYzZ', 'qQqQ'))
        else:
            fmt = fmt.translate(string.maketrans('yYzZ', 'hHiI'))
        return self._fmt_prefix + fmt

    def fmt_size(self, fmt):
        return struct.calcsize(self._convert_format(fmt))

    def read_fmt(self, fmt, force_list=False):
        fmt = self._convert_format(fmt)
        vals = struct.unpack(fmt, self.read(struct.calcsize(fmt)))
        if len(vals) == 1 and not force_list:
            return vals[0]
        else:
            return vals

    def write_fmt(self, fmt, *args):
        fmt = self._convert_format(fmt)
        self.write(struct.pack(fmt, *args))


class TiffDirectory(object):
    def __init__(self, fh, number, in_pointer_offset):
        self.entries = {}
        count = fh.read_fmt('Y')
        for _ in range(count):
            entry = TiffEntry(fh)
            self.entries[entry.tag] = entry
        self._in_pointer_offset = in_pointer_offset
        self._out_pointer_offset = fh.tell()
        self._fh = fh
        self._number = number

    def delete(self):
        # Get strip offsets/lengths
        try:
            offsets = self.entries[STRIP_OFFSETS].value()
            lengths = self.entries[STRIP_BYTE_COUNTS].value()
        except KeyError:
            raise IOError('Directory is not stripped')

        # Wipe strips
        for offset, length in zip(offsets, lengths):
            if DEBUG:
                print 'Zeroing', offset, 'for', length
            self._fh.seek(offset)
            self._fh.write('\0' * length)

        # Remove directory
        if DEBUG:
            print 'Deleting directory', self._number
        self._fh.seek(self._out_pointer_offset)
        out_pointer = self._fh.read_fmt('Z')
        self._fh.seek(self._in_pointer_offset)
        self._fh.write_fmt('Z', out_pointer)


class TiffEntry(object):
    def __init__(self, fh):
        self.start = fh.tell()
        self.tag, self.type, self.count, self.value_offset = \
                fh.read_fmt('HHZZ')
        self._fh = fh

    def value(self):
        if self.type == ASCII:
            item_fmt = 'c'
        elif self.type == SHORT:
            item_fmt = 'H'
        elif self.type == LONG:
            item_fmt = 'I'
        elif self.type == LONG8:
            item_fmt = 'Q'
        elif self.type == FLOAT:
            item_fmt = 'f'
        elif self.type == DOUBLE:
            item_fmt = 'd'
        else:
            raise ValueError('Unsupported type')

        fmt = '%d%s' % (self.count, item_fmt)
        len = self._fh.fmt_size(fmt)
        if len <= self._fh.fmt_size('Z'):
            # Inline value
            self._fh.seek(self.start + self._fh.fmt_size('HHZ'))
        else:
            # Out-of-line value
            self._fh.seek(self.value_offset)
        items = self._fh.read_fmt(fmt, force_list=True)
        if self.type == ASCII:
            if items[-1] != '\0':
                raise ValueError('String not null-terminated')
            return ''.join(items[:-1])
        else:
            return items


def accept(filename, format):
    if DEBUG:
        print filename + ':', format


def do_aperio_svs(filename):
    with TiffFile(filename) as fh:
        # Check for SVS file
        try:
            desc0 = fh.directories[0].entries[IMAGE_DESCRIPTION].value()
            if not desc0.startswith('Aperio'):
                raise UnrecognizedFile
        except KeyError:
            raise UnrecognizedFile
        accept(filename, 'SVS')

        # Find and delete label
        for directory in fh.directories:
            lines = directory.entries[IMAGE_DESCRIPTION].value().splitlines()
            if len(lines) >= 2 and lines[1].startswith('label '):
                directory.delete()
                break
        else:
            raise IOError("No label in SVS file")


def do_hamamatsu_ndpi(filename):
    with TiffFile(filename) as fh:
        # Check for NDPI file
        if NDPI_MAGIC not in fh.directories[0].entries:
            raise UnrecognizedFile
        accept(filename, 'NDPI')

        # Find and delete macro image
        for directory in fh.directories:
            if directory.entries[NDPI_SOURCELENS].value()[0] == -1:
                directory.delete()
                break
        else:
            raise IOError("No label in NDPI file")


format_handlers = [
    do_aperio_svs,
    do_hamamatsu_ndpi,
]


def _main():
    global DEBUG

    parser = OptionParser(usage='%prog [options] file [file...]',
            description=PROG_DESCRIPTION, version=PROG_VERSION)
    parser.add_option('-d', '--debug', action='store_true',
            help='Enable debugging')
    opts, args = parser.parse_args()
    if not args:
        parser.error('specify a file')
    DEBUG = opts.debug

    exit_code = 0
    for filename in args:
        try:
            for handler in format_handlers:
                try:
                    handler(filename)
                    break
                except UnrecognizedFile:
                    pass
            else:
                raise IOError('Unrecognized file type')
        except Exception, e:
            if DEBUG:
                raise
            print >>sys.stderr, '%s: %s' % (filename, str(e))
            exit_code = 1
    sys.exit(exit_code)


if __name__ == '__main__':
    _main()
