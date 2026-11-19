"""Verified one-click migration of the official UU plugin (legacy unit)."""
import logging
import os
import re
import stat
import tempfile
from pathlib import Path

_KNOWN_USERS = ("deck", "bazzite", "uu")
_EXEC_RE = re.compile(
    r"/bin/sh (?P<path>/(?:var/)?home/(?P<user>[A-Za-z0-9_-]+)/uu/uuplugin_monitor\.sh)"
)
_DECKY_HOME_RE = re.compile(r"/(?:var/)?home/([A-Za-z0-9_-]+)")
_VENDOR_NAMES = {
    "uuplugin_monitor.sh",
    "uuplugin_monitor.config",
    "uninstall.sh",
    "uu.tar.gz",
    "uu.tar.gz.md5",
    "uuplugin",
    "xuplugin-guardian",
    "uu.conf",
}
_TMP_NAMES = _VENDOR_NAMES | {"uu.update", "uu.uninstall"}
_CONFIG_LINE = "router=steam-deck-plugin"
_MAX_CONFIG = 4096

_log = logging.getLogger(__name__)


def _is_plain_root_owned(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return (
        stat.S_ISREG(st.st_mode)
        and st.st_uid == 0
        and not (st.st_mode & 0o022)
    )


class OfficialInstall:
    def __init__(self, root, system_dir, home, run):
        self.root = Path(root).resolve()
        self.run = run
        self._paused = False
        self._renamed = False
        self._backup = None
        self._was_enabled = False
        self._was_active = False

        self.unit = Path(system_dir) / "uuplugin.service"

        vendor = self.root / "usr/lib/systemd/system/uuplugin.service"
        if vendor.exists() or vendor.is_symlink():
            raise ValueError("vendor uuplugin.service present")

        if not _is_plain_root_owned(self.unit):
            raise ValueError("admin unit missing or not plain root-owned")

        text = self.unit.read_text(encoding="utf-8", errors="replace")
        sections = {}
        cur = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("[") and s.endswith("]"):
                cur = s[1:-1]
                sections.setdefault(cur, [])
            elif cur is not None and s and not s.startswith("#") and "=" in s:
                sections[cur].append(s)

        descriptions = [ln for ln in sections.get("Unit", []) if ln.startswith("Description=")]
        if descriptions != ["Description=UU Plugin"]:
            raise ValueError("unexpected Unit Description")

        svc = sections.get("Service", [])
        execs = [s for s in svc if s.startswith("ExecStart=")]
        if len(execs) != 1:
            raise ValueError("expected exactly one ExecStart")
        cmd = execs[0][len("ExecStart="):]
        m = _EXEC_RE.fullmatch(cmd)
        if not m:
            raise ValueError("unexpected ExecStart path")

        captured_path = m.group("path")
        captured_user = m.group("user")
        if captured_user not in _KNOWN_USERS:
            hm = _DECKY_HOME_RE.fullmatch(str(home or ""))
            if not hm or hm.group(1) != captured_user:
                raise ValueError("unexpected user in ExecStart path")

        root = self.root.resolve()
        requested = root / captured_path.lstrip("/")
        parent = requested.parent

        if (
            parent.is_symlink()
            or parent.parent.is_symlink()
            or (parent.exists() and not parent.is_dir())
        ):
            raise ValueError("install dir parent invalid")

        parent_resolved = parent.resolve()
        expected_a = root / "home" / captured_user / "uu"
        expected_b = root / "var" / "home" / captured_user / "uu"
        if parent_resolved != expected_a and parent_resolved != expected_b:
            raise ValueError("install dir mismatch: %s" % parent_resolved)

        self.install_dir = parent_resolved

        cfg = parent_resolved / "uuplugin_monitor.config"
        try:
            fd = os.open(
                str(cfg), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            )
        except FileNotFoundError:
            fd = None
        except OSError as e:
            raise ValueError("config open failed: %s" % e)
        if fd is not None:
            try:
                with os.fdopen(fd, "rb") as fh:
                    cst = os.fstat(fh.fileno())
                    if not stat.S_ISREG(cst.st_mode):
                        raise ValueError("config not regular")
                    data = fh.read(_MAX_CONFIG + 1)
            except ValueError:
                raise
            except OSError as e:
                raise ValueError("config read failed: %s" % e)
            if len(data) > _MAX_CONFIG:
                raise ValueError("config too large")
            lines = data.decode("utf-8", "replace").splitlines()
            if _CONFIG_LINE not in lines:
                raise ValueError("config missing required line")

    def _checked(self, args):
        r = self.run(args)
        if r.returncode != 0:
            err = (getattr(r, "stderr", "") or "").strip()
            raise RuntimeError(
                "命令执行失败 %s: %s" % (args, err or "未知错误")
            )
        return r

    def _query(self, which):
        r = self.run(["systemctl", which, "uuplugin.service"])
        return r.returncode == 0

    def pause(self):
        if self._paused:
            return
        self._was_enabled = self._query("is-enabled")
        self._was_active = self._query("is-active")
        self._paused = True

        self._checked(["systemctl", "stop", "uuplugin.service"])
        if self._query("is-active"):
            raise RuntimeError("停止后 uuplugin 仍处于运行状态")
        self._checked(["systemctl", "disable", "uuplugin.service"])

        if self.unit.is_symlink() or not self.unit.exists():
            raise RuntimeError("uuplugin.service 单元已消失或变为符号链接")

        fd, tmp = tempfile.mkstemp(
            prefix=".uuplugin-", suffix=".bak", dir=str(self.unit.parent)
        )
        os.close(fd)
        target = str(self.unit)
        try:
            os.replace(target, tmp)
        except OSError as e:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RuntimeError("备份 uuplugin.service 失败: %s" % e)
        self._backup = Path(tmp)
        self._renamed = True

        self._checked(["systemctl", "daemon-reload"])

    def restore(self):
        if not self._paused:
            return
        err = None
        if self._renamed:
            if self._backup is None or not self._backup.exists():
                raise RuntimeError("预期的单元备份缺失，无法恢复 uuplugin.service")
            try:
                os.replace(str(self._backup), str(self.unit))
                self._renamed = False
            except OSError as e:
                raise RuntimeError("恢复 uuplugin.service 失败: %s" % e)
            self._checked(["systemctl", "daemon-reload"])

        try:
            if self._was_enabled and not self._query("is-enabled"):
                self._checked(["systemctl", "enable", "uuplugin.service"])
        except Exception as e:
            if err is None:
                err = e
        try:
            if self._was_active and not self._query("is-active"):
                self._checked(["systemctl", "start", "uuplugin.service"])
        except Exception as e:
            if err is None:
                err = e
        if err is not None:
            raise err

    def finish(self):
        vdir = self.install_dir
        try:
            resolved = vdir.resolve()
        except OSError as e:
            _log.warning("跳过清理: 目标安装目录解析失败 %s: %s", vdir, e)
            resolved = None
        if resolved is None:
            pass
        elif resolved != self.install_dir:
            _log.warning("跳过清理: 目标安装目录解析发生变化 %s", vdir)
        else:
            self._cleanup(vdir, _VENDOR_NAMES)

        tmp = self.root / "tmp/uu"
        try:
            resolved_tmp = tmp.resolve()
        except OSError as e:
            _log.warning("跳过临时目录清理: 路径解析失败 %s: %s", tmp, e)
            resolved_tmp = None
        if resolved_tmp is not None:
            if resolved_tmp == tmp:
                try:
                    st = os.lstat(tmp)
                except FileNotFoundError:
                    st = None
                except OSError as e:
                    _log.warning("跳过临时目录清理: 无法检查 %s: %s", tmp, e)
                    st = None
                if (
                    st is not None
                    and stat.S_ISDIR(st.st_mode)
                    and st.st_uid == 0
                    and not (st.st_mode & 0o022)
                ):
                    self._cleanup(tmp, _TMP_NAMES)
            else:
                _log.warning("跳过临时目录清理: 路径解析不符合预期 %s", tmp)

        if self._backup is not None:
            try:
                if self._backup.exists() or self._backup.is_symlink():
                    os.unlink(str(self._backup))
            except OSError as e:
                _log.warning("删除临时单元备份失败 %s: %s", self._backup, e)

    def _cleanup(self, directory, names):
        try:
            dfd = os.open(
                str(directory), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            )
        except FileNotFoundError:
            return
        except OSError as e:
            _log.warning("清理打开目录失败 %s: %s", directory, e)
            return
        try:
            for name in names:
                if "/" in name or name.startswith("."):
                    continue
                try:
                    os.unlink(name, dir_fd=dfd)
                except FileNotFoundError:
                    continue
                except OSError as e:
                    _log.warning("清理跳过 %s: %s", name, e)
        finally:
            os.close(dfd)
