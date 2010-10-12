# Disk.py -- Pythonized Lustre Disk
# Copyright (C) 2009 CEA
#
# This file is part of shine
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA  02111-1307, USA.
#
# $Id$


"""
Lustre Disk abstraction module.

"""

import copy
import os
import stat
import struct
import tempfile
import subprocess

### From lustre/include/lustre_disk.h:

# on-disk files
MDT_LOGS_DIR = "LOGS"
MOUNT_CONFIGS_DIR = "CONFIGS"
MOUNT_DATA_FILE = "%s/mountdata" % MOUNT_CONFIGS_DIR
LAST_RCVD = "last_rcvd"
LOV_OBJID = "lov_objid"
HEALTH_CHECK = "health_check"

# persistent mount data
LDD_F_SV_TYPE_MDT = 0x0001  # MDT
LDD_F_SV_TYPE_OST = 0x0002  # OST
LDD_F_SV_TYPE_MGS = 0x0004  # MGS
LDD_F_NEED_INDEX = 0x0010   # need an index assignment
LDD_F_VIRGIN = 0x0020       # never registered
LDD_F_UPDATE = 0x0040       # update all related config logs
LDD_F_REWRITE_LDD = 0x0080  # rewrite the LDD
LDD_F_WRITECONF = 0x0100    # regenerate all logs for this fs
LDD_F_UPGRADE14 = 0x0200    # COMPAT_14
LDD_F_PARAM = 0x0400        # process as lctl conf_param

# enum ldd_mount_type
LDD_MT_EXT3 = 0
LDD_MT_LDISKFS = 1
LDD_MT_SMFS = 2
LDD_MT_REISERFS = 3
LDD_MT_LDISKFS2 = 4
LDD_MT_LAST = 5

LDD_INCOMPAT_SUPP = 0
LDD_ROCOMPAT_SUPP = 0

LDD_MAGIC = 0x1dd00001

# From lustre-1.6.7/lustre/include/lustre_disk.h:
# 
# /* On-disk configuration file. In host-endian order. */
# struct lustre_disk_data {
#      __u32      ldd_magic;
#      __u32      ldd_feature_compat;  /* compatible feature flags */
#      __u32      ldd_feature_rocompat;/* read-only compatible feature flags */
#      __u32      ldd_feature_incompat;/* incompatible feature flags */
# 
#      __u32      ldd_config_ver;      /* config rewrite count - not used */
#      __u32      ldd_flags;           /* LDD_SV_TYPE */
#      __u32      ldd_svindex;         /* server index (0001), must match 
#                                         svname */
#      __u32      ldd_mount_type;      /* target fs type LDD_MT_* */
#      char       ldd_fsname[64];      /* filesystem this server is part of */
#      char       ldd_svname[64];      /* this server's name (lustre-mdt0001)*/
#      __u8       ldd_uuid[40];        /* server UUID (COMPAT_146) */
# 
# /*200*/ char    ldd_userdata[1024 - 200]; /* arbitrary user string */
# /*1024*/__u8    ldd_padding[4096 - 1024];
# /*4096*/char    ldd_mount_opts[4096]; /* target fs mount opts */
# /*8192*/char    ldd_params[4096];     /* key=value pairs */
# };


class DiskDeviceError(Exception):
    """
    Associated device error.
    """
    def __init__(self, disk, message=None):
        Exception.__init__(self, message)
        self._disk = disk


class Disk:
    """
    Represents a low-level Lustre Disk as defined in lustre/include/
    lustre_disk.h. Base class for Lustre Target (see Target.py).
    """

    def __init__(self, dev, jdev=None):
        self.dev = dev
        self.jdev = jdev

        # filled by _device_check
        self.dev_isblk = False
        self.dev_size = 0

        # filled by _mountdata_check (use provided accessors if needed)
        self.ldd_fsname = None
        self.ldd_svname = None
        self._ldd_flags = 0

    def update(self, other):
        """
        Update my serializable fields from other/distant object.
        """
        self.dev_isblk = other.dev_isblk
        self.dev_size = other.dev_size
        self.ldd_fsname = copy.copy(other.ldd_fsname)
        self.ldd_svname = copy.copy(other.ldd_svname)
        self._ldd_flags = other._ldd_flags

    def _disk_check(self, fsname_check=None, label_check=None):
        """
        Check that described lustre disk device is sane.

        This includes checking device path, type, size, ... and Lustre
        mountdata file.
        """
        self._device_check()
        self._mountdata_check(fsname_check, label_check)

    def _device_check(self):
        """
        Device sanity checking based on the stat() syscall.
        """
        try:
            info = os.stat(self.dev)
        except OSError, error:
            raise DiskDeviceError(self, str(error))

        mode = info[stat.ST_MODE]

        if stat.S_ISBLK(mode):
            # block device
            self.dev_isblk = True
            # get dev size
            partitions = open("/proc/partitions", 'r')
            try:
                dev = os.path.basename(self.dev)
                for line in partitions:
                    d_info = line.rstrip('\n').split(' ')
                    if len(d_info) > 1 and d_info[-1] == dev:
                        self.dev_size = int(d_info[-2]) * 1024
                        break
            finally:
                partitions.close()

        elif stat.S_ISREG(mode):
            # regular file
            self.dev_isblk = False
            self.dev_size = int(info[stat.ST_SIZE])
        else:
            # unsupported
            raise DiskDeviceError(self, "unsupported device type")

    def _mountdata_check(self, fsname_check=None, label_check=None):
        """
        Read on-disk CONFIGS/mountdata and optionally check its content against
        provided fsname and service label.
        """

        # Run debugfs to read mountdata file without having to mount the
        # ldiskfs filesystem.
        tmp_mountdata = tempfile.NamedTemporaryFile(prefix='shine-debugfs-', 
                                                    suffix='-mountdata')
        retcode = subprocess.call("export PATH=/usr/lib/lustre/:${PATH};" + \
                        "debugfs -c -R 'dump /%s %s' '%s'" % \
                        (MOUNT_DATA_FILE, tmp_mountdata.name, self.dev),
                        stderr=subprocess.PIPE, shell=True)

        # Note: checking debugfs retcode is not reliable as debugfs seems to
        # always return 0.
        if retcode > 0:
            raise DiskDeviceError(self, "Failed to run 'debugfs' to read " + \
                                  "mountdata (rc=%d)" % retcode)

        try:
            # Read struct lustre_disk_data, which is:
            # 12288 bytes == sizeof(struct lustre_disk_data) as of 1.6.7 or
            # 1.8.0
            data_read = tmp_mountdata.read(12288)
            if len(data_read) == 0:
                raise DiskDeviceError(self, "Failed to read mountdata for %s" \
                                      % self.dev)

            fmt = 'IIIIIIII64s64s40s824s3072s4096s4096s'
            required_length = struct.calcsize(fmt)
            if len(data_read) < required_length:
                raise DiskDeviceError(self, \
                        "Unexpected EOF while reading %s" % MOUNT_DATA_FILE)
            
            # Unpack first fields of struct lustre_disk_data (in native byte
            # order).
            (ldd_magic, ldd_feat_compat, ldd_feat_rocompat, ldd_feat_incompat,
                    ldd_config_ver, ldd_flags, ldd_svindex, ldd_mount_type,
                    ldd_fsname, ldd_svname, ldd_uuid, ldd_userdata,
                    ldd_padding, ldd_mount_opts, ldd_params) = \
                    struct.unpack(fmt, data_read)

            # Light sanity check.
            if ldd_magic != LDD_MAGIC:
                raise DiskDeviceError(self, "Bad magic in %s: %x!=%x" % \
                        (MOUNT_DATA_FILE, ldd_magic, LDD_MAGIC))

            # Could add supported features check here.

            # Check configured lustre service and fsname on this disk
            self.ldd_fsname = ldd_fsname[0:ldd_fsname.find('\0')]
            self.ldd_svname = ldd_svname[0:ldd_svname.find('\0')]
            if fsname_check and self.ldd_fsname != fsname_check:
                raise DiskDeviceError(self, \
                        "Found service %s for fs '%s'!='%s' on %s" % \
                        (self.ldd_svname, self.ldd_fsname, \
                         fsname_check, self.dev))
            if label_check and self.ldd_svname != label_check:
                raise DiskDeviceError(self, \
                         "Found service %s!=%s for fs '%s' on %s" % \
                        (self.ldd_svname, label_check, \
                         self.ldd_fsname, self.dev))

            self._ldd_flags = ldd_flags

            # If needed, LDD_PARAMS could be get with:
            # ldd_params = ldd_params[0:ldd_params.find('\0')].strip()
        finally:
            tmp_mountdata.close()

    def has_need_index_flag(self):
        """LDD flag: need an index assignment"""
        return self._ldd_flags & LDD_F_NEED_INDEX

    def has_first_time_flag(self):
        """LDD flag: never registered"""
        return self._ldd_flags & LDD_F_VIRGIN

    def has_update_flag(self):
        """LDD flag: update all related config logs"""
        return self._ldd_flags & LDD_F_UPDATE

    def has_rewrite_ldd_flag(self):
        """LDD flag: rewrite the LDD"""
        return self._ldd_flags & LDD_F_REWRITE_LDD

    def has_writeconf_flag(self):
        """LDD flag: regenerate all logs for this fs"""
        return self._ldd_flags & LDD_F_WRITECONF

    def has_upgrade14_flag(self):
        """LDD flag: COMPAT 14"""
        return self._ldd_flags & LDD_F_UPGRADE14

    def has_param_flag(self):
        """LDD flag: process as lctl conf_param"""
        return self._ldd_flags & LDD_F_PARAM

