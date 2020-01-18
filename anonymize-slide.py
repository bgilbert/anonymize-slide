#!/usr/bin/python
#
#  anonymize-slide.py - Delete the label from a whole-slide image.
#
#  Copyright (c) 2007-2013 Carnegie Mellon University
#  Copyright (c) 2011      Google, Inc.
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


from configparser import RawConfigParser
import io
from glob import glob
from optparse import OptionParser
import os
import string
import struct
import sys

PROG_DESCRIPTION = '''
Delete the slide label from an MRXS, NDPI, or SVS whole-slide image.
'''.strip()
PROG_VERSION = '1.1.1'
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

# Format headers
LZW_CLEARCODE = b'\x80'
JPEG_SOI = b'\xff\xd8'
UTF8_BOM = b'\xef\xbb\xbf'

# MRXS
MRXS_HIERARCHICAL = 'HIERARCHICAL'
MRXS_NONHIER_ROOT_OFFSET = 41


class UnrecognizedFile(Exception):
    pass


class TiffFile(object):
    def __init__(self, path):
        mode = 'r+b'
        self.file = io.open(path, mode)
        self.close_file = (self.file is not path)

        # Check header, decide endianness
        endian = self.file.read(2)
        endianII = bytes('II',  'utf-8')
        endianMM = bytes('MM',  'utf-8')

        if endian == endianII:
            self._fmt_prefix = '<'
        elif endian == endianMM:
            self._fmt_prefix = '>'
        else:
            raise UnrecognizedFile

        # Check TIFF version
        self._bigtiff = False
        self._ndpi = False
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
            in_pointer_offset = self.file.tell()
            directory_offset = self.read_fmt('D')
            if directory_offset == 0:
                break
            self.file.seek(directory_offset)
            directory = TiffDirectory(self, len(self.directories),
                    in_pointer_offset)
            if not self.directories and not self._bigtiff:
                # Check for NDPI.  Because we don't know we have an NDPI file
                # until after reading the first directory, we will choke if
                # the first directory is beyond 4 GB.
                if NDPI_MAGIC in directory.entries:
                    if DEBUG:
                        print('Enabling NDPI mode.')
                    self._ndpi = True
            self.directories.append(directory)
        if not self.directories:
            raise IOError('No directories')

    def __enter__(self):
        return self

    def __exit__(self, *args, **kwargs):
        if (not self.close_file):
            return  # do nothing
        # clean up
        exit = getattr(self.file, '__exit__', None)
        if exit is not None:
            return exit(*args, **kwargs)
        else:
            exit = getattr(self.file, 'close', None)
            if exit is not None:
                exit()

    def __getattr__(self, attr):
        return getattr(self.file, attr)

    def __iter__(self):
        return iter(self.file)

    def _convert_format(self, fmt):
        # Format strings can have special characters:
        # y: 16-bit   signed on little TIFF, 64-bit   signed on BigTIFF
        # Y: 16-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF
        # z: 32-bit   signed on little TIFF, 64-bit   signed on BigTIFF
        # Z: 32-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF
        # D: 32-bit unsigned on little TIFF, 64-bit unsigned on BigTIFF/NDPI
        if self._bigtiff:
            fmt = fmt.translate(str.maketrans('yYzZD', 'qQqQQ'))
        elif self._ndpi:
            fmt = fmt.translate(str.maketrans('yYzZD', 'hHiIQ'))
        else:
            fmt = fmt.translate(str.maketrans('yYzZD', 'hHiII'))
        return self._fmt_prefix + fmt

    def fmt_size(self, fmt):
        return struct.calcsize(self._convert_format(fmt))

    def near_pointer(self, base, offset):
        # If NDPI, return the value whose low-order 32-bits are equal to
        # @offset and which is within 4 GB of @base and below it.
        # Otherwise, return offset.
        if self._ndpi and offset < base:
            seg_size = 1 << 32
            offset += ((base - offset) // seg_size) * seg_size
        return offset

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

    def delete(self, expected_prefix=None):
        # Get strip offsets/lengths
        try:
            offsets = self.entries[STRIP_OFFSETS].value()
            lengths = self.entries[STRIP_BYTE_COUNTS].value()
        except KeyError:
            raise IOError('Directory is not stripped')

        # Wipe strips
        for offset, length in zip(offsets, lengths):
            offset = self._fh.near_pointer(self._out_pointer_offset, offset)
            if DEBUG:
                print('Zeroing', offset, 'for', length)
            self._fh.seek(offset)
            if expected_prefix:
                buf = self._fh.file.read(len(expected_prefix))
                if buf != expected_prefix:
                    raise IOError('Unexpected data in image strip')
                self._fh.file.seek(offset)
            write_byte = b'\0'
            self._fh.file.write(write_byte * length)

        # Remove directory
        if DEBUG:
            print('Deleting directory', self._number)
        self._fh.file.seek(self._out_pointer_offset)
        out_pointer = self._fh.read_fmt('D')
        self._fh.file.seek(self._in_pointer_offset)
        self._fh.write_fmt('D', out_pointer)


class TiffEntry(object):
    def __init__(self, fh):
        self.start = fh.file.tell()
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
            self._fh.file.seek(self.start + self._fh.fmt_size('HHZ'))
        else:
            # Out-of-line value
            self._fh.file.seek(self._fh.near_pointer(self.start, self.value_offset))
        items = self._fh.read_fmt(fmt, force_list=True)
        if self.type == ASCII:
            utf8_zero = bytes('\0', 'utf-8')
            if items[-1] != utf8_zero:
                raise ValueError('String not null-terminated')
            return b''.join(items[:-1])
        else:
            return items


class MrxsFile(object):
    def __init__(self, filename):
        # Split filename
        dirname, ext = os.path.splitext(filename)
        if ext != '.mrxs':
            raise UnrecognizedFile

        # Parse slidedat
        self._slidedatfile = os.path.join(dirname, 'Slidedat.ini')
        self._dat = RawConfigParser()
        self._dat.optionxform = str
        try:
            with open(self._slidedatfile, 'rb') as fh_prewrapped:
                fh = io.TextIOWrapper(fh_prewrapped)
                self._have_bom = (fh.read(len(UTF8_BOM)) == UTF8_BOM)
                if not self._have_bom:
                    fh.seek(0)
                self._dat.read_file(fh)
        except IOError:
            raise UnrecognizedFile

        # Get file paths
        self._indexfile = os.path.join(dirname,
                self._dat.get(MRXS_HIERARCHICAL, 'INDEXFILE'))
        self._datafiles = [os.path.join(dirname,
                self._dat.get('DATAFILE', 'FILE_%d' % i))
                for i in range(self._dat.getint('DATAFILE', 'FILE_COUNT'))]

        # Build levels
        self._make_levels()

    def _make_levels(self):
        self._levels = {}
        self._level_list = []
        layer_count = self._dat.getint(MRXS_HIERARCHICAL, 'NONHIER_COUNT')
        for layer_id in range(layer_count):
            level_count = self._dat.getint(MRXS_HIERARCHICAL,
                    'NONHIER_%d_COUNT' % layer_id)
            for level_id in range(level_count):
                level = MrxsNonHierLevel(self._dat, layer_id, level_id,
                        len(self._level_list))
                self._levels[(level.layer_name, level.name)] = level
                self._level_list.append(level)

    @classmethod
    def _read_int32(cls, f):
        buf = f.read(4)
        if len(buf) != 4:
            raise IOError('Short read')
        return struct.unpack('<i', buf)[0]

    @classmethod
    def _assert_int32(cls, f, value):
        v = cls._read_int32(f)
        if v != value:
            raise ValueError('%d != %d' % (v, value))

    def _get_data_location(self, record):
        with open(self._indexfile, 'rb') as fh:
            fh.seek(MRXS_NONHIER_ROOT_OFFSET)
            # seek to record
            table_base = self._read_int32(fh)
            fh.seek(table_base + record * 4)
            # seek to list head
            list_head = self._read_int32(fh)
            fh.seek(list_head)
            # seek to data page
            self._assert_int32(fh, 0)
            page = self._read_int32(fh)
            fh.seek(page)
            # check pagesize
            self._assert_int32(fh, 1)
            # read rest of prologue
            self._read_int32(fh)
            self._assert_int32(fh, 0)
            self._assert_int32(fh, 0)
            # read values
            position = self._read_int32(fh)
            size = self._read_int32(fh)
            fileno = self._read_int32(fh)
            return (self._datafiles[fileno], position, size)

    def _zero_record(self, record):
        path, offset, length = self._get_data_location(record)
        with open(path, 'r+b') as fh:
            fh.seek(0, 2)
            do_truncate = (fh.tell() == offset + length)
            if DEBUG:
                if do_truncate:
                    print('Truncating', path, 'to', offset)
                else:
                    print('Zeroing', path, 'at', offset, 'for', length)
            fh.seek(offset)
            buf = fh.read(len(JPEG_SOI))
            if buf != JPEG_SOI:
                raise IOError('Unexpected data in nonhier image')
            if do_truncate:
                fh.truncate(offset)
            else:
                fh.seek(offset)
                fh.write('\0' * length)

    def _delete_index_record(self, record):
        if DEBUG:
            print('Deleting record', record)
        with open(self._indexfile, 'r+b') as fh:
            entries_to_move = len(self._level_list) - record - 1
            if entries_to_move == 0:
                return
            # get base of table
            fh.seek(MRXS_NONHIER_ROOT_OFFSET)
            table_base = self._read_int32(fh)
            # read tail of table
            fh.seek(table_base + (record + 1) * 4)
            buf = fh.read(entries_to_move * 4)
            if len(buf) != entries_to_move * 4:
                raise IOError('Short read')
            # overwrite the target record
            fh.seek(table_base + record * 4)
            fh.write(buf)

    def _hier_keys_for_level(self, level):
        ret = []
        for k, _ in self._dat.items(MRXS_HIERARCHICAL):
            k_bytes = bytes(k, 'utf-8')
            level_key_prefix_bytes = bytes(level.key_prefix, 'utf-8')
            level_key_prefix_underscore_bytes = bytes(level.key_prefix + '_', 'utf-8')
            if k_bytes == level_key_prefix_bytes or k_bytes.startswith(level_key_prefix_underscore_bytes):
                ret.append(k)
        return ret

    def _rename_section(self, old, new):
        if self._dat.has_section(old):
            if DEBUG:
                print('[%s] -> [%s]' % (old, new))
            self._dat.add_section(new)
            for k, v in self._dat.items(old):
                self._dat.set(new, k, v)
            self._dat.remove_section(old)
        elif DEBUG:
            print('[%s] does not exist' % old)

    def _delete_section(self, section):
        if DEBUG:
            print('Deleting [%s]' % section)
        self._dat.remove_section(section)

    def _set_key(self, section, key, value):
        if DEBUG:
            prev = self._dat.get(section, key)
            print('[%s] %s: %s -> %s' % (section, key, prev, value))
        self._dat.set(section, key, value)

    def _rename_key(self, section, old, new):
        if DEBUG:
            print('[%s] %s -> %s' % (section, old, new))
        v = self._dat.get(section, old)
        self._dat.remove_option(section, old)
        self._dat.set(section, new, v)

    def _delete_key(self, section, key):
        if DEBUG:
            print('Deleting [%s] %s' % (section, key))
        self._dat.remove_option(section, key)

    def _write(self):
        buf = io.StringIO()
        self._dat.write(buf)
        with open(self._slidedatfile, 'wb') as fh_prewrapped:
            fh = io.TextIOWrapper(fh_prewrapped)
            if self._have_bom:
                fh.write(UTF8_BOM)
            buf_replaced = buf.getvalue().replace('\n', '\r\n')
            fh.write(buf_replaced)
        fh.close()

    def delete_level(self, layer_name, level_name):
        level = self._levels[(layer_name, level_name)]
        record = level.record

        # Zero image data
        self._zero_record(record)

        # Delete pointer from nonhier table in index
        self._delete_index_record(record)

        # Remove slidedat keys
        for k in self._hier_keys_for_level(level):
            self._delete_key(MRXS_HIERARCHICAL, k)

        # Remove slidedat section
        self._delete_section(level.section)

        # Rename section and keys for subsequent levels in the layer
        prev_level = level
        for cur_level in self._level_list[record + 1:]:
            if cur_level.layer_id != prev_level.layer_id:
                break
            for k in self._hier_keys_for_level(cur_level):
                new_k = k.replace(cur_level.key_prefix,
                        prev_level.key_prefix, 1)
                self._rename_key(MRXS_HIERARCHICAL, k, new_k)
            self._set_key(MRXS_HIERARCHICAL, prev_level.section_key,
                    prev_level.section)
            self._rename_section(cur_level.section, prev_level.section)
            prev_level = cur_level

        # Update level count within layer
        count_k = 'NONHIER_%d_COUNT' % level.layer_id
        count_v = self._dat.getint(MRXS_HIERARCHICAL, count_k)
        self._set_key(MRXS_HIERARCHICAL, count_k, count_v - 1)

        # Write slidedat
        self._write()

        # Refresh metadata
        self._make_levels()


class MrxsNonHierLevel(object):
    def __init__(self, dat, layer_id, level_id, record):
        self.layer_id = layer_id
        self.id = level_id
        self.record = record
        self.layer_name = dat.get(MRXS_HIERARCHICAL,
                'NONHIER_%d_NAME' % layer_id)
        self.key_prefix = 'NONHIER_%d_VAL_%d' % (layer_id, level_id)
        self.name = dat.get(MRXS_HIERARCHICAL, self.key_prefix)
        self.section_key = self.key_prefix + '_SECTION'
        self.section = dat.get(MRXS_HIERARCHICAL, self.section_key)


def accept(filename, format):
    if DEBUG:
        print(filename + ':', format)


def do_aperio_svs(filename):
    with TiffFile(filename) as fh:
        # Check for SVS file
        try:
            desc0 = fh.directories[0].entries[IMAGE_DESCRIPTION].value()
            aperio_bytes = bytes('Aperio', 'utf-8')
            if not desc0.startswith(aperio_bytes):
                raise UnrecognizedFile
        except KeyError:
            raise UnrecognizedFile
        accept(filename, 'SVS')

        # Find and delete label
        for directory in fh.directories:
            lines = directory.entries[IMAGE_DESCRIPTION].value().splitlines()
            label_bytes = b'label '
            if len(lines) >= 2:
                line1_bytes = bytes(lines[1], 'utf-8')
                if line1_bytes.startswith(label_bytes):
                    directory.delete(expected_prefix=LZW_CLEARCODE)
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
                directory.delete(expected_prefix=JPEG_SOI)
                break
        else:
            raise IOError("No label in NDPI file")


def do_3dhistech_mrxs(filename):
    mrxs = MrxsFile(filename)
    try:
        mrxs.delete_level('Scan data layer', 'ScanDataLayer_SlideBarcode')
    except KeyError:
        raise IOError('No label in MRXS file')


format_handlers = [
    do_aperio_svs,
    do_hamamatsu_ndpi,
    do_3dhistech_mrxs,
]


def _main():
    global DEBUG

    parser = OptionParser(usage='%prog [options] file [file...]',
            description=PROG_DESCRIPTION, version=PROG_VERSION)
    parser.add_option('-d', '--debug', action='store_true',
            help='show debugging information')
    opts, args = parser.parse_args()
    if not args:
        parser.error('specify a file')
    DEBUG = opts.debug

    if sys.platform == 'win32':
        # The shell expects us to do wildcard expansion
        filenames = []
        for arg in args:
            filenames.extend(glob(arg) or [arg])
    else:
        filenames = args

    exit_code = 0
    for filename in filenames:
        try:
            for handler in format_handlers:
                try:
                    handler(filename)
                    break
                except UnrecognizedFile:
                    pass
            else:
                raise IOError('Unrecognized file type')
        except Exception as e:
            if DEBUG:
                raise
            sys.stderr.write('%s: %s %s' % (filename, str(e), os.linesep))
            exit_code = 1
    sys.exit(exit_code)


if __name__ == '__main__':
    _main()
