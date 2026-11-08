#!/system/bin/sh
# 服务启动器（xiaomusic + 本仓库 server）—— 必须以 root 执行
#
# 为什么必须 root：
#   容器（proot）要把共享存储 bind 进 /sdcard，而 `su <termux用户>` 切过去后 SELinux 域
#   会变成 magisk 域，读不到 /storage/emulated/0，bind 静默失败 —— 结果就是 xiaomusic 的
#   music_path 软链悬空，启动时报 FileExistsError: '/root/<music_dir>'。
#   实测只有 root 能既不丢存储、又不改坏曲库属主（曲库本来就是 root:everybody）。
#
# 装载与验证见本目录 README.md。

H=/data/data/com.termux/files/home
PX=/data/data/com.termux/files/usr

# Termux 应用的 uid：adb shell "pm list packages -U | grep com.termux" 里的 uid: 后面那串
# 只用于把日志文件属主改回 Termux（否则 Termux 里看日志会没权限）。不改也能跑。
TU_UID=10263

DISTRO=debian
XM_BIN=/root/venv/bin/xiaomusic
XM_CONF=/root/xiaomusic_conf/config.json
SV_SRV=/root/jingyumic/server.py      # 本仓库 server/server.py 在容器里的路径（按你的实际改）

export HOME=$H
export PREFIX=$PX
export PATH=$PX/bin:$PATH
export LD_LIBRARY_PATH=$PX/lib
export TMPDIR=$PX/tmp
cd $H || exit 1

rotate() { [ -f "$1" ] && mv -f "$1" "$1.1"; }

pkill -f "[s]erver.py" 2>/dev/null
pkill -f "[x]iaomusic --config" 2>/dev/null
sleep 3

rotate "$H/server.log"
rotate "$H/xiaomusic_run.log"

# 先起聚合服务（58091），它自己会等 58090
if [ -f "$SV_SRV" ]; then
  setsid nohup $PX/bin/proot-distro login $DISTRO -- \
    bash -c "exec python3 $SV_SRV" > "$H/server.log" 2>&1 < /dev/null &
  sleep 5
fi

setsid nohup $PX/bin/proot-distro login $DISTRO -- \
  bash -c "exec $XM_BIN --config $XM_CONF" > "$H/xiaomusic_run.log" 2>&1 < /dev/null &
sleep 20

chown $TU_UID:$TU_UID "$H/server.log" "$H/xiaomusic_run.log" 2>/dev/null

# 降优先级：服务不该跟前台 UI 抢 CPU。没有 stune 的设备会静默跳过。
for p in $(ps -eo pid,args 2>/dev/null | grep -E "[p]ython3 /root/" | awk '{print $1}'); do
  echo "$p" > /dev/stune/background/tasks 2>/dev/null
  renice -n 10 "$p" >/dev/null 2>&1
done

n=$(ps -eo args 2>/dev/null | grep -c "[p]ython3 $XM_BIN")
echo "[xm_launch] $(date '+%F %T') xiaomusic instances=$n"
