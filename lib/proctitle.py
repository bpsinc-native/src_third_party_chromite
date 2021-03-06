#!/usr/bin/python
# Copyright 2014 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Wrapper module for dealing with setting the process title (seen in `ps`)."""

import __main__ as main
import os

# Import the relevant funcs into our namespace for callers.
try:
  # pylint: disable=W0611, F0401
  from setproctitle import getproctitle, setproctitle
except ImportError:
  # Module not available -> can't do anything.
  getproctitle = lambda: None
  setproctitle = lambda _x: None


# Used with the settitle helper below.
_SCRIPT_NAME = os.path.basename(main.__file__)


# Used to distinguish between different runs.
_TITLE_PID = os.getpid()


def settitle(*args):
  """Set the process title to something useful to make `ps` output easy."""
  base = ('%s/%s' % (_SCRIPT_NAME, _TITLE_PID),)
  setproctitle(': '.join(base + args))
