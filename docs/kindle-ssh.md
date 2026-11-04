# SSH 连接 Kindle（越狱 + KOReader 之后）

越狱和 KOReader 都装好之后，下一步就是拿到一个 shell：用它部署、更新本仓库的
`octopus004.koplugin`，看日志、查网络。这里给三条路线，先看结论：

| 路线 | 需要额外安装 | 端口 | 能做什么 |
| --- | --- | --- | --- |
| A. KOReader 内置 SSH 服务 | 不用装，KOReader 自带 | 2222 | 传文件、跑命令、看日志（KOReader 运行时） |
| B. USB 直连 | 不用装 | 无 | 只能读写 U 盘里的文件：装插件、查日志、做自检 |
| C. USBNetwork 破解包 | 需要 KUAL/MRPI + usbnet | 22 或 USB 网卡 | 开机即有的 root shell，KOReader 没启动也能连 |

**先走 A**：不需要装任何东西，两分钟就能连上。C 只在需要 22 端口、KOReader 崩溃后
仍要登录、或要改系统文件（`mntroot rw`）时才用。**A 的菜单项找不到、或眼下只想把插件
装上**，用 B（USB 直连）最快，见后面同名一节。

本文依据 KOReader 源码里 `SSH.koplugin` 的行为和官方 wiki（`koreader/koreader/wiki/SSH`）
整理，尚未在本仓库的目标设备上逐条验证（与 README 的验证状态一致）。菜单文字以英文
界面为准，括号里是中文界面。

---

## 0. 先确认三件事

1. Kindle 和电脑在**同一个局域网**，Kindle 的 Wi-Fi 已连上。
2. 拿到设备 IP：
   - KOReader：菜单 → 网络（Network）→ 网络信息（Network info）；
   - 或在 Kindle 原生界面搜索框输入 `;711`，翻到第二页看 IP。
3. **不要先试 22 端口**。Kindle 的 iptables 只放行少数端口，KOReader 只为自己使用的
   2222 开了洞，所以 22 通常直接 connection refused（要 22 得走路线 C）。

---

## A. 用 KOReader 的 SSH 服务

### A1. 启动服务

1. 打开 KOReader，连上 Wi-Fi。
2. 入口**不在「工具」标签的第一层**，按版本在下面三处之一：
   - **设置（齿轮）→ 网络（Network）→ 最下面的 SSH 服务器**（官方 wiki 写的路径）；
   - **工具（扳手）→ 更多工具（More tools，第二页）→ SSH 服务器**；
   - 顶部菜单里点**放大镜**，输入 `ssh` 直接搜。
3. 进入子菜单把 **SSH server**（SSH 服务器）勾上。默认端口 **2222**。
4. 启动成功会弹窗显示 `SSH server started.`、端口和网络信息（含 IP）。错过了也没关系：
   同菜单里有 `SSH port: 2222` 和网络信息可查。

> 三处都找不到，见文末「菜单里找不到 SSH server」一节，先别急着装 usbnet。

> 服务只在 KOReader 运行时存在；退出 KOReader 服务即停止。想让 KOReader 一启动就开
> SSH，勾选子菜单里的 **Start SSH server with KOReader**。想随时看到状态，勾
> **Show SSH status in status bar**。

### A2. 选一种认证方式

子菜单里有两个互斥开关，**改动前必须先停掉服务**（运行中这两项是灰的）：

- **Login with key only (SECURE, public key required)** —— 推荐，只能用公钥。
- **Login without password (DANGEROUS)** —— 同网段任何人都能 root 登录，仅限临时排障。

公钥放哪里：菜单里点 **SSH public key** 会显示完整路径，实际是

```
/mnt/us/koreader/settings/SSH/authorized_keys
```

注意这是 KOReader 数据目录下的路径，**不是** `~/.ssh/authorized_keys`。文件要求：一行
一个公钥、Unix（LF）换行、没有扩展名。

最省事的做法（U 盘也行）：先用「无密码」登录一次，把公钥追加进去，再停服务、切成
「仅公钥」、重新启动。

```sh
# 电脑上：没有密钥就先建一个。KOReader 自带的是 dropbear 2018.76，不支持 ed25519，
# 用 ecdsa（或 rsa，但新版 OpenSSH 客户端要额外加 -o PubkeyAcceptedAlgorithms=+ssh-rsa）
ls ~/.ssh/kindle_ecdsa.pub || ssh-keygen -t ecdsa -b 256 -f ~/.ssh/kindle_ecdsa -C kindle

# 追加到设备（此时需要已勾选 Login without password）
cat ~/.ssh/kindle_ecdsa.pub | ssh -p 2222 root@192.168.1.23 \
  'mkdir -p /mnt/us/koreader/settings/SSH && cat >> /mnt/us/koreader/settings/SSH/authorized_keys'

# 之后连接要指定这把私钥
ssh -i ~/.ssh/kindle_ecdsa -p 2222 root@192.168.1.23
```

「登录无需密码」模式下 dropbear 用 `-n` 跳过密码校验，密码栏随便填（比如 `root`）或
直接回车；个别客户端拒绝空密码，就填 `root`。用户名始终是 `root`。

### A3. 登录

```sh
ssh -p 2222 root@192.168.1.23          # 换成 ;711 看到的 IP
```

- 第一次连接会问 `Are you sure you want to continue connecting?`，输 `yes`。
- **端口是 2222**，`-p` 写错会以为设备没开 SSH。
- 设备重刷或重装 KOReader 后 host key 会变，报 `REMOTE HOST IDENTIFICATION HAS CHANGED`
  时执行 `ssh-keygen -R '[192.168.1.23]:2222'` 再连。
- Windows 用 PowerShell 自带的 `ssh`/`scp`（Windows 10 1809+ 都有），图形界面用 WinSCP，
  协议选 SFTP/SCP、端口 2222、用户名 root。

进去之后可以确认一下环境：

```sh
uname -a
ls /mnt/us/koreader
ps | grep -i dropbear
```

> KOReader 打包了 OpenSSH 的 `sftp-server`，所以 SFTP 一般可用；万一 `sftp` 或
> OpenSSH 9+ 的 `scp` 连不上，就用 `scp -O` 或下面的 `tar | ssh` 方式，两者都不依赖
> sftp-server。

---

## A4. 部署 / 更新 octopus004 插件

插件必须放在设备的 `/mnt/us/koreader/plugins/` 下（整个文件夹，不是里面的文件）。

本仓库带了一个脚本，直接推过去：

```sh
cd /home/user/04-kindle
tools/deploy-plugin.sh 192.168.1.23                  # 默认端口 2222、用户 root
tools/deploy-plugin.sh -p 2222 -u root 192.168.1.23  # 显式指定
```

手工等价命令（不依赖 rsync，也不依赖设备上的 scp）：

```sh
cd /home/user/04-kindle
ssh -p 2222 root@192.168.1.23 'mkdir -p /mnt/us/koreader/plugins'
tar czf - octopus004.koplugin \
  | ssh -p 2222 root@192.168.1.23 'tar xzf - -C /mnt/us/koreader/plugins'
ssh -p 2222 root@192.168.1.23 'ls -l /mnt/us/koreader/plugins/octopus004.koplugin'
```

或者（OpenSSH 9 起 `scp` 默认走 SFTP，老设备上要加 `-O`）：

```sh
scp -O -P 2222 -r octopus004.koplugin root@192.168.1.23:/mnt/us/koreader/plugins/
```

覆盖插件**不会**清掉你的设置和缓存，它们不在插件目录里：

- 设置：`/mnt/us/koreader/settings/octopus004.lua`（报表 URL、启用状态）
- 缓存的上次成功报表：`/mnt/us/koreader/octopus004-report.txt`

部署完要**重启 KOReader**（菜单 → 退出，再从 KUAL 或书架重新打开）插件才会重新加载。
然后 工具（Tools）→ **004 Report** → 设置 Report URL、选 Show report。

### A5. 排错时看的几处

```sh
tail -n 50 /mnt/us/koreader/crash.log     # KOReader 崩溃/插件日志
ps | grep -Ei 'reader|dropbear'
ip addr show wlan0 | grep 'inet '         # 确认设备 IP
ls -l /mnt/us/koreader/settings/octopus004.lua
```

插件自身的 `logger` 输出也在 KOReader 日志里；屏幕上出现 `OFFLINE / CACHED` 时，先在
KOReader 里确认能打开 `https://k-macao.github.io/004/kindle.txt`（HTTPS、CA bundle 是否
存在），再看 `crash.log`。

---

## B. USB 直连：不开 SSH 也能自检和装插件

Kindle 插上电脑就是一个普通 U 盘（根目录即 `/mnt/us`），`koreader/` 全在里面。适合
「SSH 菜单项找不到」时先排查，或者干脆先把插件装上。

**先退出 KOReader 再插线**，否则设备可能只充电、不出盘。本仓库的脚本会自检并可选安装：

macOS / Linux（POSIX sh）：

```sh
cd /home/user/04-kindle
tools/kindle-usb.sh                 # 自动找 /Volumes、/media、/run/media、/mnt 下的 Kindle
tools/kindle-usb.sh /Volumes/Kindle # 或手动指定挂载点
tools/kindle-usb.sh --install       # 顺带把插件复制到 koreader/plugins
```

Windows（PowerShell，无需装 Git Bash）：

```powershell
cd 路径\04-kindle
powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1
powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1 -MountPoint D:\ -Install
powershell -ExecutionPolicy Bypass -File tools\kindle-usb.ps1 -OutFile kindle-report.txt
```

`-MountPoint` 不给就自动找盘；`-Install` 直接装插件；`-OutFile` 把输出另存为 UTF-8
文本，方便贴出来。脚本带 BOM，Windows PowerShell 5.1 下中文不会乱码。

它会检查这些，正好覆盖「菜单里没有 SSH 服务器」的两种原因：

- `koreader/dropbear`、`koreader/dropbearkey` 是否存在（缺失 → 插件静默禁用，菜单不出现）；
- `koreader/plugins/` 是否可写、已装了哪些 `*.koplugin`；
- `koreader/settings/SSH/authorized_keys`、`koreader/settings/octopus004.lua` 是否存在；
- 有 `koreader/crash.log` 就打印最后 15 行（**可能含书名和路径，贴出来前自己先看**）。

两个脚本都是只读检查 + 可选复制，不联网、不碰系统分区。PowerShell 版按 5.1 / 7 的语法写成，
目前还没有在真实 Windows 上跑过：报错就把整段输出贴出来。

不想跑脚本就是纯手工三件事（Windows 上对着盘符操作即可）：

1. 打开 U 盘里的 `koreader/`，看有没有无扩展名的 `dropbear` 文件 —— 有没有决定 SSH 菜单
   项出不出现；
2. 要装插件：把 `octopus004.koplugin` 整个文件夹复制到 U 盘的
   `koreader/plugins/` 下；
3. 安全弹出、拔线，在 Kindle 上重开 KOReader → 工具 → 004 Report。

USB 直连不能跑命令，所以要交互式 shell（看进程、ping、改配置）仍然得回到 A 或 C。

---

## C. 用 USBNetwork 拿完整 root shell（可选）

只有当 A / B 不够用时才做：需要 22 端口、KOReader 起不来时仍要登录、或要改只读根分区。

前置：KUAL + MRPI 已装（越狱的 hotfix 不等于 KUAL，需要单独装）。整个过程是往
`mrpackages` 放 `.bin`，然后在 KUAL → Helper → Install MR Packages 安装。

- **固件 < 5.16.3**：用 NiLuJe 的 USBNetwork 包（`kindle-usbnetwork-*.tar.xz`）。
- **固件 ≥ 5.16.3**：NiLuJe 版不工作，改用 **usbnetlite**（KUAL 扩展，默认密码 `kindle`）。

NiLuJe 版的关键点：

1. 安装后 Kindle 根目录出现 `usbnet/`；配置在 `usbnet/etc/config`。
2. 公钥放 `usbnet/etc/authorized_keys`（不是 `~/.ssh/`）。
3. 想让 SSH 在 Wi-Fi 上可用：`USE_WIFI="true"`；只想开 sshd、不改 USB 行为再加
   `USE_WIFI_SSHD_ONLY="true"`。想开机自启，把 `usbnet/DISABLED_auto` 改名成 `auto`。
4. USB 网卡模式：电脑网卡设 `192.168.15.201/24`，Kindle 是 `192.168.15.244`；
   老机型 root 密码是 `mario`，新固件基本已禁用密码，只能走公钥（或先用 telnet 免密进去
   追加公钥）。改密码要先 `mntroot rw` 让根分区可写。
5. 用完记得切回去，否则 USB 插上不再是 U 盘（USBMS）。

USB 网卡模式会替换掉「插上就是 U 盘」的行为，对日常拷书不方便；能用 A 就用 A。

---

## 菜单里找不到 SSH server

「工具」标签第一层通常**没有** SSH，先排除位置问题，再怀疑插件：

1. **换位置找**：设置（齿轮）→ 网络 → 最下面；工具（扳手）→ 更多工具（第二页）；
   或顶部菜单的放大镜里搜 `ssh`。
2. **插件被关掉了**：工具 → 更多工具 → **插件管理（Plugin management）** → 找到
   `SSH` / `SSH server`，勾选启用，然后重启 KOReader。被禁用的插件不会出现在菜单里。
3. **这个 KOReader 包确实没带 dropbear**：SSH 插件启动时会检查 KOReader 目录下有没有
   `dropbear` 可执行文件，没有就**静默禁用自己**（不报错、不留菜单项）。
   - 用 U 盘连电脑，看 `koreader/` 根目录里有没有无扩展名的 `dropbear`；
   - 或者用 KOReader 自带的终端：工具 → 更多工具 → 终端模拟器（Terminal），执行
     `ls -l /mnt/us/koreader/dropbear`。
4. **没有 dropbear 时**，二选一：
   - 换成官方最新 Kindle 包（当前 v2026.07.1）：固件 ≥5.16.3 用 `koreader-kindlehf`，
     更低（Oasis 1 多半属于这类）用 `koreader-kindlepw2`，整个覆盖到 Kindle 根目录；
   - 走后面的路线 C（USBNetwork / usbnetlite）。
5. 顺带一提：**不用 SSH 也能装插件**。把 `octopus004.koplugin` 文件夹拷到 Kindle 根目录
   （U 盘可见的 `/mnt/us`），在 KOReader 的终端模拟器里执行：

   ```sh
   mkdir -p /mnt/us/koreader/plugins
   cp -r /mnt/us/octopus004.koplugin /mnt/us/koreader/plugins/
   ```

   然后退出 KOReader 重开即可。终端在墨水屏上很慢，但够用；真正的交互式 shell 还是 SSH 舒服。

## 常见问题

| 现象 | 多半是 | 处理 |
| --- | --- | --- |
| `Connection refused` | 服务没开 / 端口写错 / 不在同一网段 | KOReader 里确认 SSH server 已勾选，端口 2222，先 `ping` 设备 |
| 一直 `Connection timed out` | IP 变了 / 设备休眠断了 Wi-Fi | `;711` 重新看 IP，唤醒设备后重试 |
| `Permission denied (publickey)` | 公钥没进 `authorized_keys`，或 CRLF 换行、权限不对 | 重写文件为 LF、一行一个；`chmod 600` 不必（dropbear 读的是 KOReader 目录） |
| 菜单里没有 SSH server | 位置不对（不在工具第一层）/ 插件管理里被禁用 / 包里没有 `dropbear` | 见上一节「菜单里找不到 SSH server」 |
| 公钥登录失败，密钥是 `id_ed25519` | KOReader 的 dropbear 2018.76 不支持 ed25519 | 改用 `ssh-keygen -t ecdsa -b 256` |
| `sftp` / OpenSSH 9+ 的 `scp` 失败 | 老 dropbear + 新客户端的协议差异 | 用 `scp -O`，或 `tar czf - … \| ssh … 'tar xzf - -C …'` |
| `Host key verification failed` | 设备重装过，本地 known_hosts 里还是旧 key | `ssh-keygen -R '[IP]:2222'` |
| 连上就断 / 命令卡住 | 设备睡眠或 Wi-Fi 省电 | 连接期间让设备保持唤醒，或关掉自动休眠再调试 |

## 安全提醒

- 只在可信任的局域网里开 SSH；不要在路由器上把 2222 端口映射到公网。
- 优先用「仅公钥」；「无密码登录」等于把 root 交给同网段所有人，排障完立刻关掉。
- 调试结束就把 SSH server 关掉（KOReader 退出时也会自动停）。
