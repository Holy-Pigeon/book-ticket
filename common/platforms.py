"""Small OS adapters; booking projects do not invoke platform commands directly."""
import base64
import os
from pathlib import Path
import subprocess
import sys

from .errors import LocalError


def system():
    if sys.platform == 'win32':
        return 'windows'
    if sys.platform == 'darwin':
        return 'macos'
    raise LocalError('目前支持 Windows 和 macOS，请在支持的系统运行。')


def command_options():
    return {'creationflags': subprocess.CREATE_NO_WINDOW} if system() == 'windows' else {}


def open_proxy_settings():
    try:
        if system() == 'windows':
            os.startfile('ms-settings:network-proxy')
        else:
            subprocess.run(['/usr/bin/open', '-b', 'com.apple.systempreferences'],
                           check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        raise LocalError('无法打开系统设置，请手动打开网络代理设置。') from None


def proxy_instructions(port, https=False):
    if system() == 'windows':
        return f'设置 → 网络和 Internet → 代理 → 手动代理：127.0.0.1:{port}。'
    kinds = '网页代理 (HTTP) 和安全网页代理 (HTTPS)' if https else '网页代理 (HTTP)'
    return (f'系统设置 → 网络 → 当前连接 → 详细信息 → 代理：启用{kinds}，'
            f'服务器均为 127.0.0.1，端口 {port}；点“好”应用。')


def certificate_scope(host):
    if system() == 'windows':
        return '当前 Windows 用户的受信任根证书存储（账户级信任）'
    return f'当前 macOS 用户的登录钥匙串（仅 SSL、主机 {host}；系统可能要求授权）'


def restrict_private_directory(directory):
    """Restrict a newly created, owned temporary directory before writing keys."""
    if system() == 'macos':
        Path(directory).chmod(0o700)
        return
    # .NET APIs avoid PowerShell module-path conflicts when launched from pwsh.
    # A separate environment value handles spaces/quotes without script interpolation.
    script = ('$ErrorActionPreference="Stop";$target=$env:BOOK_TICKET_PRIVATE_DIRECTORY;'
              '$sid=[System.Security.Principal.WindowsIdentity]::GetCurrent().User;'
              '$acl=[System.Security.AccessControl.DirectorySecurity]::new();'
              '$acl.SetAccessRuleProtection($true,$false);'
              '$rule=[System.Security.AccessControl.FileSystemAccessRule]::new('
              '$sid,"FullControl","ContainerInherit,ObjectInherit","None","Allow");'
              '$acl.AddAccessRule($rule);[System.IO.Directory]::SetAccessControl($target,$acl)')
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    args = ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    environment = dict(os.environ, BOOK_TICKET_PRIVATE_DIRECTORY=str(directory))
    try:
        subprocess.run(args, env=environment, check=True, capture_output=True, **command_options())
    except (OSError, subprocess.SubprocessError):
        raise LocalError('无法限制临时私钥目录的访问权限，采集未启动。') from None


class UserCertificateTrust:
    """Only an explicitly requested temporary certificate in the current user store."""
    def __init__(self, path, thumbprint, host):
        self.path, self.thumbprint, self.host = Path(path), thumbprint, host
        self.installed = False
        self.cleanup_required = False
        self.keychain = Path.home() / 'Library/Keychains/login.keychain-db'

    def install(self):
        if system() == 'windows':
            args = ['certutil.exe', '-user', '-addstore', 'Root', str(self.path)]
        else:
            args = ['/usr/bin/security', 'add-trusted-cert', '-r', 'trustRoot',
                    '-p', 'ssl', '-s', self.host, '-k', str(self.keychain), str(self.path)]
        # Installation can add a certificate before the user denies a trust dialog.
        self.cleanup_required = True
        try:
            subprocess.run(args, check=True, capture_output=True, **command_options())
        except (OSError, subprocess.SubprocessError):
            raise LocalError('临时根证书未安装成功，采集未启动；正在尝试清理。') from None
        self.installed = True

    def remove(self):
        if not self.cleanup_required:
            return
        if system() == 'windows':
            args = ['certutil.exe', '-user', '-delstore', 'Root', self.thumbprint]
            guidance = 'certmgr.msc 的受信任根证书'
        else:
            # -t removes per-user trust settings as well as the exact certificate.
            args = ['/usr/bin/security', 'delete-certificate', '-t', '-Z',
                    self.thumbprint, str(self.keychain)]
            guidance = '钥匙串访问的“登录”钥匙串'
        try:
            result = subprocess.run(args, capture_output=True, **command_options())
            missing_after_failed_install = (system() == 'macos' and not self.installed
                                            and result.returncode == 44)  # errSecItemNotFound
            if result.returncode and not missing_after_failed_install:
                raise OSError()
        except (OSError, subprocess.SubprocessError):
            raise LocalError(f'证书自动移除失败。请在 {guidance} 按窗口指纹移除此证书；再重试结束采集。') from None
        self.installed = self.cleanup_required = False
