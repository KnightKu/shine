# Task.py -- Cluster task management
# Copyright (C) 2007 CEA
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

from EnginePdsh import EnginePdsh

from WorkerPdsh import WorkerPdsh
from WorkerSh import WorkerSh

import sys

# Only in 2.4
#import threading

TASK_CURRENT = None

class Task:

    def __init__(self):
        self.engine = EnginePdsh()

    def current(cls):
        """
        Get current task instance.
        """
        global TASK_CURRENT

        if TASK_CURRENT:
            return TASK_CURRENT

        TASK_CURRENT = Task()

        return TASK_CURRENT
    current = classmethod(current)

    def shell(self, command, nodes=None, handler=None):
        """
        Execute local or distant shell command.
        """
    
        # The shell() method supports local or distant execution, let's choose one now.
        if nodes:
            worker = WorkerPdsh(nodes, command=command, handler=handler)
        else:
            worker = WorkerSh(command, handler)

        # Schedule task for this new shell worker.
        self.engine.add(worker)

        return worker

    def copy(self, source, dest, nodes, handler=None):
        """
        Copy local file to distant nodes.
        """
        assert nodes != None

        # Start new Pdcp worker (supported by WorkerPdsh)
        worker = WorkerPdsh(nodes, source=source, dest=dest, handler=handler)

        # Schedule task for this new copy worker.
        self.engine.add(worker)

        return worker

    def run(self, timeout=0):
        self.engine.run(timeout)
        self.reset()

    def reset(self):
        self.engine.reset()

    def read(self, node):
        return self.engine.read(node)

    def retcode(self, node):
        return self.engine.retcode(node)

