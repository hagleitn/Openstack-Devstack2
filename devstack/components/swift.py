# vim: tabstop=4 shiftwidth=4 softtabstop=4

#    Copyright (C) 2012 Yahoo! Inc. All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import re

from devstack import component as comp
from devstack import log as logging
from devstack import shell as sh
from devstack import utils

LOG = logging.getLogger("devstack.components.swift")

# Swift has alot of config files!
SWIFT_CONF = 'swift.conf'
PROXY_SERVER_CONF = 'proxy-server.conf'
ACCOUNT_SERVER_CONF = 'account-server.conf'
CONTAINER_SERVER_CONF = 'container-server.conf'
OBJECT_SERVER_CONF = 'object-server.conf'
RSYNC_CONF = 'rsyncd.conf'
SYSLOG_CONF = 'rsyslog.conf'
SWIFT_MAKERINGS = 'swift-remakerings'
SWIFT_STARTMAIN = 'swift-startmain'
SWIFT_INIT = 'swift-init'
SWIFT_IMG = 'drives/images/swift.img'
DEVICE_PATH = 'drives/sdb1'
CONFIGS = [SWIFT_CONF, PROXY_SERVER_CONF, ACCOUNT_SERVER_CONF,
           CONTAINER_SERVER_CONF, OBJECT_SERVER_CONF, RSYNC_CONF,
           SYSLOG_CONF, SWIFT_MAKERINGS, SWIFT_STARTMAIN]
SWIFT_RSYNC_LOC = '/etc/rsyslog.d/10-swift.conf'
DEF_LOOP_SIZE = 1000000

# Adjustments to rsync/rsyslog
RSYNC_CONF_LOC = '/etc/default/rsync'
RSYNCD_CONF_LOC = '/etc/rsyncd.conf'
RSYNC_SERVICE_RESTART = ['service', 'rsync', 'restart']
RSYSLOG_SERVICE_RESTART = ['service', 'rsyslog', 'restart']
RSYNC_ON_OFF_RE = re.compile(r'^\s*RSYNC_ENABLE\s*=\s*(.*)$', re.I)

# Defines our auth service type
AUTH_SERVICE = 'keystone'

# Defines what type of loopback filesystem we will make
# xfs is preferred due to its extended attributes
FS_TYPE = "xfs"

# Subdirs of the git checkout
BIN_DIR = 'bin'
CONFIG_DIR = 'etc'
LOG_DIR = 'logs'

# Config keys we warm up so u won't be prompted later
WARMUP_PWS = [('service_token', 'the service admin token'),
              ('swift_hash', 'the random unique string for your swift cluster')]


class SwiftUninstaller(comp.PythonUninstallComponent):
    def __init__(self, *args, **kargs):
        comp.PythonUninstallComponent.__init__(self, *args, **kargs)
        self.datadir = sh.joinpths(self.app_dir, self.cfg.getdefaulted('swift', 'data_location', 'data'))
        self.cfg_dir = sh.joinpths(self.app_dir, CONFIG_DIR)
        self.bin_dir = sh.joinpths(self.app_dir, BIN_DIR)
        self.logdir = sh.joinpths(self.datadir, LOG_DIR)

    def pre_uninstall(self):
        sh.umount(sh.joinpths(self.datadir, DEVICE_PATH))
        sh.replace_in(RSYNC_CONF_LOC, RSYNC_ON_OFF_RE, 'RSYNC_ENABLE=false', True)

    def post_uninstall(self):
        sh.execute(*RSYSLOG_SERVICE_RESTART, run_as_root=True)
        sh.execute(*RSYNC_SERVICE_RESTART, run_as_root=True)


class SwiftInstaller(comp.PythonInstallComponent):
    def __init__(self, *args, **kargs):
        comp.PythonInstallComponent.__init__(self, *args, **kargs)
        self.cfg_dir = sh.joinpths(self.app_dir, CONFIG_DIR)
        self.bin_dir = sh.joinpths(self.app_dir, BIN_DIR)
        self.datadir = sh.joinpths(self.app_dir, self.cfg.getdefaulted('swift', 'data_location', 'data'))
        self.logdir = sh.joinpths(self.datadir, LOG_DIR)
        self.startmain_file = sh.joinpths(self.bin_dir, SWIFT_STARTMAIN)
        self.makerings_file = sh.joinpths(self.bin_dir, SWIFT_MAKERINGS)
        self.fs_dev = sh.joinpths(self.datadir, DEVICE_PATH)
        self.fs_image = sh.joinpths(self.datadir, SWIFT_IMG)
        self.auth_server = AUTH_SERVICE

    def _get_download_locations(self):
        places = list()
        places.append({
                'uri': ('git', 'swift_repo'),
                'branch': ('git', 'swift_branch')
            })
        return places

    def _get_config_files(self):
        return list(CONFIGS)

    def warm_configs(self):
        for (pw_key, prompt) in WARMUP_PWS:
            self.pw_gen.get_password(pw_key, prompt)

    def _get_param_map(self, config_fn):
        return {
            'USER': self.cfg.getdefaulted('swift', 'swift_user', sh.getuser()),
            'GROUP': self.cfg.getdefaulted('swift', 'swift_group', sh.getgroupname()),
            'SWIFT_DATA_LOCATION': self.datadir,
            'SWIFT_CONFIG_LOCATION': self.cfg_dir,
            'SERVICE_TOKEN': self.cfg.get('passwords', 'service_token'),
            'AUTH_SERVER': self.auth_server,
            'SWIFT_HASH': self.cfg.get('passwords', 'swift_hash'),
            'SWIFT_LOGDIR': self.logdir,
            'SWIFT_PARTITION_POWER_SIZE': self.cfg.getdefaulted('swift', 'partition_power_size', '9'),
            # Note: leave these alone, will be adjusted later
            'NODE_PATH': '%NODE_PATH%',
            'BIND_PORT': '%BIND_PORT%',
            'LOG_FACILITY': '%LOG_FACILITY%',
        }

    def _create_data_location(self):
        loop_size = self.cfg.get('swift', 'loopback_disk_size')
        if not loop_size:
            loop_size = DEF_LOOP_SIZE
        else:
            loop_size = utils.to_bytes(loop_size)
        sh.create_loopback_file(fname=self.fs_image,
                                size=loop_size,
                                fs_type=FS_TYPE)
        self.tracewriter.file_touched(self.fs_image)
        sh.mount_loopback_file(self.fs_image, self.fs_dev, FS_TYPE)
        sh.chown_r(self.fs_dev, sh.geteuid(), sh.getegid())

    def _create_node_config(self, node_number, port):
        for t in ['object', 'container', 'account']:
            src_fn = sh.joinpths(self.cfg_dir, '%s-server.conf' % t)
            tgt_fn = sh.joinpths(self.cfg_dir, '%s-server/%d.conf' % (t, node_number))
            adjustments = {
                           '%NODE_PATH%': sh.joinpths(self.datadir, str(node_number)),
                           '%BIND_PORT%': str(port),
                           '%LOG_FACILITY%': str(2 + node_number),
                          }
            sh.copy_replace_file(src_fn, tgt_fn, adjustments)
            port += 1

    def _delete_templates(self):
        for t in ['object', 'container', 'account']:
            sh.unlink(sh.joinpths(self.cfg_dir, '%s-server.conf' % t))

    def _create_nodes(self):
        for i in range(1, 5):
            self.tracewriter.dirs_made(sh.mkdirslist(sh.joinpths(self.fs_dev, '%d/node' % i)))
            link_tgt = sh.joinpths(self.datadir, str(i))
            sh.symlink(sh.joinpths(self.fs_dev, str(i)), link_tgt)
            self.tracewriter.symlink_made(link_tgt)
            start_port = (6010 + (i - 1) * 5)
            self._create_node_config(i, start_port)
        self._delete_templates()

    def _turn_on_rsync(self):
        sh.symlink(sh.joinpths(self.cfg_dir, RSYNC_CONF), RSYNCD_CONF_LOC)
        self.tracewriter.symlink_made(RSYNCD_CONF_LOC)
        sh.replace_in(RSYNC_CONF_LOC, RSYNC_ON_OFF_RE, 'RSYNC_ENABLE=true', True)

    def _create_log_dirs(self):
        self.tracewriter.dirs_made(*sh.mkdirslist(sh.joinpths(self.logdir, 'hourly')))
        sh.symlink(sh.joinpths(self.cfg_dir, SYSLOG_CONF), SWIFT_RSYNC_LOC)
        self.tracewriter.symlink_made(SWIFT_RSYNC_LOC)

    def _setup_binaries(self):
        sh.move(sh.joinpths(self.cfg_dir, SWIFT_MAKERINGS), self.makerings_file)
        sh.chmod(self.makerings_file, 0777)
        self.tracewriter.file_touched(self.makerings_file)
        sh.move(sh.joinpths(self.cfg_dir, SWIFT_STARTMAIN), self.startmain_file)
        sh.chmod(self.startmain_file, 0777)
        self.tracewriter.file_touched(self.startmain_file)

    def _make_rings(self):
        sh.execute(self.makerings_file, run_as_root=True)

    def post_install(self):
        self._create_data_location()
        self._create_nodes()
        self._turn_on_rsync()
        self._create_log_dirs()
        self._setup_binaries()
        self._make_rings()


class SwiftRuntime(comp.PythonRuntime):
    def __init__(self, *args, **kargs):
        comp.PythonRuntime.__init__(self, *args, **kargs)
        self.datadir = sh.joinpths(self.app_dir, self.cfg.getdefaulted('swift', 'data_location', 'data'))
        self.cfg_dir = sh.joinpths(self.app_dir, CONFIG_DIR)
        self.bin_dir = sh.joinpths(self.app_dir, BIN_DIR)
        self.logdir = sh.joinpths(self.datadir, LOG_DIR)

    def start(self):
        sh.execute(*RSYSLOG_SERVICE_RESTART, run_as_root=True)
        sh.execute(*RSYNC_SERVICE_RESTART, run_as_root=True)
        swift_start_cmd = [sh.joinpths(self.bin_dir, SWIFT_INIT)] + ['all', 'start']
        sh.execute(*swift_start_cmd, run_as_root=True)

    def stop(self):
        swift_stop_cmd = [sh.joinpths(self.bin_dir, SWIFT_INIT)] + ['all', 'stop']
        sh.execute(*swift_stop_cmd, run_as_root=True)

    def restart(self):
        swift_restart_cmd = [sh.joinpths(self.bin_dir, SWIFT_INIT)] + ['all', 'restart']
        sh.execute(*swift_restart_cmd, run_as_root=True)
