#!/system/bin/sh
# 开机自启（Magisk service.d）—— 装到 /data/adb/service.d/99-jingyumic-autostart.sh
#
# 为什么不用 Termux:Boot：HyperOS / MIUI 一类的系统会拦掉 Termux:Boot 的
# BOOT_COMPLETED，重启后脚本根本不跑，而且不报错 —— 排查起来很费时间。
# Magisk 的 service.d 在 boot 完成后由 root 执行，绕开应用自启动权限。

TU_UID=10263      # Termux 的 uid：pm list packages -U | grep com.termux

(
  until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
  sleep 10

  AL=/data/data/com.termux/files/home/autostart.log

  # 等共享存储挂上，否则容器里 /sdcard 是空占位目录
  i=0
  while [ $i -lt 60 ]; do
    [ -d /storage/emulated/0/Music ] && break
    i=$((i+1)); sleep 2
  done

  # 等网络就绪。
  # 注意：不要用 `getprop dhcp.wlan0.ipaddress` 判断 —— 部分机型上它返回空字符串，
  # 会让这里白等满 60 次；`ip addr` 才是可靠的判据。
  i=0
  while [ $i -lt 45 ]; do
    ip addr show wlan0 2>/dev/null | grep -q "inet " && break
    i=$((i+1)); sleep 2
  done

  IP=$(ip addr show wlan0 2>/dev/null | awk '/inet /{print $2; exit}')
  echo "[autostart] $(date '+%F %T') boot ok, ip=$IP" >> "$AL"
  sh /data/adb/jingyumic/xm_launch.sh >> "$AL" 2>&1

  # 顺手把 sshd 拉起来（Termux 的 openssh），方便以后 adb 不在时也能进
  PX=/data/data/com.termux/files/usr
  pgrep -f sshd >/dev/null 2>&1 || $PX/bin/sshd >/dev/null 2>&1

  chown "$TU_UID:$TU_UID" "$AL" 2>/dev/null
) &
exit 0
