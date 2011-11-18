#!/usr/bin/python

# Copyright (c) 2011 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Cros unit test library, with utility functions."""

import cStringIO
import os
import re
import shutil
import sys
import tempfile
import unittest

import mox

# pylint: disable=W0212,R0904

def tempdir_decorator(func):
  """Populates self.tempdir with path to a temporary writeable directory."""
  def f(self, *args, **kwargs):
    self.tempdir = tempfile.mkdtemp()
    try:
      os.chmod(self.tempdir, 0700)
      return func(self, *args, **kwargs)
    finally:
      if os.path.exists(self.tempdir):
        shutil.rmtree(self.tempdir)

  f.__name__ = func.__name__
  return f


def tempfile_decorator(func):
  """Populates self.tempfile with path to a temporary writeable file"""
  def f(self, *args, **kwargs):
    tmpfile = tempfile.NamedTemporaryFile(dir=self.tempdir, delete=False)
    tmpfile.close()
    self.tempfile = tmpfile.name
    return func(self, *args, **kwargs)

  f.__name__ = func.__name__
  return tempdir_decorator(f)

class EasyAttr(dict):
  """Convenient class for simulating objects with attributes in tests.

  An EasyAttr object can be created with any attributes initialized very
  easily.  Examples:

  1) An object with .id=45 and .name="Joe":
  testobj = EasyAttr(id=45, name="Joe")
  2) An object with .title.text="Big" and .owner.text="Joe":
  testobj = EasyAttr(title=EasyAttr(text="Big"), owner=EasyAttr(text="Joe"))
  """

  __slots__ = ()

  def __getattr__(self, attr):
    try:
      return self[attr]
    except KeyError:
      return AttributeError(attr)

  def __delattr__(self, attr):
    try:
      self.pop(attr)
    except KeyError:
      raise AttributeError(attr)

  def __setattr__(self, attr, value):
    self[attr] = value

  def __dir__(self):
    return self.keys()

class TestCase(unittest.TestCase):
  """Base class for cros unit tests with utility methods."""

  # This works with error output from operation module.
  ERROR_PREFIX = re.compile('^\033\[1;31m')

  __slots__ = ['_stderr', '_stderr_cap', '_stdout', '_stdout_cap']

  def __init__(self, arg):
    """Base class __init__ takes a second argument."""
    unittest.TestCase.__init__(self, arg)

    self._stdout = None
    self._stderr = None
    self._stdout_cap = None
    self._stderr_cap = None

  def _IsErrorLine(self, line):
    """Return True if |line| has prefix associated with error output."""
    return self.ERROR_PREFIX.search(line)

  def _StartCapturingOutput(self):
    """Begin capturing stdout and stderr."""
    self._stdout = sys.stdout
    self._stderr = sys.stderr
    sys.stdout = self._stdout_cap = cStringIO.StringIO()
    sys.stderr = self._stderr_cap = cStringIO.StringIO()

  def _RetrieveCapturedOutput(self):
    """Return captured output so far as (stdout, stderr) tuple."""
    try:
      if self._stdout and self._stderr:
        return (self._stdout_cap.getvalue(), self._stderr_cap.getvalue())
      return None
    except AttributeError:
      # This will happen if output capturing was never on.
      return None

  def _StopCapturingOutput(self):
    """Stop capturing stdout and stderr."""
    try:
      sys.stdout = self._stdout
      sys.stderr = self._stderr
      self._stdout = None
      self._stderr = None
    except AttributeError:
      # This will happen if output capturing was never on.
      pass

  def _AssertOutputEndsInError(self, output):
    """Return True if |output| ends with an error message."""
    lastline = [ln for ln in output.split('\n') if ln][-1]
    self.assertTrue(self._IsErrorLine(lastline),
                    msg='expected output to end in error line, but '
                    '_IsErrorLine says this line is not an error:\n%s' %
                    lastline)

class MoxTestCase(TestCase, mox.MoxTestBase):
  """Add mox.MoxTestBase super class to TestCase."""
