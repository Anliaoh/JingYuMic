// 鲸屿Mic PC 客户端 —— Electron 主进程
// 启动后自动扫描局域网内的平板服务(58090)，加载 QQ 音乐风格界面；
// 找不到时显示连接页，可手动填 IP。
const { app, BrowserWindow, ipcMain, Tray, Menu, nativeImage } = require("electron");
const { execFile } = require("child_process");
const fs = require("fs");
const path = require("path");
const os = require("os");
const net = require("net");

const PORT = 58090;
app.disableHardwareAcceleration(); // 远程桌面/截屏环境更稳
app.commandLine.appendSwitch("in-process-gpu"); // 规避个别显卡驱动下 GPU 子进程崩溃
app.commandLine.appendSwitch("disable-gpu-compositing");
/* v4.23c：Chromium 默认禁止「无用户手势的自动播放」。pc.html 在切歌 / 自动连播 /
   跟随音箱切歌这些路径上都是直接 audio.play()，没有点击手势，于是被拒 →
   抛 NotAllowedError: play() failed because the user doesn't have permission…
   桌面音乐播放器本来就该能自己起播，这里整进程放开（只影响本客户端，
   不改变任何浏览器行为）。 */
app.commandLine.appendSwitch("autoplay-policy", "no-user-gesture-required");
if (process.env.JY_DEBUG) app.commandLine.appendSwitch("remote-debugging-port", "9333");
// 任务栏图标/分组用固定 ID，避免显示成 Electron 默认图标
app.setAppUserModelId("com.jingyu.mic.pc");

let win = null;
let tray = null;
let isQuitting = false;   // 只有从托盘菜单「退出」才真的结束进程
let booted = false;       // 是否已提示过「收进托盘」
// 「关闭窗口 = 最小化到托盘」的开关（设置里可关；关了以后点关闭就退出）
let closeToTray = loadCfg().trayMin !== false;

function cfgFile() {
  return path.join(app.getPath("userData"), "config.json");
}
function loadCfg() {
  try {
    return JSON.parse(fs.readFileSync(cfgFile(), "utf8")) || {};
  } catch (e) {
    return {};
  }
}
function saveCfg(patch) {
  try {
    const c = Object.assign(loadCfg(), patch);
    fs.mkdirSync(path.dirname(cfgFile()), { recursive: true });
    fs.writeFileSync(cfgFile(), JSON.stringify(c));
  } catch (e) {}
}
function savedAddr() {
  return loadCfg().addr || null;
}
function saveAddr(ip) {
  saveCfg({ addr: ip });
}

/* ---------------- 自定义图标（窗口/任务栏 + 托盘 + 桌面快捷方式） ----------------
   App 端有「桌面图标」功能（AppBridge.setIcon），PC 端等价物：
   ① 窗口/任务栏 win.setIcon ② 托盘 tray.setImage ③ 桌面快捷方式 .lnk 的 IconLocation */
function customIconPng() { return path.join(app.getPath("userData"), "custom_icon.png"); }
function customIconIco() { return path.join(app.getPath("userData"), "custom_icon.ico"); }
function defaultIcoPath() {
  const guess = path.join(process.resourcesPath, "..", "icon.ico");   // JingYuMic-PC\icon.ico
  try { if (fs.existsSync(guess)) return guess; } catch (e) {}
  return path.join(__dirname, "icon.ico");
}
// 单枚 PNG 直接封装成 ICO（Vista+ 支持 PNG 压缩条目）；尺寸取自 IHDR，≥256 记 0（=256）
function pngToIco(buf) {
  let w = 0, h = 0;
  try {
    if (buf.length > 24 && buf.readUInt32BE(0) === 0x89504e47) {
      w = buf.readUInt32BE(16); h = buf.readUInt32BE(20);
    }
  } catch (e) {}
  const head = Buffer.alloc(6);
  head.writeUInt16LE(0, 0); head.writeUInt16LE(1, 2); head.writeUInt16LE(1, 4);
  const dir = Buffer.alloc(16);
  dir[0] = w >= 256 ? 0 : w; dir[1] = h >= 256 ? 0 : h; dir[2] = 0; dir[3] = 0;
  dir.writeUInt16LE(1, 4); dir.writeUInt16LE(32, 6);
  dir.writeUInt32LE(buf.length, 8); dir.writeUInt32LE(22, 12);
  return Buffer.concat([head, dir, buf]);
}
function findLnk() {
  const dirs = [];
  try { dirs.push(app.getPath("desktop")); } catch (e) {}
  dirs.push(path.join(os.homedir(), "Desktop"), path.join(os.homedir(), "OneDrive", "Desktop"));
  for (const d of dirs) {
    const p = path.join(d, "鲸屿Mic.lnk");
    try { if (fs.existsSync(p)) return p; } catch (e) {}
  }
  return null;
}
function setLnkIcon(icoPath) {
  const lnk = findLnk();
  if (!lnk) { console.log("[jy] desktop lnk not found"); return; }
  const target = icoPath || defaultIcoPath();
  // 桌面快捷方式的图标改走 WScript.Shell：一句 PowerShell，不落地脚本文件
  execFile("powershell.exe", ["-NoProfile", "-Command",
    "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('" + lnk + "');$s.IconLocation='" + target + ",0';$s.Save()"],
    (err) => {
      if (err) console.log("[jy] set lnk icon failed:", err.message);
      else console.log("[jy] lnk icon ->", target);
    });
}
function applyAppIcon() {
  const p = customIconPng();
  if (!fs.existsSync(p)) return;
  const img = nativeImage.createFromPath(p);
  if (img.isEmpty()) return;
  try { if (win && !win.isDestroyed()) win.setIcon(img); } catch (e) {}
  try { if (tray && !tray.isDestroyed()) tray.setImage(img); } catch (e) {}
}
function resetAppIcon() {
  try { fs.unlinkSync(customIconPng()); } catch (e) {}
  try { fs.unlinkSync(customIconIco()); } catch (e) {}
  const img = nativeImage.createFromPath(defaultIcoPath());
  if (!img.isEmpty()) {
    try { if (win && !win.isDestroyed()) win.setIcon(img); } catch (e) {}
    try { if (tray && !tray.isDestroyed()) tray.setImage(img); } catch (e) {}
  }
  setLnkIcon(null);
}

function localIP() {
  // 用 UDP connect 取默认路由出口 IP（不发包），比网卡遍历更准
  const dgram = require("dgram");
  const s = dgram.createSocket("udp4");
  return new Promise((resolve) => {
    const done = (v) => {
      try { s.close(); } catch (e) {}
      resolve(v);
    };
    s.on("error", () => done(null));
    try {
      s.connect(80, "223.5.5.5", () => done(s.address().address));
    } catch (e) {
      done(null);
    }
  });
}

function probe(ip) {
  return new Promise((resolve) => {
    const s = net.createConnection({ host: ip, port: PORT });
    const to = setTimeout(() => {
      s.destroy();
      resolve(false);
    }, 600);
    s.on("connect", () => {
      clearTimeout(to);
      s.destroy();
      resolve(true);
    });
    s.on("error", () => {
      clearTimeout(to);
      resolve(false);
    });
  });
}

async function discover() {
  const ip = await localIP();
  console.log("[jy] localIP =", ip);
  if (!ip) return [];
  const prefix = ip.split(".").slice(0, 3).join(".");
  const hits = [];
  let jobs = [];
  for (let i = 1; i < 255; i++) jobs.push(prefix + "." + i);
  // 分批并发
  const BATCH = 64;
  for (let j = 0; j < jobs.length; j += BATCH) {
    const rs = await Promise.all(jobs.slice(j, j + BATCH).map(probe));
    rs.forEach((ok, idx) => {
      if (ok) hits.push(jobs[j + idx]);
    });
  }
  return hits;
}

const LANDING_FILE = path.join(__dirname, "connect.html");

let curAddr = null;   // 当前已连接的平板 IP

function tabletURL(ip) {
  // v35：加版本参数穿透缓存（每次改 pc.html 必须 +1，否则客户端一直跑旧缓存）
  // cv= 携带 package.json 版本号，供「设置-关于」显示客户端当前版本
  // v3.96：129→131。130 那次只改了包内 main.js、没同步到安装目录，用户端一直是 129，
  //   而 ?v=129 这一份早在 22:21 那次 pc.html 部署**之前**就被 Electron 缓存住了，
  //   于是后面所有网页端修复在用户 PC 上都看不见（v3.94 已经栽过一模一样的一次）。
  // v4.13：156→157。pc.html 加了自动推进的 pre-flight（发「下一首」之前先问设备在放什么）
  //   和歌词失败重试，必须 bump，否则用户 PC 还在跑缓存里那份旧页。
  // v4.41：185→186。pc.html 的「下一首/上一首」在随机模式下改成跟 App 同一套语义
  //   （随机取一首、且不取当前这首），js 变了就必须 bump，否则用户端跑的还是缓存里那份。
  // R6 前端配套：186→187。pc.html 新增 awaitStart 冻结态（点歌后进度条/歌词等设备
  //   真出声才起跑），同样是 js 变了，必须 bump。
  // 语音关闭复播修复：189→190。remote/pc 两端补「设备停+软件标志也停=外部显式停止」分支，
  //   js 变了，必须 bump（PC 端 clearCache 已兜底，但按铁律同步戳）。
  return "http://" + ip + ":" + PORT + "/static/pc.html?v=194&cv=" + encodeURIComponent(app.getVersion());
}

/* v3.96：加载平板页面之前先清掉 HTTP 缓存。
   只靠手工 bump `?v=` 已经翻车两次了 —— v3.94 一次（?v=125 没跟着 pc.html 涨），
   这一次是 130 只改了包内 main.js、没同步到安装目录，用户端一直在跑 ?v=129，
   而 129 那一份早在 22:21 那次 pc.html 部署**之前**就被 Electron 缓存钉住了，
   于是之后所有的网页端修复在用户 PC 上都像没做一样。
   清缓存是幂等操作，页面在局域网内几百 KB，代价可以忽略。 */
function loadTablet(ip) {
  if (!win || win.isDestroyed()) return;
  var url = tabletURL(ip);
  try {
    var p = win.webContents.session.clearCache();
    if (p && typeof p.then === "function") {
      p["catch"](function () {}).then(function () {
        if (win && !win.isDestroyed()) win.loadURL(url);
      });
      return;
    }
  } catch (e) {}
  win.loadURL(url);
}

function createWindow() {
  win = new BrowserWindow({
    width: 1240,
    height: 820,
    minWidth: 960,
    minHeight: 620,
    title: "鲸屿Mic",
    backgroundColor: "#0D1017",
    autoHideMenuBar: true,
    frame: false,          // QQ 音乐式无边框：窗口按钮嵌在页面右上角，拖动靠 -webkit-app-region
    icon: path.join(__dirname, "icon.ico"),
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
      sandbox: false,
      spellcheck: false,
      /* v4.22（JY-桌面歌词同步）：主窗口最小化 / 被遮挡时，Chromium 默认会把定时器
         节流到很慢（backgroundThrottling 默认 true），页面里每 400ms 的
         dlyrPush / poll / gpPull 几乎停摆 —— 桌面歌词就卡在上一句，表现为
         「有时会不同步」（其实只在主窗口不可见时发生）。音乐播放器本来就要常驻
         后台跑同步，这里关掉节流。 */
      backgroundThrottling: false,
    },
  });

  // 无边框后页面自己接管窗口按钮 / 最大化状态
  const sendMaxed = () => {
    try { if (win && !win.isDestroyed()) win.webContents.send("win-maxed", win.isMaximized()); } catch (e) {}
  };
  win.on("maximize", sendMaxed);
  win.on("unmaximize", sendMaxed);

  // 网页自己的 <title> 会覆盖窗口标题，这里统一钉成应用名
  win.on("page-title-updated", (e) => {
    e.preventDefault();
    win.setTitle("鲸屿Mic");
  });

  // 关闭窗口：开关开着 = 收进托盘继续后台运行；关了 = 直接退出
  win.on("close", (e) => {
    if (isQuitting) return;
    if (!closeToTray) {
      isQuitting = true;
      app.quit();
      return;
    }
    e.preventDefault();
    win.hide();
    try { win.setSkipTaskbar(true); } catch (err) {}
    if (!booted) {
      booted = true;
      try {
        tray.displayBalloon({
          title: "鲸屿Mic 仍在运行",
          content: "已最小化到右下角托盘。点托盘图标可重新打开，右键托盘可退出。",
        });
      } catch (err) {}
    }
  });

  // 主窗口没了：桌面歌词小窗一起收掉
  win.on("closed", () => {
    try { if (dlyrWin) dlyrWin.destroy(); } catch (err) {}
    dlyrWin = null;
    win = null;
  });

  // 连接失败（平板关机/IP变了）→ 回到连接页
  win.webContents.on("did-fail-load", (e, code, desc, url, isMain) => {
    if (isMain && !url.startsWith("data:") && !url.startsWith("file:")) {
      showLanding("平板连接已断开，请重新连接");
    }
  });

  (async () => {
    const saved = savedAddr();
    if (saved && (await probe(saved))) {
      console.log("[jy] connect saved:", saved);
      curAddr = saved;
      loadTablet(saved);
    } else {
      showLanding();
      // 后台扫描，找到自动连
      const hits = await discover();
      console.log("[jy] discover hits:", hits);
      if (hits.length && win && !win.isDestroyed()) {
        curAddr = hits[0];
        win.webContents.send("found", hits[0]);
      }
    }
  })();
}

function showLanding(msg) {
  win.loadFile(LANDING_FILE).then(() => {
    if (msg) win.webContents.executeJavaScript(
      'st(' + JSON.stringify('<span class="bad">' + msg + "</span>") + ')'
    );
  });
}

/* ---------------- 桌面歌词（置顶小窗） ---------------- */
let dlyrWin = null;
let dlyrDrag = null, dlyrDragTimer = null;   // 手动拖窗（见 dlyr-drag-begin）
const trayState = { name: "", playing: false, dlyrOn: false, liked: false, mode: "", vol: null };   // 托盘/面板要显示的状态

/* 托盘弹出面板（左键点托盘图标）：圆环三键 + ♡歌名 + 音量条，布局照 QQ 音乐 */
let trayPanel = null;
function pushTrayState() {
  try { if (trayPanel && !trayPanel.isDestroyed()) trayPanel.webContents.send("tray-state", {
    name: trayState.name, playing: trayState.playing, liked: trayState.liked,
    mode: trayState.mode, vol: trayState.vol, dlyrOn: trayState.dlyrOn,
  }); } catch (e) {}
}
function createTrayPanel() {
  const p = new BrowserWindow({
    width: 300, height: 334,
    frame: false, transparent: true, hasShadow: false,
    alwaysOnTop: true, skipTaskbar: true, resizable: false,
    minimizable: false, maximizable: false, fullscreenable: false, show: false,
    webPreferences: { nodeIntegration: true, contextIsolation: false, sandbox: false, spellcheck: false },
  });
  try { p.setAlwaysOnTop(true, "screen-saver"); } catch (e) {}
  p.loadFile(path.join(__dirname, "tray.html"));
  p.on("blur", () => { try { if (trayPanel && !trayPanel.isDestroyed()) trayPanel.hide(); } catch (e) {} });
  p.on("closed", () => { trayPanel = null; });
  return p;
}
function toggleTrayPanel() {
  if (trayPanel && !trayPanel.isDestroyed() && trayPanel.isVisible()) { trayPanel.hide(); return; }
  if (!tray || tray.isDestroyed()) return;
  if (!trayPanel || trayPanel.isDestroyed()) trayPanel = createTrayPanel();
  // 贴着托盘图标弹出：水平居中于图标，托盘在下方就弹在上面，反之亦然
  const tb = tray.getBounds();
  const { screen } = require("electron");
  const wa = screen.getDisplayMatching(tb).workArea;
  const W = 300, H = 334;
  let x = Math.round(tb.x + tb.width / 2 - W / 2);
  let y = tb.y > wa.y + wa.height / 2 ? Math.round(tb.y - H - 10) : Math.round(tb.y + tb.height + 10);
  x = Math.max(wa.x + 6, Math.min(x, wa.x + wa.width - W - 6));
  y = Math.max(wa.y + 6, Math.min(y, wa.y + wa.height - H - 6));
  trayPanel.setPosition(x, y, false);
  trayPanel.show();
  trayPanel.focus();
  pushTrayState();
}

function notifyDlyrState() {
  try { if (win && !win.isDestroyed()) win.webContents.send("dlyr-state", trayState.dlyrOn); } catch (e) {}
}

function toggleDlyr(force) {
  const target = typeof force === "boolean" ? force : !trayState.dlyrOn;
  if (target) {
    if (dlyrWin && !dlyrWin.isDestroyed()) {
      dlyrWin.show();
      try { dlyrWin.moveTop(); } catch (e) {}     // 被别的置顶窗口压住时再顶上来
      trayState.dlyrOn = true; rebuildTray(); notifyDlyrState(); return;
    }
    const { screen } = require("electron");
    const wa = screen.getPrimaryDisplay().workArea;
    const W = Math.min(Math.round(wa.width * 0.62), 900), H = 120;
    let x = wa.x + Math.round((wa.width - W) / 2), y = wa.y + wa.height - H - 48;
    const pos = loadCfg().dlyr;
    if (pos && typeof pos.x === "number" && typeof pos.y === "number") { x = pos.x; y = pos.y; }
    /* 记住的位置要夹回「现在还在的那块屏」。
       以前拔掉副屏 / 改了分辨率后，窗口按旧坐标开在屏幕外 —— 点了「词」看不见东西，
       表现就是「点了没反应」。顺便留 24px 让它至少露一角，能拖回来。 */
    try {
      const dm = screen.getDisplayMatching({ x, y, width: W, height: H }).workArea;
      const m = 24;
      x = Math.max(dm.x - m, Math.min(x, dm.x + dm.width - W + m));
      y = Math.max(dm.y - m, Math.min(y, dm.y + dm.height - H + m));
    } catch (e) {}
    dlyrWin = new BrowserWindow({
      x, y, width: W, height: H,
      frame: false, transparent: true, hasShadow: false,
      alwaysOnTop: true, skipTaskbar: true, resizable: false,
      minimizable: false, maximizable: false, fullscreenable: false,
      webPreferences: { nodeIntegration: true, contextIsolation: false, sandbox: false, spellcheck: false },
    });
    try { dlyrWin.setAlwaysOnTop(true, "screen-saver"); } catch (e) {}
    /* 默认整窗鼠标穿透：这一层透明区域不许妨碍下面任何程序的点击。
       鼠标压到歌词/✕ 上时，页面会发 dlyr-hit 把它临时收回来（见下面的 ipc）。 */
    try { dlyrWin.setIgnoreMouseEvents(true, { forward: true }); } catch (e) {}
    dlyrWin.loadFile(path.join(__dirname, "dlyr.html"));
    // 拖完记住位置，下次还在原地
    let moveT = null;
    const saveMove = () => {
      clearTimeout(moveT);
      moveT = setTimeout(() => {
        try { if (dlyrWin && !dlyrWin.isDestroyed()) { const [mx, my] = dlyrWin.getPosition(); saveCfg({ dlyr: { x: mx, y: my } }); } } catch (e) {}
      }, 400);
    };
    dlyrWin.on("move", saveMove);
    dlyrWin.on("closed", () => {
      dlyrWin = null;
      dlyrDrag = null;
      if (dlyrDragTimer) { clearInterval(dlyrDragTimer); dlyrDragTimer = null; }
      if (trayState.dlyrOn) { trayState.dlyrOn = false; rebuildTray(); notifyDlyrState(); }
      pushTrayState();
    });
    trayState.dlyrOn = true;
  } else {
    try { if (dlyrWin && !dlyrWin.isDestroyed()) dlyrWin.destroy(); } catch (e) {}
    dlyrWin = null;
    trayState.dlyrOn = false;
  }
  rebuildTray();
  notifyDlyrState();
  pushTrayState();          // 托盘面板菜单里的「桌面歌词」开关字样跟着换
}

/* ---------------- 托盘（左键开关主窗口；右键弹 QQ 音乐式控制面板） ---------------- */
function showWin() {
  if (!win || win.isDestroyed()) {
    createWindow();
    return;
  }
  try { win.setSkipTaskbar(false); } catch (e) {}
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function rebuildTray() {
  // 原生右键菜单已停用：右键改弹 tray.html 控制面板（QQ 音乐式）。
  // 保留函数本身——toggleDlyr / tray-now 等处还在调它，做成空操作即可。
}

function createTray() {
  let img = nativeImage.createFromPath(path.join(__dirname, "tray.png"));
  if (img.isEmpty()) img = nativeImage.createFromPath(path.join(__dirname, "icon.ico"));
  tray = new Tray(img);
  tray.setToolTip("鲸屿Mic");
  rebuildTray();
  // 左键单击/双击：开/关主界面（恢复原行为）
  tray.on("click", () => {
    if (win && !win.isDestroyed() && win.isVisible() && !win.isMinimized()) {
      win.hide();
      try { win.setSkipTaskbar(true); } catch (e) {}
    } else {
      showWin();
    }
  });
  // 右键：弹/收 QQ 音乐式控制面板
  tray.on("right-click", () => toggleTrayPanel());
  tray.on("double-click", showWin);
}

// 只允许开一个实例：再次双击图标就是把已有窗口叫出来
if (!app.requestSingleInstanceLock()) {
  app.quit();
  process.exit(0);
}
app.on("second-instance", () => showWin());

app.whenReady().then(() => {
  ipcMain.handle("probe", async (e, ip) => probe(ip));
  ipcMain.handle("discover", async () => {
    const hits = await discover();
    return hits[0] || null;
  });
  ipcMain.on("connect", (e, ip) => {
    saveAddr(ip);
    curAddr = ip;
    loadTablet(ip);
  });

  // 页面右上角的窗口控制按钮（无边框窗口）
  ipcMain.on("win-min", () => { try { if (win && !win.isDestroyed()) win.minimize(); } catch (e) {} });
  ipcMain.on("win-max", () => {
    try {
      if (!win || win.isDestroyed()) return;
      if (win.isMaximized()) win.unmaximize(); else win.maximize();
    } catch (e) {}
  });
  ipcMain.on("win-close", () => { try { if (win && !win.isDestroyed()) win.close(); } catch (e) {} });
  ipcMain.handle("win-max-query", () => (win && !win.isDestroyed() ? win.isMaximized() : false));

  // 桌面歌词：页面开/关小窗、把当前句推进小窗
  ipcMain.on("dlyr-toggle", () => toggleDlyr());
  ipcMain.on("dlyr-query", (e) => { try { e.sender.send("dlyr-state", trayState.dlyrOn); } catch (err) {} });
  ipcMain.on("dlyr-close", () => toggleDlyr(false));
  /* 桌面歌词窗口的「鼠标进出热区」：空白区穿透，压到歌词/✕ 上才接事件。
     只认歌词窗口自己发来的，主页面发什么都不理。 */
  ipcMain.on("dlyr-hit", (e, hit) => {
    try {
      if (!dlyrWin || dlyrWin.isDestroyed()) return;
      if (e.sender !== dlyrWin.webContents) return;
      dlyrWin.setIgnoreMouseEvents(!hit, { forward: true });
    } catch (err) {}
  });
  /* 拖窗不用 -webkit-app-region：那种拖动区会让页面收不到 mousemove，
     指针停在字上时热区判定卡住、穿透状态退不回来，又开始挡鼠标。
     改成主进程按全局光标位置搬窗口 —— 拖到屏幕边、拖出窗口范围都跟得住。 */
  ipcMain.on("dlyr-drag-begin", () => {
    try {
      if (!dlyrWin || dlyrWin.isDestroyed()) return;
      const { screen } = require("electron");
      const p0 = screen.getCursorScreenPoint();
      const [wx, wy] = dlyrWin.getPosition();
      dlyrDrag = { p0, wx, wy };
      if (dlyrDragTimer) clearInterval(dlyrDragTimer);
      dlyrDragTimer = setInterval(() => {
        try {
          if (!dlyrDrag || !dlyrWin || dlyrWin.isDestroyed()) { clearInterval(dlyrDragTimer); dlyrDragTimer = null; return; }
          const p = screen.getCursorScreenPoint();
          dlyrWin.setPosition(dlyrDrag.wx + (p.x - dlyrDrag.p0.x), dlyrDrag.wy + (p.y - dlyrDrag.p0.y));
        } catch (err) { clearInterval(dlyrDragTimer); dlyrDragTimer = null; }
      }, 16);
    } catch (err) {}
  });
  ipcMain.on("dlyr-drag-end", () => {
    dlyrDrag = null;
    if (dlyrDragTimer) { clearInterval(dlyrDragTimer); dlyrDragTimer = null; }
  });
  /* 小窗加载完：让页面立刻重推一次当前句。
     否则刚开的那一下是空的（或上次残留的旧词），要等下一句才「活」过来 —— 看着像没反应。 */
  ipcMain.on("dlyr-ready", () => {
    try { if (win && !win.isDestroyed()) win.webContents.send("dlyr-state", trayState.dlyrOn); } catch (err) {}
  });
  ipcMain.on("dlyr-line", (e, p) => {
    try { if (dlyrWin && !dlyrWin.isDestroyed()) dlyrWin.webContents.send("dlyr-line", p || {}); } catch (err) {}
  });

  // 托盘状态同步：正在播放的歌 / 播放状态 / 喜欢 / 模式 / 音量（页面在变化时推过来）
  ipcMain.on("tray-now", (e, p) => {
    p = p || {};
    const next = {
      name: p.name || "",
      playing: !!p.playing,
      liked: !!p.liked,
      mode: p.mode || "",
      vol: typeof p.vol === "number" ? p.vol : trayState.vol,
    };
    const changed = JSON.stringify(next) !== JSON.stringify({
      name: trayState.name, playing: trayState.playing, liked: trayState.liked,
      mode: trayState.mode, vol: trayState.vol,
    });
    if (changed) {
      trayState.name = next.name;
      trayState.playing = next.playing;
      trayState.liked = next.liked;
      trayState.mode = next.mode;
      trayState.vol = next.vol;
      rebuildTray();
      pushTrayState();
    }
  });

  // 托盘面板：控制键转发给页面；菜单项本地处理
  ipcMain.on("panel-cmd", (e, c) => {
    if (c === "dlyr") { toggleDlyr(); return; }
    if (c === "quit") {
      isQuitting = true;
      try { if (dlyrWin) dlyrWin.destroy(); } catch (e2) {}
      try { if (tray) tray.destroy(); } catch (e2) {}
      try { if (trayPanel) trayPanel.destroy(); } catch (e2) {}
      app.quit();
      return;
    }
    if (c === "settings") {
      try { if (trayPanel && !trayPanel.isDestroyed()) trayPanel.hide(); } catch (e2) {}
      showWin();
      try { if (win && !win.isDestroyed()) win.webContents.send("tray-cmd", "settings"); } catch (e3) {}
      return;
    }
    if (c === "rescan") {
      (async () => {
        try { if (trayPanel && !trayPanel.isDestroyed()) trayPanel.hide(); } catch (e2) {}
        showWin();
        showLanding("正在重新扫描局域网…");
        const hits = await discover();
        if (hits.length) {
          saveAddr(hits[0]);
          curAddr = hits[0];
          loadTablet(hits[0]);
        } else if (win && !win.isDestroyed()) {
          showLanding("没找到平板服务，请手动填 IP");
        }
      })();
      return;
    }
    try { if (win && !win.isDestroyed()) win.webContents.send("tray-cmd", c); } catch (err) {}
  });
  ipcMain.on("tray-panel-ready", () => pushTrayState());

  // 「关闭窗口 = 最小化到托盘」的开关（设置里那个勾选框）
  ipcMain.on("tray-min-set", (e, v) => {
    closeToTray = v !== false;
    saveCfg({ trayMin: closeToTray });
  });

  // 换桌面图标：图片落 userData，窗口/托盘即时生效，桌面快捷方式改 IconLocation
  ipcMain.on("icon-set", (e, dataUrl) => {
    try {
      const m = /^data:image\/(png|jpeg|webp);base64,(.+)$/.exec(String(dataUrl || ""));
      if (!m) throw new Error("图片格式不支持");
      const buf = Buffer.from(m[2], "base64");
      if (!buf.length) throw new Error("图片是空的");
      fs.writeFileSync(customIconPng(), buf);
      fs.writeFileSync(customIconIco(), pngToIco(buf));
      applyAppIcon();
      setLnkIcon(customIconIco());
      try { e.sender.send("icon-done", true, "桌面图标已更换"); } catch (err) {}
    } catch (err) {
      try { e.sender.send("icon-done", false, "换图标失败：" + String(err.message || err)); } catch (e2) {}
    }
  });
  ipcMain.on("icon-reset", (e) => {
    resetAppIcon();
    try { e.sender.send("icon-done", true, "已恢复默认图标"); } catch (err) {}
  });

  // 小米账号登录窗口：滑块验证由用户完成，token 写回平板
  let loginWin = null, loginTimer = null;
  ipcMain.on("open-login", () => {
    if (!curAddr) { if (win) win.webContents.send("login-fail", "尚未连接平板"); return; }
    if (loginWin) { loginWin.focus(); return; }
    loginWin = new BrowserWindow({
      width: 1060, height: 760, title: "登录小米账号",
      autoHideMenuBar: true, backgroundColor: "#FFFFFF",
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
    });
    const ses = loginWin.webContents.session;
    // UA 伪装成移动端 Chrome（与平板 App 授权页一致，避免被 sts 拒绝）
    loginWin.webContents.setUserAgent(
      "Mozilla/5.0 (Linux; Android 13; M2102K1C) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
    );
    ses.clearStorageData().catch(() => {});
    loginWin.loadURL("https://account.xiaomi.com/fe/service/login?sid=micoapi&_banner=false");
    const started = Date.now();
    loginTimer = setInterval(async () => {
      try {
        if (!loginWin) { clearInterval(loginTimer); return; }
        if (Date.now() - started > 6 * 60 * 1000) { clearInterval(loginTimer); return; }
        const cks = await ses.cookies.get({ domain: "xiaomi.com" });
        const get = (n) => cks.find((c) => c.name === n);
        const pt = get("passToken"), uid = get("userId");
        if (pt && uid && pt.value && uid.value && uid.value !== "0") {
          clearInterval(loginTimer);
          const cu = get("cUserId");
          const body = {
            passToken: pt.value, userId: uid.value,
            cUserId: cu ? cu.value : "", deviceId: "",
          };
          try {
            const r = await fetch("http://" + curAddr + ":58091/api/save_login", {
              method: "POST", headers: { "Content-Type": "application/json" },
              body: JSON.stringify(body),
            });
            const d = await r.json();
            console.log("[jy] save_login resp:", JSON.stringify(d));
            if (win && !win.isDestroyed()) {
              win.webContents.send(d.ok ? "login-ok" : "login-fail", d.msg || "");
            }
          } catch (e) {
            console.log("[jy] save_login error:", e.message);
            if (win && !win.isDestroyed()) win.webContents.send("login-fail", "写入平板失败");
          }
          try { loginWin.close(); } catch (e) {}
        }
      } catch (e) { /* ignore */ }
    }, 900);
    loginWin.on("closed", () => { loginWin = null; clearInterval(loginTimer); });
  });

  createTray();
  createWindow();
  applyAppIcon();   // 上次换过自定义图标的话，启动就恢复（窗口 + 托盘）
});

// 关掉窗口不退出：进程缩在托盘里继续跑（window-all-closed 不 quit）
app.on("window-all-closed", () => {});
app.on("before-quit", () => { isQuitting = true; });
