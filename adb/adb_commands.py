# Copyright 2014 Google Inc. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""A libusb1-based ADB reimplementation.

ADB was giving us trouble with its client/server architecture, which is great
for users and developers, but not so great for reliable scripting. This will
allow us to more easily catch errors as Python exceptions instead of checking
random exit codes, and all the other great benefits from not going through
subprocess and a network socket.

All timeouts are in milliseconds.
"""

import io
import os
import socket

from adb import adb_protocol
from adb import common
from adb import filesync_protocol

# From adb.h
CLASS = 0xFF
SUBCLASS = 0x42
PROTOCOL = 0x01
# pylint: disable=invalid-name
DeviceIsAvailable = common.InterfaceMatcher(CLASS, SUBCLASS, PROTOCOL)


try:
  # Imported locally to keep compatibility with previous code.
  from adb.sign_m2crypto import M2CryptoSigner
except ImportError:
  # Ignore this error when M2Crypto is not installed, there are other options.
  pass


class AdbCommands(object):
  """Exposes adb-like methods for use.

  Some methods are more-pythonic and/or have more options.
  """
  protocol_handler = adb_protocol.AdbMessage
  filesync_handler = filesync_protocol.FilesyncProtocol

  @classmethod
  def ConnectDevice(
      cls, port_path=None, serial=None, default_timeout_ms=None, **kwargs):
    """Convenience function to get an adb device from usb path or serial.

    Args:
      port_path: The filename of usb port to use.
      serial: The serial number of the device to use.
      default_timeout_ms: The default timeout in milliseconds to use.

    If serial specifies a TCP address:port, then a TCP connection is
    used instead of a USB connection.
    """
    if serial and b':' in serial:
        handle = common.TcpHandle(serial, timeout_ms=default_timeout_ms)
    else:
        handle = common.UsbHandle.FindAndOpen(
            DeviceIsAvailable, port_path=port_path, serial=serial,
            timeout_ms=default_timeout_ms)
    return cls.Connect(handle, **kwargs)

  def __init__(self, handle, device_state):
    self.handle = handle
    self._device_state = device_state

  def Close(self):
    self.handle.Close()

  @classmethod
  def Connect(cls, usb, banner=None, **kwargs):
    """Connect to the device.

    Args:
      usb: UsbHandle or TcpHandle instance to use.
      banner: See protocol_handler.Connect.
      **kwargs: See protocol_handler.Connect for kwargs. Includes rsa_keys,
          and auth_timeout_ms.
    Returns:
      An instance of this class if the device connected successfully.
    """
    if not banner:
      banner = socket.gethostname().encode()
    device_state = cls.protocol_handler.Connect(usb, banner=banner, **kwargs)
    # Remove banner and colons after device state (state::banner)
    device_state = device_state.split(b':')[0]
    return cls(usb, device_state)

  @classmethod
  def Devices(cls):
    """Get a generator of UsbHandle for devices available."""
    return common.UsbHandle.FindDevices(DeviceIsAvailable)

  def GetState(self):
    return self._device_state

  def Install(self, apk_path, destination_dir='', timeout_ms=None, replace_existing=True, transfer_progress_callback=None):
    """Install an apk to the device.

    Doesn't support verifier file, instead allows destination directory to be
    overridden.

    Args:
      apk_path: Local path to apk to install.
      destination_dir: Optional destination directory. Use /system/app/ for
        persistent applications.
      replace_existing: whether to replace existing application
      timeout_ms: Expected timeout for pushing and installing.
      transfer_progress_callback: callback method that accepts filename, bytes_written and total_bytes of APK transfer

    Returns:
      The pm install output.
    """
    if not destination_dir:
      destination_dir = '/data/local/tmp/'
    basename = os.path.basename(apk_path)
    destination_path = destination_dir + basename
    self.Push(apk_path, destination_path, timeout_ms=timeout_ms, progress_callback=transfer_progress_callback)
    
    cmd = ['pm install']
    if replace_existing:
      cmd.append('-r')
    cmd.append('"%s"' % destination_path)
    return self.Shell(' '.join(cmd), timeout_ms=timeout_ms)
  
  def Uninstall(self, package_name, keep_data=False, timeout_ms=None):
    """Removes a package from the device.

    Args:
      package_name: Package name of target package.
      keep_data: whether to keep the data and cache directories
      timeout_ms: Expected timeout for pushing and installing.

    Returns:
      The pm uninstall output.
    """
    cmd = ['pm uninstall']
    if keep_data:
      cmd.append('-k')
    cmd.append('"%s"' % package_name)
    return self.Shell(' '.join(cmd), timeout_ms=timeout_ms)

  def Push(self, source_file, device_filename, mtime='0', timeout_ms=None, progress_callback=None):
    """Push a file or directory to the device.

    Args:
      source_file: Either a filename, a directory or file-like object to push to
                   the device.
      device_filename: Destination on the device to write to.
      mtime: Optional, modification time to set on the file.
      timeout_ms: Expected timeout for any part of the push.
      progress_callback: callback method that accepts filename, bytes_written and total_bytes,
                         total_bytes will be -1 for file-like objects
    """
    should_close = False
    if isinstance(source_file, str):
      if os.path.isdir(source_file):
        self.Shell("mkdir " + device_filename)
        for f in os.listdir(source_file):
          self.Push(os.path.join(source_file, f), device_filename + '/' + f, progress_callback=progress_callback)
        return
      source_file = open(source_file, "rb")
      should_close = True

    connection = self.protocol_handler.Open(
        self.handle, destination=b'sync:', timeout_ms=timeout_ms)
    self.filesync_handler.Push(connection, source_file, device_filename,
                               mtime=int(mtime), progress_callback=progress_callback)
    if should_close:
      source_file.close()
    connection.Close()

  def Pull(self, device_filename, dest_file='', progress_callback=None, timeout_ms=None):
    """Pull a file from the device.

    Args:
      device_filename: Filename on the device to pull.
      dest_file: If set, a filename or writable file-like object.
      timeout_ms: Expected timeout for any part of the pull.

    Returns:
      The file data if dest_file is not set.
    """
    if not dest_file:
      dest_file = io.BytesIO()
    elif isinstance(dest_file, str):
      dest_file = open(dest_file, 'wb')
    connection = self.protocol_handler.Open(
        self.handle, destination=b'sync:',
        timeout_ms=timeout_ms)
    self.filesync_handler.Pull(connection, device_filename, dest_file, progress_callback)
    connection.Close()
    if isinstance(dest_file, io.BytesIO):
      return dest_file.getvalue()

  def Stat(self, device_filename):
    """Get a file's stat() information."""
    connection = self.protocol_handler.Open(self.handle, destination=b'sync:')
    mode, size, mtime = self.filesync_handler.Stat(
        connection, device_filename)
    connection.Close()
    return mode, size, mtime

  def List(self, device_path):
    """Return a directory listing of the given path.

    Args:
      device_path: Directory to list.
    """
    connection = self.protocol_handler.Open(self.handle, destination=b'sync:')
    listing = self.filesync_handler.List(connection, device_path)
    connection.Close()
    return listing

  def Reboot(self, destination=b''):
    """Reboot the device.

    Args:
      destination: Specify 'bootloader' for fastboot.
    """
    self.protocol_handler.Open(self.handle, b'reboot:%s' % destination)

  def RebootBootloader(self):
    """Reboot device into fastboot."""
    self.Reboot(b'bootloader')

  def Remount(self):
    """Remount / as read-write."""
    return self.protocol_handler.Command(self.handle, service=b'remount')

  def Root(self):
    """Restart adbd as root on the device."""
    return self.protocol_handler.Command(self.handle, service=b'root')

  def EnableVerity(self):
    """Re-enable dm-verity checking on userdebug builds"""
    return self.protocol_handler.Command(self.handle, service=b'enable-verity')

  def DisableVerity(self):
    """Disable dm-verity checking on userdebug builds"""
    return self.protocol_handler.Command(self.handle, service=b'disable-verity')

  def Shell(self, command, timeout_ms=None):
    """Run command on the device, returning the output."""
    return self.protocol_handler.Command(
        self.handle, service=b'shell', command=command,
        timeout_ms=timeout_ms)

  def StreamingShell(self, command, timeout_ms=None):
    """Run command on the device, yielding each line of output.

    Args:
      command: Command to run on the target.
      timeout_ms: Maximum time to allow the command to run.

    Yields:
      The responses from the shell command.
    """
    return self.protocol_handler.StreamingCommand(
        self.handle, service=b'shell', command=command,
        timeout_ms=timeout_ms)

  def Logcat(self, options, timeout_ms=None):
    """Run 'shell logcat' and stream the output to stdout.

    Args:
      options: Arguments to pass to 'logcat'.
    """
    return self.StreamingShell('logcat %s' % options, timeout_ms)
