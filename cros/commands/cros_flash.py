# Copyright (c) 2013 The Chromium OS Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Install/copy the image to the device."""

import cStringIO
import hashlib
import logging
import os
import shutil
import tempfile
import urlparse

from chromite import cros
from chromite.buildbot import constants
from chromite.lib import cros_build_lib
from chromite.lib import dev_server_wrapper as ds_wrapper
from chromite.lib import osutils
from chromite.lib import remote_access
from chromite.lib import timeout_util


# The folder in devserver's static_dir that cros flash uses to store
# symlinks to local images given by user. Note that this means if
# there is a board or board-version (e.g. peppy-release) named
# 'others', there will be a conflict.
DEVSERVER_LOCAL_IMAGE_SYMLINK_DIR = 'others'

IMAGE_NAME_TO_TYPE = {
    'chromiumos_test_image.bin': 'test',
    'chromiumos_image.bin': 'dev',
    'chromiumos_base_image.bin': 'base'
}


def _CheckBoardMismatch(xbuddy_path, board):
  """Returns True if |board| doesn't match the board in |xbuddy_path|"""
  # xbuddy_path: {local|remote}/board/version. The local/remote
  # keyword is optional.
  path_list = xbuddy_path.split('/')
  keywords = ('local', 'remote')
  if path_list[0] in keywords:
    xbuddy_board = path_list[1]
  else:
    xbuddy_board = path_list[0]

  return xbuddy_board != board


# pylint: disable=E1101
def GenerateXbuddyRequest(path, static_dir, board=None):
  """Generate an xbuddy request used to retreive payloads.

  This function generates a xbuddy request based on |path|. If the
  request is sent to the devserver, the server will respond with a
  URL pointing to the folder of update payloads.

  If |path| is an xbuddy path (xbuddy://subpath), strip the '://"
  and returns xbuddy/subpath. If |path| is a local path to an image,
  creates a symlink in the static_dir, so that xbuddy can access the
  image; returns the corresponding xbuddy path. If |path| is 'latest',
  returns the request for the latest local build for |board|.

  Args:
    path: Either a local path to an image or a xbuddy path (xbuddy://)
      or 'latest' for latest local build.
    static_dir: static directory of the local devserver.
    board: The board to use when no board is specified in |path|.

  Returns:
    A xbuddy request.
  """
  if path == 'latest':
    # Use the latest local build.
    board_to_use = board if board else cros_build_lib.GetDefaultBoard()
    path = 'xbuddy://local/%s/latest' % board_to_use

  parsed = urlparse.urlparse(path)
  if parsed.scheme == 'xbuddy':
    xbuddy_path = '%s%s' % (parsed.netloc, parsed.path)
    if board and _CheckBoardMismatch(xbuddy_path, board):
      logging.warning(
          '%s does not match device board %s.', xbuddy_path, board)
      if not cros_build_lib.BooleanPrompt(default=False):
        cros_build_lib.Die('Exiting Cros Flash...')
  else:
    if not os.path.exists(path):
      raise ValueError('Image path does not exist: %s' % path)

    chroot_path = ds_wrapper.ToChrootPath(path)
    # Create and link static_dir/DEVSERVER_LOCAL_IMAGE_SYMLINK_DIR/hashed_path
    # to the image folder, so that xbuddy/devserver can understand the path.
    # Alternatively, we can to pass '--image' at devserver startup, but this
    # flag is to be deprecated soon.
    relative_dir = os.path.join(DEVSERVER_LOCAL_IMAGE_SYMLINK_DIR,
                                hashlib.md5(chroot_path).hexdigest())
    abs_dir = os.path.join(static_dir, relative_dir)
    # Make the parent directory if it doesn't exist.
    osutils.SafeMakedirs(os.path.dirname(abs_dir))
    # Create the symlink if it doesn't exist.
    if not os.path.lexists(abs_dir):
      logging.info('Creating a symlink %s -> %s', abs_dir,
                   os.path.dirname(chroot_path))
      os.symlink(os.path.dirname(chroot_path), abs_dir)

    xbuddy_path = os.path.join(relative_dir,
                               IMAGE_NAME_TO_TYPE[os.path.basename(path)])

  return 'xbuddy/%s?for_update=true&return_dir=true' % xbuddy_path


class DeviceUpdateError(Exception):
  """Thrown when there is an error during device update."""


@cros.CommandDecorator('flash')
class FlashCommand(cros.CrosCommand):
  """Update the device with an image.

  This command updates the device with the image. It assumes that
  device is able to accept ssh connections.

  For rootfs partition update, this command may launch a devserver to
  generate payloads. As a side effect, it may create symlinks in
  static_dir/others used by the devserver.
  """

  EPILOG = """
To update the device with the latest locally built image:
  cros flash device latest

To update the device with an xbuddy path:
  cros flash device xbuddy://{local|remote}/board/version

  Common xbuddy version aliases are latest, latest-{dev|beta|stable}, and
  latest-official, where 'latest' is a short version of latest-stable.

  For more information about xbuddy paths, please see
  http://www.chromium.org/chromium-os/how-tos-and-troubleshooting/\
using-the-dev-server/xbuddy-for-devserver
"""

  CHERRYPY_ERROR_MSG = """
If you see a cherrypy import error, that is because your device is
running an older image (<R33-4986.0.0) that does not have cherrypy
package installed by default.

Cros flash launches a devserver instance on the device to serve the
update payload for rootfs update. You can fix this with one of the
following three options.

  1. Update the device to a newer image with a USB stick.
  2. Run 'cros deploy device cherrypy' to install cherrpy.
  3. Use '--no-rootfs-update' to update the stateful parition first (with the
    risk that the rootfs/stateful version mismatch may cause some problems).
  """

  ROOTFS_FILENAME = 'update.gz'
  STATEFUL_FILENAME = 'stateful.tgz'
  DEVSERVER_STATIC_DIR = ds_wrapper.FromChrootPath(
      os.path.join(constants.CHROOT_SOURCE_ROOT, 'devserver', 'static'))
  DEVSERVER_PKG_DIR = os.path.join(constants.SOURCE_ROOT, 'src/platform/dev')
  STATEFUL_UPDATE_BIN = '/usr/bin/stateful_update'
  UPDATE_ENGINE_BIN = 'update_engine_client'
  # Timeout for update engine to update the device.
  UPDATE_ENGINE_TIMEOUT = 60 * 5
  # Root working directory on the device. This directory is in the
  # stateful partition and thus has enough space to store the payloads.
  DEVICE_WORK_DIR = '/mnt/stateful_partition/cros-flash'

  @classmethod
  def GetUpdateStatus(cls, device):
    """Returns the status of the update engine on the |device|."""
    result = device.RunCommand([cls.UPDATE_ENGINE_BIN, '--status'])
    if not result.output:
      logging.error('Cannot get update status')
      return None

    try:
      status = cros_build_lib.LoadKeyValueFile(
          cStringIO.StringIO(result.output))
    except ValueError:
      logging.error('Cannot parse update status')
      return None

    return status.get('CURRENT_OP', None)

  def UpdateStateful(self, device, payload):
    """Update the stateful partition of the device.

    Args:
      device: The RemoteDevice object to update.
      payload: The path to the update payload.
    """
    # Copy latest stateful_update to device.
    stateful_update_bin = ds_wrapper.FromChrootPath(self.STATEFUL_UPDATE_BIN)
    device.CopyToWorkDir(stateful_update_bin)

    logging.info('Updating stateful partition...')
    cmd = ['sh', os.path.join(device.work_dir,
                              os.path.basename(self.STATEFUL_UPDATE_BIN)), '-']
    device.PipeOverSSH(payload, cmd)
    logging.info('Payload decompressed to stateful partition.')

  def _CopyDevServerPackage(self, device, tempdir):
    """Copy devserver package to work directory of device.

    Args:
      device: The RemoteDevice object to copy the package to.
      tempdir: The directory to temporarily store devserver package.
    """
    logging.info('Copying devserver package to device...')
    src_dir = os.path.join(tempdir, 'src')
    osutils.RmDir(src_dir, ignore_missing=True)
    shutil.copytree(
        self.DEVSERVER_PKG_DIR, src_dir,
        ignore=shutil.ignore_patterns('*.pyc', 'tmp*', '.*', 'static', '*~'))
    device.CopyToWorkDir(src_dir)
    return os.path.join(device.work_dir, os.path.basename(src_dir))

  def UpdateRootfs(self, device, payload, tempdir):
    """Update the rootfs partition of the device.

    Args:
      device: The RemoteDevice object to update.
      payload: The path to the update payload.
      tempdir: The directory to store temporary files.
    """
    # Setup devserver and payload on the target device.
    static_dir = os.path.join(device.work_dir, 'static')
    payload_dir = os.path.join(static_dir, 'pregenerated')
    src_dir = self._CopyDevServerPackage(device, tempdir)
    device.RunCommand(['mkdir', '-p', payload_dir])
    logging.info('Copying rootfs payload to device...')
    device.CopyToDevice(payload, payload_dir)
    devserver_bin = os.path.join(src_dir, 'devserver.py')
    ds = ds_wrapper.RemoteDevServerWrapper(
        device, devserver_bin, static_dir=static_dir, log_dir=device.work_dir)

    logging.info('Checking if update engine is idle...')
    status = self.GetUpdateStatus(device)
    if status == 'UPDATE_STATUS_UPDATED_NEED_REBOOT':
      logging.info('Device needs to reboot before updating...')
      device.Reboot()
      status = self.GetUpdateStatus(device)

    if status != 'UPDATE_STATUS_IDLE':
      raise DeviceUpdateError('Update engine is not idle. Status: %s' % status)

    try:
      logging.info('Updating rootfs partition')
      ds.Start()

      omaha_url = ds.GetDevServerURL(ip=ds.device.hostname, port=ds.port,
                                     sub_dir='update/pregenerated')
      cmd = [self.UPDATE_ENGINE_BIN, '-check_for_update',
             '-omaha_url=%s' % omaha_url]
      device.RunCommand(cmd)

      def _CheckUpdateStatus(status):
        logging.info('Waiting for rootfs update...status is %s', status)
        if status == 'UPDATE_STATUS_UPDATED_NEED_REBOOT':
          return False
        elif status == 'UPDATE_STATUS_IDLE':
          raise DeviceUpdateError(
              'Update failed with unexpected update status: %s' % status)

        return True

      timeout_util.WaitForSuccess(_CheckUpdateStatus, self.GetUpdateStatus,
                                  timeout=self.UPDATE_ENGINE_TIMEOUT, period=5,
                                  func_args=[device])

    except timeout_util.TimeoutError:
      raise DeviceUpdateError('Timed out updating rootfs.')
    except ds_wrapper.DevServerException:
      logging.error(ds.TailLog())
      logging.error(self.CHERRYPY_ERROR_MSG)
      raise
    finally:
      ds.Stop()
      device.CopyFromDevice(ds.log_filename,
                            os.path.join(tempdir, 'target_devserver.log'))
      device.CopyFromDevice('/var/log/update_engine.log', tempdir,
                            follow_symlinks=True)

  def GetUpdatePayloads(self, path, payload_dir, board=None, src_image=None,
                        timeout=60 * 15):
    """Launch devserver to get the update payloads.

    Args:
      path: The image or xbuddy path.
      payload_dir: The directory to store the payloads.
      board: The default board to use when |path| is None.
      src_image: Image used as the base to generate the delta payloads.
      timeout: Timeout for launching devserver (seconds).
    """
    static_dir = self.DEVSERVER_STATIC_DIR
    ds = ds_wrapper.DevServerWrapper(
        static_dir=static_dir, src_image=src_image)
    req = GenerateXbuddyRequest(path, static_dir, board=board)
    logging.info('Starting local devserver to generate/serve payloads...')
    try:
      ds.Start()
      url = ds.OpenURL(ds.GetDevServerURL(sub_dir=req), timeout=timeout)
      ds.DownloadFile(os.path.join(url, self.ROOTFS_FILENAME), payload_dir)
      ds.DownloadFile(os.path.join(url, self.STATEFUL_FILENAME), payload_dir)
    except ds_wrapper.DevServerException:
      logging.error(ds.TailLog())
      raise
    finally:
      ds.Stop()
      if os.path.exists(ds.log_filename):
        shutil.copyfile(ds.log_filename,
                        os.path.join(payload_dir, 'local_devserver.log'))

  @classmethod
  def AddParser(cls, parser):
    """Add parser arguments."""
    super(FlashCommand, cls).AddParser(parser)
    parser.add_argument(
        'device', help='The hostname/IP address of the device.')
    parser.add_argument(
        'image', help="Image to install; can be a local path, a "
        "xbuddy path (xbuddy://{local|remote}/board/version), or simply "
        "'latest' for latest local build. You Can also specify a "
        "directory path with pre-generated payloads.")
    parser.add_argument(
        '--no-reboot', action='store_false', dest='reboot', default=True,
        help='Do not reboot after update. Default is always reboot.')
    parser.add_argument(
        '--no-wipe', action='store_false', dest='wipe', default=True,
        help='Do not wipe the temporary working directory.')
    parser.add_argument(
        '--no-stateful-update', action='store_false', dest='stateful_update',
        help='Do not update the stateful partition on the device.'
        'Default is always update.')
    parser.add_argument(
        '--no-rootfs-update', action='store_false', dest='rootfs_update',
        help='Do not update the rootfs partition on the device.'
        'Default is always update.')
    parser.add_argument(
        '--src-image', type='path',
        help='Local path to an image to be used as the base to generate '
        'payloads.')

  def __init__(self, options):
    """Initializes cros flash."""
    cros.CrosCommand.__init__(self, options)
    self.source_root = constants.SOURCE_ROOT
    self.do_stateful_update = False
    self.do_rootfs_update = False
    self.image_as_payload_dir = False
    self.reboot = True
    self.wipe = True
    self.tempdir = tempfile.mkdtemp(prefix='cros-flash')

  def Cleanup(self):
    """Cleans up the temporary directory."""
    if self.wipe:
      logging.info('Cleaning up temporary working directory...')
      osutils.RmDir(self.tempdir)
    else:
      logging.info('You can find the log files and/or payloads in %s',
                   self.tempdir)

  def _CheckPayloads(self, payload_dir):
    """Checks that all update payloads exists in |payload_dir|."""
    filenames = []
    filenames += [self.ROOTFS_FILENAME] if self.do_rootfs_update else []
    filenames += [self.STATEFUL_FILENAME] if self.do_stateful_update else []
    for fname in filenames:
      payload = os.path.join(payload_dir, fname)
      if not os.path.exists(payload):
        cros_build_lib.Die('Payload %s does not exist!' % payload)

  def _ReadOptions(self):
    """Check and read optoins."""
    options = self.options
    if not options.stateful_update and not options.rootfs_update:
      cros_build_lib.Die('No update operation to perform. Use -h to see usage.')

    # If the given path is a directory, we use the pre-generated
    # update payload(s) in the directory.
    if os.path.isdir(options.image):
      self.image_as_payload_dir = True

    self.reboot = options.reboot
    # Do not wipe if debug is set.
    self.wipe = options.wipe and not options.debug
    self.do_stateful_update =  options.stateful_update
    self.do_rootfs_update = options.rootfs_update

  # pylint: disable=E1101
  def Run(self):
    """Perfrom the cros flash command."""
    self._ReadOptions()

    try:
      with remote_access.RemoteDeviceHandler(
          self.options.device, work_dir=self.DEVICE_WORK_DIR) as device:

        if not device.board:
          logging.warning('Cannot detect the board name.')
          if not cros_build_lib.BooleanPrompt(default=False):
            cros_build_lib.Die('Exiting Cros Flash...')

        logging.info('Board is %s', device.board)

        if self.image_as_payload_dir:
          payload_dir = self.options.image
          logging.info('Using payloads in %s', payload_dir)
        else:
          # Launch a local devserver to generate/serve update payloads.
          payload_dir = self.tempdir
          self.GetUpdatePayloads(self.options.image, payload_dir,
                                 board=device.board,
                                 src_image=self.options.src_image)

        self._CheckPayloads(payload_dir)
        # Perform device updates.
        if self.do_rootfs_update:
          payload = os.path.join(payload_dir, self.ROOTFS_FILENAME)
          self.UpdateRootfs(device, payload, self.tempdir)
          logging.info('Rootfs update completed.')

        if self.do_stateful_update:
          payload = os.path.join(payload_dir, self.STATEFUL_FILENAME)
          self.UpdateStateful(device, payload)
          logging.info('Stateful update completed.')

        if self.reboot:
          logging.info('Rebooting device..')
          device.Reboot()

    except Exception:
      logging.error('Cros Flash failed before completing.')
      raise
    else:
      logging.info('Update performed successfully.')
    finally:
      self.Cleanup()