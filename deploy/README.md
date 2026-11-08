# 部署在安卓机上的两个真实坑（附可直接用的脚本）

如果你的服务器是一台**闲置安卓机**（这是本项目的推荐玩法），迟早会撞上下面两件事。
这两个脚本就是作者在自己那台机器上解决它们的方式，**不是必须**，但能省掉一晚上的排查。

| 现象 | 根因 | 本文对应 |
|---|---|---|
| 越用越卡，清后台杀应用完全没用，滑动都掉帧 | 内核不可回收内存随时间累积，不是应用占的 | [§1](#1-越用越卡内核内存泄漏不是应用问题) |
| 重启后 `:58090` 不监听，App 一直转圈；日志里 `FileExistsError` | Termux 的「所有文件访问权限」被拒 → 容器里音乐目录软链悬空 | [§2](#2-重启后服务起不来软链断在权限上) |
| 重启后服务干脆没启动 | 系统拦掉了 Termux:Boot 的 `BOOT_COMPLETED`，且不报错 | [§3](#3-开机自启别信-termuxboot) |

前提：机器已 root（Magisk）、xiaomusic 跑在 Termux 的 `proot-distro` 容器里。

---

## 1. 越用越卡：内核内存泄漏，不是应用问题

在一台连续开机 **14.4 天** 的小米平板 5 上实测：

| 指标 | 实测 | 正常参考 |
|---|---|---|
| 内核不可回收内存 `SUnreclaim` | **3.13~3.28 GB** | < 100 MB |
| 可用内存 `MemAvailable` | 244~285 MB | 1.5 GB+ |
| 系统负载（8 核） | **14~25** | < 6 |
| `kswapd0` CPU | 持续 80~96% | ≈ 0 |

应用侧 RSS 加起来只有约 1.5 GB，剩下 3 GB 全在内核里、连系统自己都回收不掉。
于是系统只能靠 ZRAM 压缩换页硬撑，`kswapd0` 和 `kcompressd0` 常年满载。

**这解释了为什么清后台毫无作用** —— 占用方在内核层，杀应用碰不到它。
**唯一的解法是重启。** 按开机时长反推泄漏速率约 **230 MB/天**，
所以这类机器**每 3~5 天重启一次**比较合适，别一路撑到半个月。

查自己的机器：

```bash
adb shell "grep -E 'MemAvailable|SUnreclaim' /proc/meminfo; cat /proc/loadavg"
```

`SUnreclaim` 是三位数 MB 以上、`load` 长期高于核数，就是它。

---

## 2. 重启后服务起不来：软链断在权限上

典型日志（`xiaomusic_run.log`）：

```
FileExistsError: [Errno 17] File exists: '/root/xiaomusic_music'
```

链条是三段：

1. Termux 的「所有文件访问权限」（`MANAGE_EXTERNAL_STORAGE`）被拒 → `~/storage` 消失；
2. 容器里 `/root/<music_dir>` 是指向 `/sdcard/Music` 的**软链**，而 `/sdcard` 只剩容器内一个
   `d---------` 的空占位目录 → 链接悬空；
3. `os.path.exists()` 对悬空软链返回 `False` → 走 `os.makedirs()` → 撞上同名的软链 → 崩。

它靠旧进程可以硬撑很久，**一直"看起来正常"，第一次重启才暴露**。

恢复只需一条（root）：

```bash
adb shell "su -c 'cmd appops set com.termux MANAGE_EXTERNAL_STORAGE allow'"
```

### 为什么启动脚本必须以 root 跑

容器要 bind 共享存储，就要求能读 `/storage/emulated/0`。实测只有一种身份通：

| 尝试 | 结果 |
|---|---|
| `su <termux用户> -c ...` | SELinux 域变 `magisk` → `Permission denied` |
| `su -Z <termux域> <uid> -c ...` | 无法 exec shell |
| `nsenter -t <termux pid> -m --` | 只借到 mount ns，域还是 `magisk`，照样拒 |
| **root 直接跑** | ✅ |

不用担心 root 写入会改坏曲库属主：曲库文件本来就是 `root:everybody`，root 写入后属主不变。

---

## 3. 开机自启：别信 Termux:Boot

HyperOS / MIUI 一类系统会拦掉 `Termux:Boot` 的 `BOOT_COMPLETED`，
**重启后脚本根本不执行，也不报错** —— 很容易误判成"脚本写错了"。

改用 Magisk 的 `service.d`：boot 完成后由 root 执行，不依赖应用自启动权限。

---

## 装

```bash
# 1) 启动器
adb push xm_launch.sh /data/local/tmp/
adb shell "su -c 'mkdir -p /data/adb/jingyumic'"
adb shell "su -c 'cp /data/local/tmp/xm_launch.sh /data/adb/jingyumic/; chmod 755 /data/adb/jingyumic/xm_launch.sh'"

# 2) 开机自启（装完记得改脚本里的 TU_UID）
adb push 99-autostart.sh /data/local/tmp/99-auto.sh
adb shell "su -c 'cp /data/local/tmp/99-auto.sh /data/adb/service.d/99-jingyumic-autostart.sh; chmod 755 /data/adb/service.d/99-jingyumic-autostart.sh'"

# 3) 先手动跑一遍，别等重启
adb shell "su -c 'sh /data/adb/jingyumic/xm_launch.sh'"
```

`xm_launch.sh` 顶部几个路径要按你的实际容器改：`XM_BIN` / `XM_CONF` / `SV_SRV` / `TU_UID`。
`TU_UID` 用 `adb shell "pm list packages -U | grep com.termux"` 查。

## 验证

```bash
adb shell "ps -eo args | grep -c '[p]ython3 .*xiaomusic'"          # 期望 1
curl -s "http://<平板IP>:58090/getplayerstatus?did=<音箱DID>"       # xiaomusic 活着
curl -s "http://<平板IP>:58091/api/health"                          # 本仓库服务活着
```

自启日志：`/data/data/com.termux/files/home/autostart.log`，
形如 `[autostart] boot ok, ip=...` 紧跟一行 `[xm_launch] ... instances=1`。

## 别踩的坑

- **不要用 `getprop dhcp.wlan0.ipaddress` 判网络就绪**：部分机型返回空字符串，会白等。
  用 `ip addr show wlan0 | grep "inet "`。
- **别在系统弹窗里拒绝 Termux 的文件权限**，否则 §2 会复发。
- **不要在 SSH 里跑启动脚本**：SSH 登录的也是 `magisk` 域，`su` 会被 Magisk 拒绝
  （默认只对 `adb shell` 放行），而且读不到 `/sdcard`。**走 adb。**

## 边界

这两个脚本只在**小米平板 5（HyperOS）+ Magisk + Termux/proot-distro(Debian)** 上验证过。
换设备时路径、uid、`stune` 组名都可能不一样 —— 脚本里每一步都有 `2>/dev/null` 兜底，
跑不通时先看 `autostart.log` 和容器里的日志，别急着改逻辑。
