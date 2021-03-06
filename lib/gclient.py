# Copyright (c) 2012 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Common functions used for syncing Chrome."""

import os
import pprint
import re

from chromite.lib import cros_build_lib
from chromite.lib import osutils

CHROME_COMMITTER_URL = 'svn://svn.chromium.org/chrome'
SVN_MIRROR_URL = 'svn://svn-mirror.golo.chromium.org'
STATUS_URL = 'https://chromium-status.appspot.com/current?format=json'


def FindGclientFile(path):
  """Returns the nearest higher-level gclient file from the specified path.

  Args:
    path: The path to use. Defaults to cwd.
  """
  return osutils.FindInPathParents(
      '.gclient', path, test_func=os.path.isfile)


def FindGclientCheckoutRoot(path):
  """Get the root of your gclient managed checkout."""
  gclient_path = FindGclientFile(path)
  if gclient_path:
    return os.path.dirname(gclient_path)
  return None


def _UseGoloMirror():
  """Check whether to use the golo.chromium.org mirrors.

  This function returns whether or not we should use the mirrors from
  golo.chromium.org, which we presume are only accessible from within
  that subdomain, and a few other known exceptions.
  """
  host = cros_build_lib.GetHostName(fully_qualified=True)
  GOLO_SUFFIXES = [
      '.golo.chromium.org',
      '.chrome.corp.google.com',
  ]
  return any([host.endswith(s) for s in GOLO_SUFFIXES])


def GetBaseURLs():
  """Get the base URLs for checking out Chromium and Chrome."""
  if _UseGoloMirror():
    external_url = '%s/chrome' % SVN_MIRROR_URL
    internal_url = '%s/chrome-internal' % SVN_MIRROR_URL
  else:
    external_url = 'http://src.chromium.org/svn'
    internal_url = 'svn://svn.chromium.org/chrome-internal'

  return external_url, internal_url


def GetTipOfTrunkSvnRevision(svn_url):
  """Returns the current svn revision for the chrome tree."""
  cmd = ['svn', 'info', svn_url]
  svn_info = cros_build_lib.RunCommand(cmd, redirect_stdout=True).output

  revision_re = re.compile(r'^Revision:\s+(\d+)')
  for line in svn_info.splitlines():
    match = revision_re.match(line)
    if match:
      svn_revision = match.group(1)
      cros_build_lib.Info('Found SVN Revision %s' % svn_revision)
      return svn_revision

  raise Exception('Could not find revision information from %s' % svn_url)


def _GetGclientURLs(internal, rev):
  """Get the URLs to use in gclient file.

  See WriteConfigFile below.
  """
  results = []
  external_url, internal_url = GetBaseURLs()

  if rev is None or isinstance(rev, (int, long)):
    rev_str = '@%s' % rev if rev else ''
    results.append(('src', '%s/trunk/src%s' % (external_url, rev_str)))
    if internal:
      results.append(('src-internal', '%s/trunk/src-internal' % internal_url))
  elif internal:
    # TODO(petermayo): Fall back to the archive directory if needed.
    primary_url = '%s/trunk/tools/buildspec/releases/%s' % (internal_url, rev)
    results.append(('CHROME_DEPS', primary_url))
  else:
    results.append(('CHROMIUM_DEPS', '%s/releases/%s' % (external_url, rev)))

  return results


def _GetGclientSolutions(internal, rev):
  """Get the solutions array to write to the gclient file.

  See WriteConfigFile below.
  """
  urls = _GetGclientURLs(internal, rev)
  custom_deps, custom_vars = {}, {}
  if _UseGoloMirror():
    custom_vars.update({
      'svn_url': SVN_MIRROR_URL,
      'webkit_trunk': '%s/webkit-readonly/trunk' % SVN_MIRROR_URL,
      'googlecode_url': SVN_MIRROR_URL + '/%s',
      'gsutil': SVN_MIRROR_URL + '/gsutil',
      'sourceforge_url': SVN_MIRROR_URL + '/%(repo)s'
    })

  solutions = [{'name': name,
                'url': url,
                'custom_deps': custom_deps,
                'custom_vars': custom_vars} for (name, url) in urls]
  return solutions


def _GetGclientSpec(internal, rev):
  """Return a formatted gclient spec.

  See WriteConfigFile below.
  """
  solutions = _GetGclientSolutions(internal=internal, rev=rev)
  return 'solutions = %s\n' % pprint.pformat(solutions)


def WriteConfigFile(gclient, cwd, internal, rev):
  """Initialize the specified directory as a gclient checkout.

  For gclient documentation, see:
    http://src.chromium.org/svn/trunk/tools/depot_tools/README.gclient

  Args:
    gclient: Path to gclient.
    cwd: Directory to sync.
    internal: Whether you want an internal checkout.
    rev: Revision or tag to use. If None, use the latest from trunk. If this is
      a number, use the specified revision. If this is a string, use the
      specified tag.
  """
  spec = _GetGclientSpec(internal=internal, rev=rev)
  cmd = [gclient, 'config', '--spec', spec]
  cros_build_lib.RunCommand(cmd, cwd=cwd)


def Revert(gclient, cwd):
  """Revert all local changes.

  Args:
    gclient: Path to gclient.
    cwd: Directory to revert.
  """
  cros_build_lib.RunCommand([gclient, 'revert', '--nohooks'], cwd=cwd)


def Sync(gclient, cwd, reset=False):
  """Sync the specified directory using gclient.

  Args:
    gclient: Path to gclient.
    cwd: Directory to sync.
    reset: Reset to pristine version of the source code.
  """
  cmd = [gclient, 'sync', '--verbose', '--nohooks', '--transitive',
         '--manually_grab_svn_rev']
  if reset:
    cmd += ['--reset', '--force', '--delete_unversioned_trees']
  cros_build_lib.RunCommand(cmd, cwd=cwd)
