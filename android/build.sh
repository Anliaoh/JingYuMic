#!/bin/bash
# 无 Gradle 直接构建 APK：aapt2 → javac → d8 → zipalign → apksigner
# 依赖：JDK 17、Android build-tools（含 aapt2/zipalign/apksigner）、platform android.jar
# 路径优先级：环境变量 > 本机 SDK（ANDROID_HOME / JAVA_HOME）> 仓库内 ../sdkbuild
set -e
cd "$(dirname "$0")"

SDK="${SDK:-../sdkbuild}"

# 仓库里不带 SDK（867MB，不进 OneDrive），本机别的目录下有一份就拿来用
if [ ! -d "$SDK/bt/android-14" ]; then
  for c in "$HOME"/WorkBuddy/*/sdkbuild "$HOME"/sdkbuild; do
    if [ -d "$c/bt/android-14" ]; then SDK="$c"; break; fi
  done
fi
if [ ! -d "$SDK/bt/android-14" ]; then
  echo "找不到 Android build-tools：设 SDK=<含 bt/android-14 的目录> 再跑，"
  echo "或放到仓库内 ../sdkbuild（bt/android-14 + plat/android-35 + jdk_tmp）"
  exit 1
fi

# ---- build-tools / android.jar ----
if [ -n "$BT_DIR" ]; then
  BT="$BT_DIR"
elif [ -n "$ANDROID_HOME" ] && [ -d "$ANDROID_HOME/build-tools" ]; then
  BT="$(ls -d "$ANDROID_HOME"/build-tools/* | sort -V | tail -1)"
else
  BT="$SDK/bt/android-14"
fi

if [ -n "$ANDROID_JAR" ]; then
  AJAR="$ANDROID_JAR"
elif [ -n "$ANDROID_HOME" ] && [ -f "$ANDROID_HOME/platforms/android-35/android.jar" ]; then
  AJAR="$ANDROID_HOME/platforms/android-35/android.jar"
else
  AJAR="$SDK/plat/android-35/android.jar"
fi

# ---- JDK ----
if [ -n "$JDK_HOME" ]; then
  JDK="$JDK_HOME/bin"
elif [ -n "$JAVA_HOME" ]; then
  JDK="$JAVA_HOME/bin"
else
  JDK="$SDK/jdk_tmp/jdk-17.0.20.1+1/bin"
fi

# Windows 下工具带 .exe 后缀
EXE=""
[ -f "$BT/aapt2.exe" ] && EXE=".exe"

# 合并 dex 用的小脚本：优先 python3，其次 python
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  if command -v python3 >/dev/null 2>&1; then PY=python3; else PY=python; fi
fi

OUT="build"
rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/gen" "$OUT/dex"

echo "==> 1/6 aapt2 compile"
"$BT/aapt2$EXE" compile --dir res -o "$OUT/res.zip"

echo "==> 2/6 aapt2 link"
"$BT/aapt2$EXE" link \
  -o "$OUT/base.apk" \
  -I "$AJAR" \
  --manifest AndroidManifest.xml \
  -R "$OUT/res.zip" \
  --java "$OUT/gen" \
  --min-sdk-version 26 \
  --target-sdk-version 30 \
  --version-code 56 \
  --version-name 3.26 \
  --auto-add-overlay

echo "==> 3/6 javac"
"$JDK/javac$EXE" -source 8 -target 8 -nowarn -encoding UTF-8 \
  -bootclasspath "$AJAR" -d "$OUT/classes" \
  src/com/xiaomusic/remote/*.java \
  "$OUT/gen/com/xiaomusic/remote/R.java"

echo "==> 4/6 d8 -> classes.dex"
"$JDK/jar$EXE" cf "$OUT/classes.jar" -C "$OUT/classes" .
"$JDK/java$EXE" -cp "$BT/lib/d8.jar" com.android.tools.r8.D8 \
  --release --min-api 26 --lib "$AJAR" \
  --output "$OUT/dex" \
  "$OUT/classes.jar"

echo "==> 5/6 打包 dex + zipalign"
cp "$OUT/base.apk" "$OUT/unsigned.apk"
"$PY" - <<'PY'
import zipfile, shutil
src = "build/unsigned.apk"
shutil.copyfile(src, src + ".tmp")
with zipfile.ZipFile(src + ".tmp", "a", zipfile.ZIP_DEFLATED) as z:
    z.write("build/dex/classes.dex", "classes.dex")
shutil.move(src + ".tmp", src)
print("dex merged")
PY
"$BT/zipalign$EXE" -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

echo "==> 6/6 apksigner"
if [ ! -f keystore.jks ]; then
  echo "    keystore.jks 不存在，自动生成（口令 android / alias xiaoremote）"
  "$JDK/keytool$EXE" -genkeypair -keystore keystore.jks \
    -storepass android -keypass android -alias xiaoremote \
    -keyalg RSA -keysize 2048 -validity 10000 \
    -dname "CN=XiaoRemote, O=XiaoMusic, C=CN" >/dev/null 2>&1
fi
"$JDK/java$EXE" -jar "$BT/lib/apksigner.jar" sign \
  --ks keystore.jks --ks-pass pass:android --key-pass pass:android \
  --ks-key-alias xiaoremote \
  --out ../xiaoremote.apk "$OUT/aligned.apk"

"$JDK/java$EXE" -jar "$BT/lib/apksigner.jar" verify --print-certs ../xiaoremote.apk | head -5
ls -la ../xiaoremote.apk
echo "BUILD OK"
