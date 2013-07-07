# FileSystem.py -- Lustre FS
# Copyright (C) 2007-2013 CEA
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

"""
Lustre FileSystem class.

Represents a Lustre FS.
"""

import os
import sys
import socket
import logging
import logging.handlers

from ClusterShell.NodeSet import NodeSet
from ClusterShell.Task import task_self

from Shine.Configuration.Globals import Globals

from Shine.Lustre.Actions.Action import ActionGroup
from Shine.Lustre.Actions.Proxy import FSProxyAction
from Shine.Lustre.Actions.Install import Install

from Shine.Lustre.Component import ComponentGroup
from Shine.Lustre.Server import Server
from Shine.Lustre.Client import Client
from Shine.Lustre.Router import Router
from Shine.Lustre.Target import MGT, MDT, OST, Journal
# FileSystem class needs to re-export all Target status, they are used in
# Shine.Commands.*
from Shine.Lustre.Component import INPROGRESS, EXTERNAL, MOUNTED, \
                                   RECOVERING, OFFLINE, RUNTIME_ERROR, \
                                   CLIENT_ERROR, TARGET_ERROR


class FSError(Exception):
    """
    Base FileSystem error exception.
    """

class FSBadTargetError(FSError):
    """
    Raise when a attempt to create an unknown target is detected.
    """
    def __init__(self, target_name):
        msg = "Syntax error: unrecognized target \"%s\"" % target_name
        FSError.__init__(msg)

class FSStructureError(FSError):
    """
    Lustre file system structure error, raised after an invalid configuration
    is encountered. For example, you will get this error if you try to assign
    two targets `MGT' to a filesystem.
    """

class FSRemoteError(FSError):
    """
    Remote host(s) not available, or a remote operation failed.
    """
    def __init__(self, nodes, rc, msg):
        FSError.__init__(self)
        self.msg = msg
        self.nodes = nodes
        self.rc = int(rc)

    def __str__(self):
        return "%s: %s [rc=%d]" % (self.nodes, self.msg, self.rc)


class FileSystem:
    """
    The Lustre FileSystem abstract class.
    """

    def __init__(self, fs_name, event_handler=None):
        self.fs_name = fs_name
        self.event_handler = event_handler
        self.proxy_errors = []

        # All FS components (MGT, MDT, OST, Clients, ...)
        self.components = ComponentGroup()

        # file system MGT
        self.mgt = None

        self.debug = False
        self.logger = self._setup_logging()

    def set_debug(self, debug):
        self.debug = debug

    def _setup_logging(self):
        """Setup logging configuration for the whole filesystem."""
        # XXX: This is only here for fsck, currently.

        logger = logging.getLogger('Shine.Lustre')

        # Level
        if self.debug:
            logger.setLevel(logging.DEBUG)
        else:
            logger.setLevel(logging.INFO)

        # Formatter
        formatter = logging.Formatter(datefmt="%Y-%m-%d %X",
                      fmt='%(name)s %(levelname)s  %(message)s')

        try:
            # Handler
            handler = logging.handlers.SysLogHandler(address='/dev/log')
            logger.addHandler(handler)
            handler.setFormatter(formatter)
        except socket.error:
            logging.raiseExceptions = False
            msg = "Error connecting to syslog, disabling logging."
            print >> sys.stderr, "WARNING: %s" % msg

        return logger


    def get_mgs_nids(self):
        return self.mgt.get_nids()
    
    #
    # file system event handling
    #

    def _invoke(self, compname, action, status, **kwargs):
        """
        Inform the filesystem the provided event happened.
        If an event handler is set, the associated callback will be called.
        """
        # New style event handling: One global handler
        if self.event_handler:
            self.event_handler.event_callback(compname, action, status,
                                              **kwargs)
 
    def local_event(self, compname, action, status, **params):
        # Currently, all event callbacks need a node.
        # When localy called, add the current node
        node = Server.hostname_short()

        self._invoke(compname, action, status, node=node, **params)

    def distant_event(self, compname, action, status, node, **params):
        
        # Update the local component instance with the provided instance
        # if one is available in params.
        if 'comp' in params:
            comp = params['comp']
            comp.fs = self
            try:
                # Special hack for Journal object as they are not put in
                # components list.
                if comp.TYPE == Journal.TYPE:
                    comp.target.fs = self
                    target = self.components[comp.target.uniqueid()]
                    target.journal.update(comp)
                    other = target.journal
                else:
                    other = self.components[comp.uniqueid()]
                    # update target from remote one
                    other.update(comp)

                # substitute target parameter by local one
                params['comp'] = other
            except KeyError, error:
                print >> sys.stderr, "ERROR: Component update " \
                                     "failed (%s)" % str(error)

        self._invoke(compname, action, status, node=node, **params)

    def _handle_shine_proxy_error(self, nodes, message):
        self.proxy_errors.append((NodeSet(nodes), message))

    #
    # file system construction
    #

    def _attach_component(self, comp):
        self.components.add(comp)
        if comp.TYPE == MGT.TYPE:
            self.mgt = comp

    def new_target(self, server, type, index, dev, jdev=None, group=None,
                   tag=None, enabled=True, mode='managed', network=None):
        """
        Create a new attached target.
        """
        if type not in [ 'mgt', 'mdt', 'ost' ]:
            raise FSBadTargetError(type)

        if type == 'mgt' and self.mgt and len(self.mgt.get_nids()) > 0:
            raise FSStructureError("A Lustre FS has only one MGT.")

        # Instantiate matching target class (eg. 'ost' -> OST).
        module_name = sys.modules[self.__class__.__module__]
        target = getattr(module_name, type.upper())(fs=self, server=server,
                index=index, dev=dev, jdev=jdev, group=group, tag=tag,
                enabled=enabled, mode=mode, network=network)
        
        self._attach_component(target)

        return target

    def new_client(self, server, mount_path, mount_options=None, enabled=True):
        """
        Create a new attached client.
        """
        client = Client(self, server, mount_path, mount_options, enabled)
        self._attach_component(client)
        return client

    def new_router(self, server, enabled=True):
        """
        Create a new attached router.
        """
        router = Router(self, server, enabled)
        self._attach_component(router)
        return router

    #
    # Task management.
    #

    def _proxy_action(self, action, servers, comps=None, addopts=None,
                      **kwargs):
        """Create a proxy action to remotely run a shine action."""
        assert(isinstance(servers, NodeSet))
        assert(comps is None or isinstance(comps, ComponentGroup))

        failover = kwargs.get('failover')
        mountdata = kwargs.get('mountdata')
        return FSProxyAction(self, action, servers, self.debug, comps, addopts,
                             failover, mountdata)

    def _run_actions(self):
        """
        Start actions run-loop.

        It clears all previous proxy errors and starts task run-loop. This
        launches all FSProxyAction prepared before by example.
        """
        self.proxy_errors = []
        # XXX: Warning, also update _distant_action_by_server()
        task_self().set_default("stderr_msgtree", False)
        task_self().set_info('connect_timeout', 
                             Globals().get_ssh_connect_timeout())
        task_self().resume()

    def _check_errors(self, expected_states, components=None):
        """
        This verifies that executed tasks were successfull.

        It verifies all provided components (Target, Clients, ...) have
        expected state. If not, it returns the most incoherent state.

        If there is no error, it returns the expected state.
        """
        assert type(expected_states) is list

        # If a component list is provided, check that all components from it
        # have expected state.
        result = None
        for comp in components or []:

            # This should never happen but it is convenient for debugging if
            # there is some uncatched bug somewhere.
            # (ie: cannot unpickle due to ClusterShell version mismatch)
            if comp.state is None:
                msg = "WARNING: no state report from node %s" % comp.server
                print >> sys.stderr, msg
                comp.state = RUNTIME_ERROR

            if comp.state not in expected_states:
                result = max(result, comp.state)

        return result or expected_states[0]

    def _distant_action_by_server(self, action_class, servers, **kwargs):

        # filter local server
        distant_servers = Server.distant_servers(servers)

        # perform action on distant servers
        if len(distant_servers) > 0:
            action_class(nodes=distant_servers, fs=self, **kwargs).launch()
            # XXX: merge with _run_actions()
            task_self().set_default("stderr_msgtree", False)
            task_self().set_info('connect_timeout', 
                                 Globals().get_ssh_connect_timeout())
            task_self().resume()

            err_nodes = NodeSet()
            err_code = 0
            err_txt = ""

            if task_self().num_timeout():
                # Add timeout nodes
                timeout_ns = NodeSet.fromlist(task_self().iter_keys_timeout())
                err_nodes.update(timeout_ns)
                err_code = -1
                err_txt = "Node timed out"

            if task_self().max_retcode():

                # Ignore nodes which returned 0
                for rc, nodelist in task_self().iter_retcodes():
                    if rc > 0:
                        err_nodes.update(NodeSet.fromlist(nodelist))
                err_code = task_self().max_retcode()
                err_txt = task_self().node_buffer(err_nodes[0])

            if len(err_nodes) > 0:
                raise FSRemoteError(err_nodes, err_code, err_txt)

    def install(self, fs_config_file):
        """
        Install filesystem configuration file on its servers. 
        Server list is built from enabled targets and enabled clients only.
        """

        # Get all possible servers 
        servers = self.components.managed().allservers()

        self._distant_action_by_server(Install, servers,
                                       config_file=fs_config_file)
        
    def remove(self, servers=None):
        """
        Remove FS config files.
        """
        result = 0

        if servers is None:
            # Get all possible servers 
            servers = self.components.managed().allservers()

        # filter local server
        distant_servers = Server.distant_servers(servers)

        # If size is different, we have a local server in the list
        if len(distant_servers) < len(servers):
            # remove local fs configuration file
            fs_file = os.path.join(Globals().get_conf_dir(), 
                                   "%s.xmf" % self.fs_name)
            if os.path.exists(fs_file):
                result = os.remove(fs_file)

        if len(distant_servers) > 0:
            # Perform the remove operations on all targets for these nodes.
            self._proxy_action('remove', distant_servers).launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        if self.proxy_errors:
            return RUNTIME_ERROR
        
        return result

    def format(self, comps=None, **kwargs):

        comps = (comps or self.components).managed(supports='format')
        for server, targets in comps.groupbyserver():

            if server.is_local():
                # local server
                for target in targets:
                    target.format(**kwargs).launch()

            else:
                self._proxy_action('format', server.hostname, targets,
                                   **kwargs).launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        # Check for errors and return OFFLINE or error code
        return self._check_errors([OFFLINE], comps)


    def tunefs(self, comps=None, **kwargs):

        comps = (comps or self.components).managed(supports='tunefs')
        for server, targets in comps.groupbyserver():

            if server.is_local():
                # local server
                for target in targets:
                    target.tunefs(**kwargs).launch()

            else:
                self._proxy_action('tunefs', server.hostname, targets,
                                   **kwargs).launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        # Check for errors and return OFFLINE or error code
        return self._check_errors([OFFLINE], comps)

    def fsck(self, comps=None, **kwargs):

        comps = (comps or self.components).managed(supports='fsck')
        for server, targets in comps.groupbyserver():

            if server.is_local():
                # local server
                for target in targets:
                    target.fsck(**kwargs).launch()

            else:
                self._proxy_action('fsck', server.hostname, targets,
                                   **kwargs).launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        # Check for errors and return OFFLINE or error code
        return self._check_errors([OFFLINE], comps)


    def status(self, comps=None, **kwargs):
        """
        Get status of filesystem.
        """
        comps = (comps or self.components).managed('status')

        for server, srvcomps in comps.groupbyserver():

            if server.is_local():
                for comp in srvcomps:
                    comp.status().launch()
            else:
                self._proxy_action('status', server.hostname, srvcomps,
                                   **kwargs).launch()

        # Run local and proxy actions
        self._run_actions()
        
        # Here we check MOUNTED but in fact, any status is OK.
        return self._check_errors([MOUNTED], comps)

    def start(self, comps=None, **kwargs):
        """
        Start Lustre file system servers.
        """
        comps = (comps or self.components).managed(supports='start')

        # What starting order to use?
        key = lambda t: t.TYPE == MDT.TYPE
        for target in comps.filter(key=key):
            # Found enabled MDT: perform writeconf check.
            self.status(comps=ComponentGroup([target]))
            if target.has_first_time_flag() or target.has_writeconf_flag():
                # first_time or writeconf flag found, start MDT before OSTs
                MDT.START_ORDER, OST.START_ORDER = OST.START_ORDER, MDT.START_ORDER

        # Iterate over targets, grouping them by start order and server.
        for _order, targets in comps.groupby(attr='START_ORDER'):
            grp = ActionGroup()
            modprobe = None

            for server, subtargets in targets.groupbyserver():

                if server.is_local():
                    if not modprobe:
                        modprobe = server.load_modules()
                    # Start targets if we are on the good server.
                    for target in subtargets:
                        # Note that target.start() should never block here:
                        # it will perform necessary non-blocking actions and
                        # (when needed) will start local ClusterShell workers.
                        grp.add(target.start(**kwargs))
                else:
                    # Start per selected targets on this server.
                    self._proxy_action('start', server.hostname, subtargets,
                                       **kwargs).launch()

            if len(grp):
                grp.depends_on(modprobe)
                modprobe.launch()

            # Resume current task, ie. start runloop, process workers events
            # and also act as a target-type barrier.
            self._run_actions()

            # Avoid broken cascading starts, so we break now if
            # a target of the previous type failed to start.
            result = self._check_errors([MOUNTED, RECOVERING], targets)
            if result not in [MOUNTED, RECOVERING]:
                return result

        return MOUNTED


    def stop(self, comps=None, **kwargs):
        """
        Stop file system.
        """
        comps = (comps or self.components).managed(supports='stop')

        # We use a similar logic than start(): see start() for comments.
        # iterate over targets by start order and server
        for _order, targets in comps.groupby(attr='START_ORDER', reverse=True):
            grp = ActionGroup()
            rmmod = None
            for server, subtargets in targets.groupbyserver():

                if server.is_local():
                    if not rmmod:
                        rmmod = server.unload_modules()
                    # Stop targets if we are on the good server.
                    for target in subtargets:
                        grp.add(target.stop(**kwargs))
                else:
                    # Stop per selected targets on this server.
                    self._proxy_action('stop', server.hostname, subtargets,
                                       **kwargs).launch()

            if len(grp):
                rmmod.depends_on(grp)
                grp.launch()

            # Run local actions and FSProxyAction
            self._run_actions()
        
            result = self._check_errors([OFFLINE], targets)
            if result != OFFLINE:
                return result

        return OFFLINE

    def mount(self, comps=None, **kwargs):
        """
        Mount FS clients.
        """
        comps = (comps or self.components).managed(supports='mount')

        modprobe = None
        grp = ActionGroup()
        for server, clients in comps.groupbyserver():

            if server.is_local():
                if not modprobe:
                    modprobe = server.load_modules()
                # local client
                for comp in clients:
                    grp.add(comp.mount(**kwargs))
            else:
                # distant client
                self._proxy_action('mount', server.hostname, clients,
                                    **kwargs).launch()

        if len(grp):
            grp.depends_on(modprobe)
            grp.launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        # Ok, workers have completed, perform late status check...
        return self._check_errors([MOUNTED], comps)

    def umount(self, comps=None, **kwargs):
        """
        Unmount FS clients.
        """
        comps = (comps or self.components).managed(supports='umount')

        rmmod = None
        grp = ActionGroup()
        for server, clients in comps.groupbyserver():
            if server.is_local():
                if not rmmod:
                    rmmod = server.unload_modules()
                # local clients
                for comp in clients:
                    comp.umount(**kwargs).launch()
            else:
                # distant clients
                self._proxy_action('umount', server.hostname, clients,
                                   **kwargs).launch()

        if len(grp):
            rmmod.depends_on(grp)
            grp.launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        # Ok, workers have completed, perform late status check...
        return self._check_errors([OFFLINE], comps)

    def execute(self, comps=None, **kwargs):
        """
        Execute custom command.
        """
        comps = (comps or self.components).managed(supports='execute')
        for server, srvcomps in comps.groupbyserver():

            if server.is_local():
                # local client
                for comp in srvcomps:
                    comp.execute(**kwargs).launch()
            else:
                # distant client
                self._proxy_action('execute', server.hostname, srvcomps,
                                   **kwargs).launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        # Here we check MOUNTED but in fact, any status is OK.
        # XXX: Is that ok, to check MOUNTED here?
        return self._check_errors([MOUNTED], comps)

    def tune(self, tuning_model, comps=None, **kwargs):
        """
        Tune server.
        """
        comps = (comps or self.components).managed()

        type_map = { 'mgt': 'mgs', 
                     'mdt': 'mds', 
                     'ost': 'oss', 
                     'client': 'client', 
                     'router': 'router' }

        # Copy tuning file on distant servers
        tuning_conf = Globals().get_tuning_file()
        if tuning_conf:
            self._distant_action_by_server(Install, comps.servers(),
                                           config_file=tuning_conf)

        # Apply tunings
        for server, srvcomps in comps.groupbyserver():
            if server.is_local():
                types = set([type_map[tgt.TYPE] for tgt in srvcomps])
                server.tune(tuning_model, types, self.fs_name)
            else:
                self._proxy_action('tune', server.hostname, srvcomps,
                                   **kwargs).launch()

        # Run local actions and FSProxyAction
        self._run_actions()

        # Check for proxy errors, and return 'result' if no proxy errors
        result = task_self().max_retcode()
        return self._check_errors([result])
