-- SPDX-License-Identifier: AGPL-3.0-or-later
-- Wake/scheduling patterns adapted from dmellok/tesserae-koreader (AGPL-3.0-or-later).
local Device = require("device")
local DataStorage = require("datastorage")
local LuaSettings = require("luasettings")
local UIManager = require("ui/uimanager")
local NetworkMgr = require("ui/network/manager")
local Font = require("ui/font")
local Blitbuffer = require("ffi/blitbuffer")
local Geom = require("ui/geometry")
local GestureRange = require("ui/gesturerange")
local InputContainer = require("ui/widget/container/inputcontainer")
local FrameContainer = require("ui/widget/container/framecontainer")
local VerticalGroup = require("ui/widget/verticalgroup")
local TextBoxWidget = require("ui/widget/textboxwidget")
local TextWidget = require("ui/widget/textwidget")
local ImageWidget = require("ui/widget/imagewidget")
local InfoMessage = require("ui/widget/infomessage")
local ButtonDialog = require("ui/widget/buttondialog")
local InputDialog = require("ui/widget/inputdialog")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local CenterContainer = require("ui/widget/container/centercontainer")
local logger = require("logger")
local lfs = require("libs/libkoreader-lfs")
local _ = require("gettext")
local dir = (debug.getinfo(1, "S").source:match("^@(.+)$") or ""):match("(.*/)" ) or "./"
local Wake = dofile(dir .. "wake.lua")
-- 地址映射与错误翻译：GitHub raw 连不上时自动改试 jsDelivr 之类的镜像。
local Net = dofile(dir .. "net.lua")
local Screen = Device.screen
local DEFAULT_URL = "https://k-macao.github.io/004/kindle.txt"
local PLUGIN_VERSION = "0.9.2"
local MAX_BYTES = 2 * 1024 * 1024
local MANIFEST_MAX = 256 * 1024
-- stocks.json 同时带 91 个板块与 A/H 龙头 26 周压缩量价，已不再是小清单；
-- 完整 130 根日线仍在 sidecar，不由 Kindle 每 7 分钟下载。
local STOCKS_MANIFEST_MAX = 2 * 1024 * 1024
local STOCKS_MANIFEST_TIMEOUT = 8
local PAGE_MAX = 4 * 1024 * 1024
local PAGES_TOTAL_MAX = 64 * 1024 * 1024
local MAX_PAGES = 60
local DOC_MAX = 8 * 1024 * 1024
local DOC_PREFIX = "octopus004-report-"
-- 每日新闻是第二本书：同一个下载/校验/翻页/清理流程，换一套设置键与前缀，
-- 两份文档互不覆盖（各自的 sig 变了才重下，各自的旧文件各自清）。
local NEWS_PREFIX = "octopus004-news-"
-- 卡片版 EPUB 的前缀必须和纯文本缓存（octopus004-stocks.txt）区分开：
-- pruneDocs 是按前缀删旧文件的，共用前缀会把文本回退版一起删掉。
local STOCKS_PREFIX = "octopus004-stocks-card-"
local DOC_KINDS = {
    doc  = { manifest = "doc_url",  sig = "doc_sig",  seq = "doc_seq",  file = "doc_file",
             file_name = "doc_file_name", opened_at = "doc_opened_at", prefix = DOC_PREFIX,
             data_time = "data_time", data_time_kind = "data_time_kind",
             label = "EPUB" },
    news = { manifest = "news_url", sig = "news_sig", seq = "news_seq", file = "news_file",
             file_name = "news_file_name", opened_at = "news_opened_at", prefix = NEWS_PREFIX,
             data_time = "news_data_time", data_time_kind = "news_data_time_kind",
             label = "NEWS" },
    -- 行情面板的 EPUB 卡片版与新闻/报告共用「下载书 → 交给阅读器」的流程；
    -- 清单里没有 epub 字段时回退到纯文本 stocks.txt（见 refreshStocks）。
    stocks = { manifest = "stocks_url", sig = "stocks_epub_sig", seq = "stocks_epub_seq",
               file = "stocks_epub_file", file_name = "stocks_epub_file_name",
               opened_at = "stocks_epub_opened_at", prefix = STOCKS_PREFIX,
               data_time = "stocks_data_time", data_time_kind = "stocks_data_time_kind",
               label = "STOCKS" },
}
-- 同花顺行情面板是第三种来源：清单 site/stocks.json（generated 做签名）+ 卡片版
-- site/stocks.epub（有就交给阅读器，见 refreshStocksEpub）+ 纯文本回退版
-- site/stocks.txt。每 INTERVAL 秒只拉一次清单，generated 变了才重下正文；
-- 文本缓存与报告/新闻的缓存文件分开，互不覆盖。
local STOCKS_CACHE = "octopus004-stocks.txt"
-- 检查间隔（秒）：7 分钟。服务端工作流最快 5 分钟一轮，60 秒一查大多是在
-- 重复拉同一条清单，白唤醒设备；7 分钟足够跟上服务端节奏，也更省电。
local INTERVAL = 420
-- 界面上显示的间隔（"7m" / "90s"）：INTERVAL 是秒，人读的是分钟。
local function intervalText()
    if INTERVAL % 60 == 0 then return string.format("%dm", INTERVAL / 60) end
    return string.format("%ds", INTERVAL)
end
-- stocks 清单会短超时探测多个镜像再择优，随后还可能下载 EPUB；90 秒会在有效的
-- 第二个正文镜像返回前误杀整轮。上限只放宽 watchdog，不改变每个请求自己的超时。
local WIFI_WAIT = 135
-- 设备上显示的时间统一为北京时间（UTC+8），与设备本地时区无关。
local BEIJING_OFFSET = 8 * 3600

local Report = WidgetContainer:extend{ name = "octopus004", is_doc_only = false }

-- 设置文件：自动开始 / 打开书籍的守卫都写在它里面，所以打开它之前要先能算出路径。
local function settingsPath()
    return DataStorage:getSettingsDir() .. "/octopus004.lua"
end

-- 进程级守卫：同一个 KOReader 进程里只自动 start() 一次。
-- showReader() 会把当前 ReaderUI 整个换掉、插件随之重新 init；如果只看设置里的
-- autostart_resume，init→start→openDoc→showReader→init 就会无限重开（实测死循环）。
-- 这个标记只在进程内存里，重启 KOReader 才会清空。
local session_started = false
local session_opened_doc = false
-- 设置文件里的 auto_opened_doc：上一次（乃至上一个进程）已经自动开过书。
-- init 时读出来，和进程内标记一起决定要不要再 showReader()。

-- 直接读写设置文件的小工具：init 阶段 self.store 还没建好，守卫要能独立用。
local function loadSettings()
    local ok, store = pcall(LuaSettings.open, LuaSettings, settingsPath())
    if not ok or not store then return nil end
    return store, store.data or {}
end

local function patchSettings(patch)
    local store, data = loadSettings()
    if not store then return false end
    for key, value in pairs(patch) do
        if value == nil then store:delSetting(key) else store:saveSetting(key, value) end
    end
    store:flush()
    return true
end

-- 自杀开关：设置文件里出现 \`emergency_stop = true\` 时插件什么都不做。
-- 设备上被自动开始/重开困住时，退出 KOReader、改这一个键、再启动就能彻底静下来。
local function emergencyStopped()
    local _, data = loadSettings()
    return data ~= nil and data.emergency_stop == true
end

-- 不要为了后台计时向 UIManager:show() 放一个“0x0 隐形窗口”：KOReader 只把触摸
-- 事件交给最上层窗口，尺寸为零也可能挡住整块屏幕。定时器直接挂 UIManager，换
-- ReaderUI 时由设置里的接力标记让新插件实例重建即可。

local function dataDir()
    return DataStorage:getDataDir()
end

-- Resolve a possibly relative page path against the manifest URL.
local function resolveURL(base, path)
    if path:match("^https://") then return path end
    if path:match("^//") then return "https:" .. path end
    if path:match("^/") then
        local origin = base:match("^(https://[^/]+)")
        return (origin or "") .. path
    end
    local folder = base:match("^(.*/)")
    return (folder or base) .. path
end

-- 时间换算必须定义在 Report:refresh 之前：Lua 的 local 只在声明点之后可见，
-- refresh 里 finish() 调用 beijing()，定义在后面就会被当成全局 nil 而闪退。
-- Epoch seconds for a UTC civil time (days-from-civil). Does not consult the
-- device timezone, so it is correct whatever the Kindle's local clock is set to.
local function utcEpoch(y, mo, d, h, mi, sec)
    y = (mo <= 2) and (y - 1) or y
    local era = math.floor(y / 400)
    local yoe = y - era * 400
    local mp = (mo + 9) % 12
    local doy = math.floor((153 * mp + 2) / 5) + d - 1
    local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
    local days = era * 146097 + doe - 719468
    return days * 86400 + h * 3600 + mi * 60 + sec
end

-- "2026-11-02T10:09:05Z" -> epoch seconds, or nil when unparsable.
local function isoSeconds(iso)
    local y, mo, d, h, mi, sec =
        tostring(iso):match("^(%d%d%d%d)-(%d%d)-(%d%d)T(%d%d):(%d%d):(%d%d)Z$")
    if not y then return nil end
    return utcEpoch(tonumber(y), tonumber(mo), tonumber(d),
                    tonumber(h), tonumber(mi), tonumber(sec))
end

-- Format an epoch as Beijing wall-clock time.
local function beijing(epoch, fmt)
    return os.date("!" .. fmt, epoch + BEIJING_OFFSET)
end

function Report:init()
    self.store = LuaSettings:open(settingsPath())
    self.settings = self.store.data or {}
    if self.settings.url == nil then self.settings.url = DEFAULT_URL end
    if self.settings.image_url == nil then self.settings.image_url = "" end
    if self.settings.display == nil then self.settings.display = "text" end
    if self.settings.doc_url == nil then self.settings.doc_url = "" end
    if self.settings.news_url == nil then self.settings.news_url = "" end
    if self.settings.stocks_url == nil then self.settings.stocks_url = "" end
    -- 自动阅读默认是「取消」的：装好插件后不会自己开始翻页，要手动 Show report。
    -- 想让它开机就自己跑，在菜单里打开 Auto-start on launch（见 addToMainMenu）。
    if self.settings.autostart == nil then
        -- 老版本只有 enabled（true = 正在跑）：跑着的话当作用户是要它自动跑的，
        -- 但这次启动仍然不自动开——新语义下「自动」必须显式打开。
        self.settings.autostart = self.settings.enabled == true
        self.settings.enabled = false
    end
    if self.settings.enabled == nil then self.settings.enabled = false end
    if self.settings.auto_opened_doc then session_opened_doc = true end
    -- ReaderUI 每次换书都会重新建立插件实例；检查时间/错误不能只存在旧实例内存里，
    -- 否则 Status 会误报 Last check: never / No error。
    self.last_check = tonumber(self.settings.last_check)
    local saved_error = tostring(self.settings.last_error or "")
    self.last_error = saved_error ~= "" and saved_error or nil
    self.wake = Wake.new({ Device = Device, UIManager = UIManager, logger = logger })
    self.refresh_task = function() self:refresh(false) end
    self.cache_path = dataDir() .. "/octopus004-report.txt"
    self.stocks_cache_path = dataDir() .. "/" .. STOCKS_CACHE
    self.image_dir = dataDir() .. "/octopus004-pages"
    self.page_index = 1
    self.page_count = tonumber(self.settings.image_count) or 0
    if self.ui and self.ui.menu then
        self.menu = self.ui.menu
        self.ui.menu:registerToMainMenu(self)
    end
    -- 紧急停止：设置文件里写了 emergency_stop = true 就什么都不做（菜单里可以再关掉）。
    if self.settings.emergency_stop then
        self.settings.enabled = false
        self.settings.autostart = false
        self.settings.autostart_resume = nil
        self:save()
        logger.warn("004: emergency_stop is set, the plugin stays idle")
        return
    end
    -- openDoc/showReader 或用户切换书籍会销毁旧 ReaderUI，也会顺手取消旧实例的
    -- 计时器。同一进程的新实例看到接力标记/session_started 后立即检查一次；这不是
    -- 再次开书，所以不会形成 init → openDoc → init 的循环。
    if self.settings.enabled
            and (self.settings.resume_after_reader_swap == true or session_started) then
        UIManager:scheduleIn(2, function()
            -- showReader 返回后，旧实例还会提交新 EPUB 的 sig/data_time。这里重新打开
            -- 设置文件，不能拿 init 时读到的旧表整份写回去覆盖那些新值。
            local latest_store, latest = loadSettings()
            if latest_store and latest then
                self.store, self.settings = latest_store, latest
            end
            if not self.settings.enabled then return end
            self.settings.resume_after_reader_swap = false
            patchSettings({ resume_after_reader_swap = false })
            self.last_check = tonumber(self.settings.last_check)
            local latest_error = tostring(self.settings.last_error or "")
            self.last_error = latest_error ~= "" and latest_error or nil
            self:ensureSession()
            self:refresh(false)
        end)
    -- 自动开始必须同时满足：用户开了 Auto-start、上次是跑着的、而且这个 KOReader
    -- 进程还没自动开过（session_started）。第三条是死循环的根治：showReader() 之后
    -- 插件会重新 init，光看设置会一遍遍重进 start()。
    elseif self.settings.autostart and self.settings.autostart_resume and not session_started then
        UIManager:scheduleIn(3, function() self:start() end)
    end
end

function Report:save()
    for k, v in pairs(self.settings) do self.store:saveSetting(k, v) end
    self.store:flush()
end

function Report:note(text) UIManager:show(InfoMessage:new{ text = text, timeout = 5 }) end

function Report:isImageMode() return self.settings.display == "image" end
function Report:isDocMode() return self.settings.display == "doc" end
function Report:isNewsMode() return self.settings.display == "news" end
function Report:isStocksMode() return self.settings.display == "stocks" end
-- 两种 EPUB 来源共用同一套「下载书 → 交给阅读器」的流程。
function Report:isBookMode() return self:isDocMode() or self:isNewsMode() end
-- 阅读器里是不是有一本我们要刷新的书：报告/新闻的 EPUB，或行情卡片版。
function Report:usesReader()
    if self:isBookMode() then return true end
    return self:isStocksMode() and self.settings.stocks_epub_file_name ~= nil
end
function Report:docKind() return self:isNewsMode() and "news" or "doc" end
function Report:docSpec() return DOC_KINDS[self:docKind()] end

local function displayName(mode)
    if mode == "image" then return "page images" end
    if mode == "doc" then return "epub" end
    if mode == "news" then return "news" end
    if mode == "stocks" then return "stocks" end
    return "text"
end

function Report:readCache()
    local f = io.open(self.cache_path, "rb")
    if not f then return nil end
    local s = f:read(MAX_BYTES + 1); f:close()
    if not s or #s == 0 or #s > MAX_BYTES then return nil end
    return s
end

function Report:readStocksCache()
    local f = io.open(self.stocks_cache_path, "rb")
    if not f then return nil end
    local s = f:read(MAX_BYTES + 1); f:close()
    if not s or #s == 0 or #s > MAX_BYTES then return nil end
    return s
end

function Report:pagePath(index)
    return string.format("%s/page-%03d.png", self.image_dir, (index or 1) - 1)
end

function Report:hasPage(index)
    return lfs.attributes(self:pagePath(index), "mode") == "file"
end

-- Read width/height out of a PNG's IHDR chunk (8-byte signature + 8-byte
-- length/type, then two big-endian 32-bit values).
function Report:pngSize(path)
    local f = io.open(path, "rb")
    if not f then return nil end
    local head = f:read(24); f:close()
    if not head or #head < 24 or head:sub(1, 8) ~= "\137PNG\r\n\26\n" then return nil end
    local function be32(chunk)
        local b1, b2, b3, b4 = chunk:byte(1, 4)
        return ((b1 * 256 + b2) * 256 + b3) * 256 + b4
    end
    return be32(head:sub(17, 20)), be32(head:sub(21, 24))
end

function Report:holdScreen()
    if self.settings.sleep_screen_held then return end
    self.settings.saved_screensaver_type = G_reader_settings:readSetting("screensaver_type")
    self.settings.sleep_screen_held = true
    G_reader_settings:saveSetting("screensaver_type", "disable")
    self:save()
end

function Report:releaseScreen()
    if not self.settings.sleep_screen_held then return end
    if self.settings.saved_screensaver_type ~= nil then
        G_reader_settings:saveSetting("screensaver_type", self.settings.saved_screensaver_type)
    else G_reader_settings:delSetting("screensaver_type") end
    self.settings.sleep_screen_held = nil
    self.settings.saved_screensaver_type = nil
    self:save()
end

-- 两个「自动」会抢屏幕：004 自己按 INTERVAL 翻页，KOReader 的自动翻页（autoturn）
-- 也在翻同一页。开跑前先让 autoturn 停掉；不改用户存下来的 autoturn 预设，
-- 004 停了以后用户原来的设置还在（同理见 holdScreen/releaseScreen 对屏保的处理）。
function Report:cancelAutoTurn()
    local doc = self.ui and self.ui.document
    if doc and doc.config and doc.config.auto_turn then
        pcall(function() doc.config.auto_turn:stop() end)
    end
    local ok, ReaderAutoTurn = pcall(require, "apps/reader/modules/readerturning")
    if ok and ReaderAutoTurn and ReaderAutoTurn.stop then
        pcall(ReaderAutoTurn.stop, ReaderAutoTurn)
    end
end

function Report:start()
    if emergencyStopped() then
        self:note("004: emergency_stop is on — clear it in the settings file first")
        return
    end
    session_started = true
    -- 手动 Show report 是明确的「再开一次」；自动开始那一路不重置，
    -- 否则 init→start→openDoc→init 就又能循环下去。
    if self.interactive_start then
        session_opened_doc = false
        self.settings.auto_opened_doc = nil
        patchSettings({ auto_opened_doc = nil })
    end
    self.settings.enabled = true
    self.settings.autostart_resume = self.settings.autostart and true or nil
    self:save(); self:holdScreen()
    self:ensureSession()
    self:cancelAutoTurn()
    if self.wake:mode(true) == "awake" then self.wake:hold_awake() end
    if self:isBookMode() then
        -- the reader shows the document; this widget stays out of the way
    elseif self:isImageMode() then
        if not self.dashboard and self:hasPage(self.page_index) then self:showPage(false) end
    elseif self:isStocksMode() then
        -- 已经有卡片版就直接交给阅读器，不必等这一轮联网。
        -- 但同一个进程只自动开一次：showReader() 会重建 ReaderUI、插件重新 init，
        -- 每次都开就又回到「重开 → init → 重开」的死循环。
        local spec = DOC_KINDS.stocks
        local current = self.settings[spec.file]
        if current and lfs.attributes(current, "mode") == "file"
                and not self:docIsOpen(spec) and self.settings.stocks_epub_file_name
                and not session_opened_doc then
            self:openDoc(spec, current)
            return
        end
        local cached = self:readStocksCache()
        if cached and not self.dashboard then self:showText(cached, false) end
    else
        local cached = self:readCache()
        if cached and not self.dashboard then self:showText(cached, false) end
    end
    self:refresh(true)
end

function Report:stop(silent)
    session_started = true   -- 这个进程里手动停了，就别再自动开始
    self.settings.enabled = false
    -- 手动 Stop 就是「这次别跑了」：即便 Auto-start 是 on，下次启动也不再自动开。
    self.settings.autostart_resume = nil
    self:save()
    self.gen = (self.gen or 0) + 1
    self.in_cycle = false
    self.wake:cancel(); self.wake:kindle_hold(false); self.wake:release_awake()
    self:releaseScreen()
    if self.dashboard then
        UIManager:close(self.dashboard)
        self.dashboard, self.box, self.header, self.page_widget = nil, nil, nil, nil
    end
    UIManager:setDirty("all", "full")
    if not silent then self:note("004: stopped (auto-start off until you start it again)") end
end

-- 宿主（阅读器/文件管理器）还活着时，把 004 的开关也挂到它的菜单里：
-- 自动开始之后插件自己的入口在 工具 → 004 Report，但被 EPUB 模式困住时
-- 阅读器菜单里能直接 Stop 更救命。
function Report:attachToHost()
    local ui = self.ui
    if not ui or not ui.menu or ui.menu == self.menu then return end
    self.menu = ui.menu
    pcall(function() ui.menu:registerToMainMenu(self) end)
end

function Report:onCloseWidget()
    -- Preserve enabled state for the replacement ReaderUI, but cancel callbacks owned by
    -- this dying instance. The next instance resumes from session_started/the swap marker.
    self.gen = (self.gen or 0) + 1
    self.wake:cancel(); self.wake:kindle_hold(false); self.wake:release_awake()
    self:releaseScreen()
    if self.dashboard then UIManager:close(self.dashboard); self.dashboard = nil end
end

-- 只把菜单挂到当前宿主。绝不能 UIManager:show 一个隐形 Widget 当“后台壳”：
-- 它会成为最上层窗口并吞掉 Kindle 全屏触摸。计时器本来就是 UIManager 全局任务。
function Report:ensureSession()
    if not self.settings.enabled then return end
    self:attachToHost()
end

function Report:onSuspend()
    self.wake:cancel()
end

function Report:onResume()
    if self.settings.enabled and self.next_due and os.time() >= self.next_due - 5 and not self.in_cycle then
        UIManager:scheduleIn(2, self.refresh_task)
    end
end

-- 下载书（EPUB 模式）只自动打开一次：之后的重开交给用户，避免界面被反复打断。
function Report:noteDocOpened()
    session_opened_doc = true
end

-- Single HTTPS entry point: strict cap, no redirects, never disables TLS checks.
-- Returns body, error, status_code, response_headers.
function Report:httpGet(url, max_bytes, extra_headers, timeout)
    if type(url) ~= "string" or not url:match("^https://") then return nil, "URL must use HTTPS", nil end
    local ca = dataDir() .. "/ca-bundle.crt"
    if lfs.attributes(ca, "mode") ~= "file" then return nil, "KOReader CA bundle not found", nil end
    local https = require("ssl.https")
    https.TIMEOUT = timeout or 25
    local headers = { ["User-Agent"] = "KOReader-004/0.2", ["Cache-Control"] = "no-cache" }
    if extra_headers then
        for k, v in pairs(extra_headers) do headers[k] = v end
    end
    local chunks, size, too_large = {}, 0, false
    local request = {
        url = url, method = "GET", headers = headers,
        protocol = "any", options = { "all", "no_sslv2", "no_sslv3", "no_tlsv1", "no_tlsv1_1" },
        verify = "peer", cafile = ca, redirect = false,
        sink = function(chunk)
            if chunk then
                size = size + #chunk
                if size > max_bytes then too_large = true; return nil, "response too large" end
                chunks[#chunks + 1] = chunk
            end
            return 1
        end,
    }
    -- Keep both LuaSec's regular return values and any thrown Lua error. Some
    -- KOReader/LuaSec builds provide only a nil result on transport failures;
    -- retaining the other values makes the on-device error actionable.
    local called, ok, code, response_headers, status_line = pcall(https.request, request)
    if not called then
        logger.warn("004: HTTPS request raised an error", tostring(ok))
        return nil, "HTTPS request error: " .. tostring(ok), nil
    end
    local numeric_code = tonumber(code)
    if too_large then return nil, "Response exceeds limit", numeric_code end
    if not ok then
        local detail = code
        if detail == nil or detail == "" then detail = status_line end
        if detail == nil or detail == "" then
            detail = type(response_headers) == "string" and response_headers or nil
        end
        if detail == nil or detail == "" then
            detail = "no transport details returned; check Wi-Fi, URL, and TLS certificates"
        end
        detail = tostring(detail):gsub("%c", " "):sub(1, 240)
        logger.warn("004: HTTPS request failed", detail)
        return nil, detail, numeric_code
    end
    code = numeric_code
    if code ~= 200 then return nil, "HTTP " .. tostring(code), code, response_headers end
    return table.concat(chunks), nil, code, response_headers
end

-- 设置里可以写多个地址（空格/逗号分隔），每个再展开成镜像；用户写的先试，镜像兜底。
-- 返回的 `attempts` 记着每个地址失败的原因，状态页据此告诉用户卡在哪一层。
function Report:urlCandidates(configured)
    return Net.candidates(configured)
end

function Report:httpGetAny(urls, max_bytes, extra_headers)
    local attempts = {}
    for _, url in ipairs(urls or {}) do
        local body, err, code, response_headers = self:httpGet(Net.bust(url), max_bytes, extra_headers)
        attempts[#attempts + 1] = { url = url, err = err, code = code, headers = response_headers }
        if body then
            self.last_attempts = attempts
            self.last_via = url
            return body, nil, url, attempts, code
        end
        -- 304 是「没变」，不是失败：继续换镜像只会白下一遍全文。
        if code == 304 then
            self.last_attempts = attempts
            self.last_via = url
            return nil, err, url, attempts, 304
        end
    end
    self.last_attempts = attempts
    if #attempts == 0 then
        return nil, "没有可用的地址", nil, attempts
    end
    -- 只列前两个失败原因，加上一句人话提示；4 个镜像全列出来会把状态页撑爆。
    local parts = {}
    for index = 1, math.min(#attempts, 2) do
        parts[#parts + 1] = string.format("%s → %s", attempts[index].url,
            tostring(attempts[index].err))
    end
    local summary = table.concat(parts, "；")
    if #attempts > 2 then summary = summary .. string.format("；另 %d 个地址也失败", #attempts - 2) end
    return nil, summary .. "（" .. tostring(Net.hint(attempts[1].err)) .. "）", nil, attempts
end

-- Text mode: download the plain-text report, keep the last good copy on failure.
function Report:fetchText()
    local urls = self:urlCandidates(self.settings.url)
    local headers = {}
    local first = urls[1]
    -- ETag 是按地址发的：换成镜像后旧的 ETag 无效，干脆不发，让服务器直接给全文。
    if self.settings.etag and first and self.settings.etag_url == first and self:readCache() then
        headers["If-None-Match"] = self.settings.etag
    end
    local body, err, used, attempts, code = self:httpGetAny(urls, MAX_BYTES, headers)
    if not body then
        if code == 304 then
            local cached = self:readCache()
            if cached then return cached, nil, false end
            self.settings.etag = nil
            return nil, "Cache missing; retrying"
        end
        return nil, err
    end
    if #body == 0 or body:match("^%s*<") or body:find("\0", 1, true) then return nil, "Invalid text report" end
    local temp = self.cache_path .. ".tmp"
    local f = io.open(temp, "wb")
    if not f then return nil, "Cannot write cache" end
    local written = f:write(body); local closed = f:close()
    if not written or not closed or not os.rename(temp, self.cache_path) then
        os.remove(temp); return nil, "Cannot save cache"
    end
    local response_headers = attempts and attempts[#attempts] and attempts[#attempts].headers
    self.settings.etag = response_headers and (response_headers.etag or response_headers.ETag) or nil
    self.settings.etag_url = used
    return body, nil, true
end

-- Image mode: a JSON manifest lists device-sized page images.
function Report:fetchManifest()
    local urls = self:urlCandidates(self.settings.image_url)
    if #urls == 0 then return nil, "Set the image manifest URL first" end
    local body, err = self:httpGetAny(urls, MANIFEST_MAX)
    if not body then return nil, err end
    local decoded
    local ok = pcall(function()
        decoded = require("rapidjson").decode(body)
    end)
    if not ok or type(decoded) ~= "table" then return nil, "Invalid manifest JSON" end
    local pages = decoded.pages
    if type(pages) ~= "table" or #pages == 0 then return nil, "Manifest lists no pages" end
    if #pages > MAX_PAGES then return nil, "Manifest lists " .. #pages .. " pages (max " .. MAX_PAGES .. ")" end
    for i = 1, #pages do
        if type(pages[i]) ~= "string" or pages[i] == "" then return nil, "Bad page entry #" .. i end
    end
    return decoded
end

function Report:signatureOf(manifest)
    return tostring(manifest.generated or "") .. "|" .. tostring(#manifest.pages)
end

function Report:prunePages(keep)
    if lfs.attributes(self.image_dir, "mode") ~= "directory" then return end
    for entry in lfs.dir(self.image_dir) do
        local index = entry:match("^page%-(%d+)%.png$")
        if index and tonumber(index) + 1 > keep then
            os.remove(self.image_dir .. "/" .. entry)
        end
    end
end

function Report:downloadPages(manifest)
    if lfs.attributes(self.image_dir, "mode") ~= "directory" then lfs.mkdir(self.image_dir) end
    local base = self.settings.image_url
    local total = 0
    for i = 1, #manifest.pages do
        local url = resolveURL(base, manifest.pages[i])
        if not url:match("^https://") then return nil, "Page " .. i .. " is not HTTPS" end
        if self.header then
            self.header:setText("004 | loading " .. i .. "/" .. #manifest.pages)
            if self.dashboard then UIManager:setDirty(self.dashboard, "ui") end
        end
        local body, err = self:httpGet(url, PAGE_MAX)
        if not body then return nil, "Page " .. i .. "/" .. #manifest.pages .. ": " .. tostring(err) end
        total = total + #body
        if total > PAGES_TOTAL_MAX then return nil, "Pages exceed 64 MiB" end
        local target = self:pagePath(i)
        local temp = target .. ".tmp"
        local f = io.open(temp, "wb")
        if not f then return nil, "Cannot write page cache" end
        local written = f:write(body); local closed = f:close()
        if not written or not closed or not os.rename(temp, target) then
            os.remove(temp); return nil, "Cannot save page cache"
        end
    end
    self:prunePages(#manifest.pages)
    return #manifest.pages
end

function Report:fetchDocManifest(spec)
    spec = spec or self:docSpec()
    local urls = self:urlCandidates(self.settings[spec.manifest] or "")
    if #urls == 0 then
        return nil, (spec.manifest == "news_url" and "Set the news manifest URL first"
            or "Set the EPUB manifest URL first")
    end
    local body, err, used = self:httpGetAny(urls, MANIFEST_MAX)
    if not body then return nil, err end
    local decoded
    local ok = pcall(function() decoded = require("rapidjson").decode(body) end)
    if not ok or type(decoded) ~= "table" then return nil, "Invalid manifest JSON" end
    if type(decoded.doc) ~= "string" or decoded.doc == "" then return nil, "Manifest has no doc entry" end
    if type(decoded.generated) ~= "string" or decoded.generated == "" then
        return nil, "Manifest has no generated stamp"
    end
    -- 清单可能来自镜像：EPUB 的相对路径要以真正成功的那个地址为基准。
    decoded.base = used
    return decoded
end

-- 行情面板不能再「第一个 HTTP 200 就算成功」：raw / Pages / CDN 可能短暂处于
-- 不同发布代。逐个短超时读取并解析，按 data_time → generated 选择最新有效清单；
-- 本地已有可用缓存时拒绝倒退。正文下载仍走首个成功镜像，避免重复拉大 EPUB。
function Report:fetchStocksManifest()
    local urls = self:urlCandidates(self.settings.stocks_url or "")
    if #urls == 0 then return nil, "Set the stocks manifest URL first" end
    local attempts, best, best_url = {}, nil, nil
    for _, url in ipairs(urls) do
        local request_url = Net.compactManifest(url)
        local body, err, code, response_headers = self:httpGet(
            Net.bust(request_url), STOCKS_MANIFEST_MAX, nil, STOCKS_MANIFEST_TIMEOUT)
        -- 升级过渡：旧部署还没有 stocks-manifest.json 时只在 404 后回退全量；
        -- 一旦小清单发布，常态就是 6 个小响应，而不是每 7 分钟下载 6 份全量行情。
        if not body and code == 404 and request_url ~= url then
            body, err, code, response_headers = self:httpGet(
                Net.bust(url), STOCKS_MANIFEST_MAX, nil, STOCKS_MANIFEST_TIMEOUT)
            request_url = url
        end
        local attempt = { url = url, request_url = request_url,
            err = err, code = code, headers = response_headers }
        attempts[#attempts + 1] = attempt
        if body then
            local decoded
            local ok = pcall(function() decoded = require("rapidjson").decode(body) end)
            if not ok or type(decoded) ~= "table" then
                attempt.err = "Invalid manifest JSON"
            elseif type(decoded.doc) ~= "string" or decoded.doc == "" then
                attempt.err = "Manifest has no doc entry"
            elseif type(decoded.generated) ~= "string" or not Net.isoSeconds(decoded.generated) then
                attempt.err = "Manifest has invalid generated stamp"
            elseif decoded.data_time ~= nil and not Net.isoSeconds(decoded.data_time) then
                attempt.err = "Manifest has invalid data_time"
            else
                decoded.base = url
                attempt.manifest = decoded
                if not best or Net.compareFreshness(decoded, best) > 0 then
                    best, best_url = decoded, url
                end
            end
        end
    end
    self.last_attempts = attempts
    if not best then
        local parts = {}
        for index = 1, math.min(#attempts, 2) do
            parts[#parts + 1] = string.format("%s → %s", attempts[index].url,
                tostring(attempts[index].err or "invalid manifest"))
        end
        local summary = table.concat(parts, "；")
        if #attempts > 2 then
            summary = summary .. string.format("；另 %d 个地址也失败", #attempts - 2)
        end
        return nil, summary .. "（没有有效行情清单）"
    end

    local spec = DOC_KINDS.stocks
    local current = self.settings[spec.file]
    local have_epub = current and lfs.attributes(current, "mode") == "file"
    local have_text = self:readStocksCache() ~= nil
    local old_generated = self.settings.stocks_generated
    if not old_generated then
        local epub_sig = tostring(self.settings[spec.sig] or "")
        local text_sig = tostring(self.settings.stocks_sig or "")
        old_generated = Net.compareFreshness({ generated = epub_sig },
            { generated = text_sig }) >= 0 and epub_sig or text_sig
    end
    local floor = {
        data_time = self.settings.stocks_data_time,
        generated = old_generated,
    }
    if (have_epub or have_text) and Net.isRollback(best, floor) then
        local cached = tostring(floor.data_time or floor.generated or "unknown")
        local offered = tostring(best.data_time or best.generated or "unknown")
        return nil, "All online manifests are older than cache (online " .. offered
            .. ", cache " .. cached .. "); keeping cached panel"
    end
    best.asset_bases = {}
    for _, attempt in ipairs(attempts) do
        local candidate = attempt.manifest
        local same_content = candidate and (
            (best.data_hash ~= nil and candidate.data_hash == best.data_hash)
            or candidate.generated == best.generated)
        if same_content then best.asset_bases[#best.asset_bases + 1] = attempt.url end
    end
    self.last_via = best_url
    self.last_manifest_generated = best.generated
    return best
end

function Report:stocksAssetCandidates(manifest, field)
    local path = manifest[field]
    local list, resolved, seen = {}, {}, {}
    local bases = manifest.asset_bases or { manifest.base or self.settings.stocks_url }
    local function add(url)
        if url and not seen[url] and #list < Net.MAX_MIRRORS then
            seen[url] = true; list[#list + 1] = url
        end
    end
    -- 先放每个「同一内容版本」清单自己的正文地址，再用其 GitHub 镜像补满。
    for _, base in ipairs(bases) do
        local url = resolveURL(base, path)
        resolved[#resolved + 1] = url
        add(url)
    end
    for _, url in ipairs(resolved) do
        for _, mirror in ipairs(self:urlCandidates(url)) do add(mirror) end
    end
    return list
end

function Report:stocksSignature(manifest)
    -- data_hash 是正文语义指纹；工作流只重排相同数据时 generated 会变，但不用让
    -- Kindle 重下同一本书。老清单没有 data_hash 才回退 generated。
    return tostring(manifest.data_hash or manifest.generated or "")
end

function Report:acceptStocksManifest(manifest)
    self.settings.stocks_generated = manifest.generated
    self.settings.stocks_data_hash = manifest.data_hash
    self.settings.stocks_manifest_via = manifest.base
    if type(manifest.data_time) == "string" then
        self.settings.stocks_data_time = manifest.data_time
    end
    if type(manifest.data_time_kind) == "string" then
        self.settings.stocks_data_time_kind = manifest.data_time_kind
    end
end

-- 行情面板 EPUB 卡片版：与 refreshDoc 同形（新文件名从头打开、旧文件清理），
-- 只是清单地址用 stocks_url。失败时抛错，由 refreshStocks 回退纯文本。
function Report:refreshStocksEpub(manifest, sig)
    local spec = DOC_KINDS.stocks
    local current = self.settings[spec.file]
    local have = current and lfs.attributes(current, "mode") == "file"
    if sig == tostring(self.settings[spec.sig] or "") and have then
        self:acceptStocksManifest(manifest)
        self:save()
        if self:docIsOpen(spec) then
            self:turnDocPage(spec)
        else
            -- 上一轮可能已经把新文件写好，却在 showReader() 时被 ReaderUI 重建打断；
            -- 如果屏幕上仍是旧的 stocks-card EPUB，这一轮必须再切一次，不能只看 sig。
            local open = self.ui and self.ui.document and self.ui.document.file
            local name = open and open:match("([^/]+)$")
            if name and name:sub(1, #spec.prefix) == spec.prefix then
                self:openDoc(spec, current)
            end
        end
        return
    end
    local url = resolveURL(manifest.base or self.settings.stocks_url, manifest.epub)
    if not url:match("^https://") then error("Document URL is not HTTPS") end
    local body, download_error = self:httpGetAny(
        self:stocksAssetCandidates(manifest, "epub"), DOC_MAX)
    if not body then error(download_error) end
    if #body < 512 or body:sub(1, 2) ~= "PK" then error("Downloaded file is not an EPUB") end
    local seq = (tonumber(self.settings[spec.seq]) or 0) + 1
    local name = string.format("%s%d.epub", spec.prefix, seq)
    local target = self:docPath(spec, name)
    local temp = target .. ".tmp"
    local f = io.open(temp, "wb")
    if not f then error("Cannot write document cache") end
    local wrote = f:write(body)
    local closed = f:close()
    if not wrote or not closed or not os.rename(temp, target) then
        os.remove(temp); error("Cannot save document")
    end
    self.settings[spec.seq] = seq
    self.settings[spec.file_name] = name
    -- 不要先删屏幕上仍打开的旧书；pruneDocs 会保护实际打开路径。新书真正交给
    -- ReaderUI 后才提交 sig/data_time，showReader 失败则下一轮仍会重试。
    self:pruneDocs(name, spec.prefix)
    self:save()
    self:openDoc(spec, target)
    self.settings[spec.sig] = sig
    self:acceptStocksManifest(manifest)
    self:save()
end

function Report:refreshStocks()
    local manifest, err = self:fetchStocksManifest()
    if not manifest then error(err) end
    local sig = self:stocksSignature(manifest)
    self.last_check = os.time()
    self.settings.last_check = self.last_check
    -- 清单里有 epub 就打开卡片版（交给阅读器排版）；没有或下载失败才回退纯文本。
    if type(manifest.epub) == "string" and manifest.epub ~= "" then
        local ok, epub_error = pcall(function() self:refreshStocksEpub(manifest, sig) end)
        if ok then return end
        logger.warn("stocks epub failed, falling back to text: " .. tostring(epub_error))
    end
    local cached = self:readStocksCache()
    if sig == tostring(self.settings.stocks_sig or "") and cached then
        self:acceptStocksManifest(manifest)
        self:save()
        if not self.box then self:showText(cached, false)
        else self:advance() end
        return
    end
    local url = resolveURL(manifest.base or self.settings.stocks_url, manifest.doc)
    if not url:match("^https://") then error("Document URL is not HTTPS") end
    local body, download_error = self:httpGetAny(
        self:stocksAssetCandidates(manifest, "doc"), MAX_BYTES)
    if not body then error(download_error) end
    if #body == 0 then error("Downloaded panel is empty") end
    local temp = self.stocks_cache_path .. ".tmp"
    local f = io.open(temp, "wb")
    if not f then error("Cannot save panel cache") end
    local wrote = f:write(body)
    local closed = f:close()
    if not wrote or not closed or not os.rename(temp, self.stocks_cache_path) then
        os.remove(temp); error("Cannot save panel cache")
    end
    self.settings.stocks_sig = sig
    self:acceptStocksManifest(manifest)
    self:save()
    self:showText(body, false)
end

function Report:refreshText()
    local body, err = self:fetchText()
    if not body then error(err) end
    self.last_check = os.time(); self:save()
    if not self.box then self:showText(body, false)
    elseif self.current_text ~= body then self:showText(body, true)
    else self:advance() end
end

function Report:refreshImage()
    local manifest, err = self:fetchManifest()
    if not manifest then error(err) end
    local signature = self:signatureOf(manifest)
    local missing = false
    for i = 1, #manifest.pages do
        if not self:hasPage(i) then missing = true; break end
    end
    if signature ~= tostring(self.settings.image_sig or "") or missing then
        self.page_count = #manifest.pages
        if not self.dashboard then
            self:showText("004 · 图形界面\n\n正在下载 1/" .. #manifest.pages ..
                " 页页面图片…\n首次需要下载全部页面，之后每 " .. intervalText()
                .. " 只检查是否有更新。", false)
        end
        local count, download_error = self:downloadPages(manifest)
        if not count then error(download_error) end
        self.page_count = count
        self.settings.image_count = count
        self.settings.image_sig = signature
        self.page_index = 1
        self.last_check = os.time(); self:save()
        self:showPage(false)
        return
    end
    self.page_count = #manifest.pages
    self.settings.image_count = self.page_count
    self.last_check = os.time(); self:save()
    if not self.dashboard then self:showPage(false) else self:advance() end
end

function Report:stocksCacheLabel()
    local stamp = tostring(self.settings.stocks_data_time or "")
    local epoch = Net.isoSeconds(stamp)
    if not epoch then return stamp ~= "" and stamp or "unknown time" end
    local age = math.max(0, os.time() - epoch)
    local age_text
    if age >= 86400 then age_text = string.format("%.0f days old", age / 86400)
    elseif age >= 3600 then age_text = string.format("%.0f hours old", age / 3600)
    else age_text = string.format("%.0f minutes old", age / 60) end
    return beijing(epoch, "%Y-%m-%d %H:%M:%S") .. " Beijing, " .. age_text
end

function Report:refresh(interactive)
    if self.in_cycle or not self.settings.enabled then return end
    self.in_cycle = true; self.wake:cancel(); self.wake:kindle_hold(true)
    self.gen = (self.gen or 0) + 1
    local gen = self.gen
    local watchdog
    local function finish(error_message)
        if gen ~= self.gen then return end
        UIManager:unschedule(watchdog)
        if error_message and self:isStocksMode() then
            error_message = tostring(error_message) .. "\nCached snapshot: " .. self:stocksCacheLabel()
        end
        self.gen = self.gen + 1; self.in_cycle = false; self.last_error = error_message
        -- ReaderUI 换书后插件实例会重建；把“确实检查过”和最后错误写进设置，Status
        -- 才不会回到 never / No error。空字符串表示最近一轮成功。
        self.last_check = os.time()
        self.settings.last_check = self.last_check
        self.settings.last_error = error_message and tostring(error_message) or ""
        self:save()
        if self.header then
            local state = error_message and "OFFLINE / CACHED" or "ONLINE"
            local checked = self.last_check and beijing(self.last_check, "%m-%d %H:%M") or "--"
            self.header:setText("004 | " .. state .. " | " .. checked .. " | tap")
            if self.dashboard then UIManager:setDirty(self.dashboard, "full") end
        end
        self.wake:kindle_hold(false)
        if error_message and (interactive or self:isImageMode() or self:usesReader()) then
            if interactive or self.last_notified_error ~= error_message then
                self:note("004: " .. error_message)
                self.last_notified_error = error_message
            end
        else
            self.last_notified_error = nil
        end
        if error_message and self.dashboard then self:advance() end
        self.next_due = os.time() + INTERVAL
        self.wake:schedule(INTERVAL, self.refresh_task, self.wake:mode(true))
        -- 自动开始之后面板可能已经被关掉/换界面：确保后台壳还在，计时不会断。
        if self.settings.enabled then self:ensureSession() end
        -- Keep Wi-Fi on: reconnecting every minute is slow and may miss cycles.
        if self.wake:mode(true) == "rtc" and not interactive and not self:usesReader() then
            self.wake:sleep_now()
        end
    end
    watchdog = function() finish("Wi-Fi connection timed out") end
    UIManager:scheduleIn(WIFI_WAIT, watchdog)
    local ok = NetworkMgr:turnOnWifiAndWaitForConnection(function()
        if gen ~= self.gen then return end
        local success, err = pcall(function()
            if self:isBookMode() then self:refreshDoc(self:docKind())
            elseif self:isImageMode() then self:refreshImage()
            elseif self:isStocksMode() then self:refreshStocks()
            else self:refreshText() end
        end)
        if success then finish(nil) else finish(tostring(err)) end
    end)
    if ok == false then finish("Wi-Fi could not be started") end
end

function Report:makeScreen(frame)
    local w, h = Screen:getWidth(), Screen:getHeight()
    local plugin = self
    local screen = InputContainer:new{ dimen = Geom:new{ x = 0, y = 0, w = w, h = h }, frame }
    screen.ges_events = { Tap = { GestureRange:new{ ges = "tap", range = Geom:new{ x = 0, y = 0, w = w, h = h } } } }
    screen.onTap = function() plugin:menu() return true end
    return screen
end

function Report:showText(text, preserve)
    local previous_line = preserve and self.box and self.box.virtual_line_num or 1
    local w, h = Screen:getWidth(), Screen:getHeight()
    local margin = math.floor(w * 0.035)
    local face = Font:getFace("cfont", 22)
    local header = TextWidget:new{ text = "004  |  " .. intervalText() .. "  |  tap for menu",
        face = Font:getFace("smallinfofont") }
    local box = TextBoxWidget:new{ text = text, face = face, width = w - 2 * margin,
        height = h - 2 * margin - header:getSize().h - 8 }
    if previous_line > 1 then box:scrollLines(math.min(previous_line - 1, box:getAllLineCount() - 1)) end
    local group = VerticalGroup:new{ align = "left", header, box }
    local frame = FrameContainer:new{ width = w, height = h, padding = margin, bordersize = 0, background = Blitbuffer.COLOR_WHITE, group }
    local old = self.dashboard
    local screen = self:makeScreen(frame)
    self.box, self.dashboard, self.current_text, self.header, self.page_widget = box, screen, text, header, nil
    UIManager:show(screen); UIManager:setDirty(screen, "full")
    if old then UIManager:close(old) end
end

-- EPUB ("字模式"): the report is downloaded as a book and handed to KOReader's
-- own reader, so crengine does the typography and every font/margin setting the
-- device already has applies.
function Report:docPath(spec, name)
    return dataDir() .. "/" .. (name or self.settings[spec.file_name] or (spec.prefix .. "1.epub"))
end

function Report:docIsOpen(spec)
    local current = self.ui and self.ui.document and self.ui.document.file
    return current ~= nil and current == self.settings[(spec or self:docSpec()).file]
end

function Report:turnDocPage(spec)
    if not self:docIsOpen(spec) then return end
    -- Give the freshly opened book a full interval before the first turn.
    local opened = tonumber(self.settings[spec.opened_at])
    if opened and os.time() - opened < INTERVAL - 5 then return end
    local doc, view = self.ui.document, self.ui.view
    if not doc or not view or not view.state then return end
    local Event = require("ui/event")
    local before = view.state.page
    self.ui:handleEvent(Event:new("GotoViewRel", 1))
    -- At the last page GotoViewRel does nothing: wrap to page 1 explicitly.
    if before and view.state.page == before and doc.getPageCount then
        local pages = doc:getPageCount()
        if pages and pages > 0 then
            self.ui:handleEvent(Event:new("GotoPage", (before % pages) + 1))
        end
    end
end

function Report:openDoc(spec, path)
    local current = self.ui and self.ui.document and self.ui.document.file
    if current and current == path then
        -- 已经在看这一本了：showReader() 会重建 ReaderUI，纯属白折腾一遍界面。
        logger.dbg("004: document already open, not reopening: " .. tostring(path))
        return
    end
    session_opened_doc = true
    -- 打开书会换掉整个 ReaderUI：先把「自动开过一次」写进设置，万一进程内标记没
    -- 生效，重启后的 init 也不会再自动开书。
    patchSettings({ auto_opened_doc = true })
    self.settings[spec.file] = path
    self.settings[spec.opened_at] = os.time()
    -- showReader 会换掉整个插件实例；留下接力标记，让新实例马上联网检查并重新挂
    -- 7 分钟定时器。没有这个标记，屏幕虽打开缓存书，Status 却会永远是 never。
    if self.settings.enabled then self.settings.resume_after_reader_swap = true end
    self:save()
    -- showReader is documented as the only safe way to build a new ReaderUI
    -- instance; it closes the current one, so our widget instance is replaced.
    require("apps/reader/readerui"):showReader(path)
end

function Report:refreshDoc(kind)
    local spec = DOC_KINDS[kind or "doc"] or DOC_KINDS.doc
    local manifest, err = self:fetchDocManifest(spec)
    if not manifest then error(err) end
    local sig = manifest.generated
    if type(manifest.data_time) == "string" then self.settings[spec.data_time] = manifest.data_time end
    -- 精度跟着时间一起存：正文只给到交易日时，清单里的 00:00:00 是补出来的，
    -- 不是真实时刻，显示和算年龄都要区别对待。
    if type(manifest.data_time_kind) == "string" then
        self.settings[spec.data_time_kind] = manifest.data_time_kind
    end
    local current = self.settings[spec.file]
    local have = current and lfs.attributes(current, "mode") == "file"
    self.last_check = os.time()
    if sig == tostring(self.settings[spec.sig] or "") and have then
        self:save()
        self:turnDocPage(spec)
        return
    end
    local url = resolveURL(manifest.base or self.settings[spec.manifest], manifest.doc)
    if not url:match("^https://") then error("Document URL is not HTTPS") end
    local body, download_error = self:httpGetAny(self:urlCandidates(url), DOC_MAX)
    if not body then error(download_error) end
    if #body < 512 or body:sub(1, 2) ~= "PK" then error("Downloaded file is not an EPUB") end
    -- A fresh file name every version: KOReader then opens it at page 1 instead
    -- of resuming the previous position.
    local seq = (tonumber(self.settings[spec.seq]) or 0) + 1
    local name = string.format("%s%d.epub", spec.prefix, seq)
    local target = self:docPath(spec, name)
    local temp = target .. ".tmp"
    local f = io.open(temp, "wb")
    if not f then error("Cannot write document cache") end
    local wrote = f:write(body)
    local closed = f:close()
    if not wrote or not closed or not os.rename(temp, target) then
        os.remove(temp); error("Cannot save document")
    end
    self.settings[spec.seq] = seq
    self.settings[spec.file_name] = name
    self.settings[spec.sig] = sig
    self:pruneDocs(name, spec.prefix)
    self:save()
    self:openDoc(spec, target)
end

function Report:pruneDocs(keep, prefix)
    local dir = dataDir()
    local open = self.ui and self.ui.document and self.ui.document.file
    local open_name = open and open:match("([^/]+)$")
    local ok, iter, state = pcall(lfs.dir, dir)
    if not ok or type(iter) ~= "function" then return end
    for entry in iter, state do
        if type(entry) == "string" and prefix and entry:sub(1, #prefix) == prefix
                and entry ~= keep and entry ~= open_name then
            os.remove(dir .. "/" .. entry)
        end
    end
end

function Report:showPage(preserve)
    if not self:hasPage(self.page_index) then return false end
    local w, h = Screen:getWidth(), Screen:getHeight()
    local path = self:pagePath(self.page_index)
    -- Pages rendered at the exact screen size are shown unscaled (scale 1),
    -- which keeps e-ink text crisp; anything else falls back to best-fit.
    local pw, ph = self:pngSize(path)
    local scale = (pw == w and ph == h) and 1 or 0
    local image = ImageWidget:new{ file = path, width = w, height = h, scale_factor = scale }
    local frame = FrameContainer:new{ width = w, height = h, padding = 0, bordersize = 0,
        background = Blitbuffer.COLOR_WHITE,
        CenterContainer:new{ dimen = Geom:new{ x = 0, y = 0, w = w, h = h }, image } }
    local old = self.dashboard
    local screen = self:makeScreen(frame)
    self.box, self.dashboard, self.current_text, self.header, self.page_widget = nil, screen, nil, nil, image
    UIManager:show(screen); UIManager:setDirty(screen, "full")
    if old then UIManager:close(old) end
    return true
end

function Report:advance()
    if self:isImageMode() then
        local count = self.page_count or 0
        if count == 0 then return end
        self.page_index = (self.page_index or 1) + 1
        if self.page_index > count then self.page_index = 1 end
        self:showPage(true)
        return
    end
    if not self.box or not self.dashboard then return end
    if self.box.virtual_line_num + self.box:getVisLineCount() > self.box:getAllLineCount() then
        self.box:scrollToTop()
    else self.box:scrollDown() end
    UIManager:setDirty(self.dashboard, "full")
end

-- utcEpoch / isoSeconds / beijing 定义在文件顶部（resolveURL 之后）：
-- refresh 里 finish() 要调 beijing，local 必须先声明。不要把它们搬回这里。

-- 逐个地址单独试一遍（短超时），把结果摊在屏幕上：设备链路上到底哪个地址可用。
function Report:diagnose()
    local spec = self:docSpec()
    local manifest_url = self.settings[spec.manifest] or ""
    if self:isStocksMode() then manifest_url = self.settings.stocks_url or "" end
    local urls = self:urlCandidates(manifest_url)
    if #urls == 0 then self:note("Fill the manifest URL first"); return end
    local lines = { "Manifest: " .. tostring(manifest_url) }
    local cap = self:isStocksMode() and STOCKS_MANIFEST_MAX or MANIFEST_MAX
    for _, url in ipairs(urls) do
        local started = os.time()
        local request_url = self:isStocksMode() and Net.compactManifest(url) or url
        local body, err = self:httpGet(Net.bust(request_url), cap, nil, 10)
        local spent = os.time() - started
        local status = body and string.format("OK %d B", #body)
            or string.format("%s (%ds)", tostring(err), spent)
        lines[#lines + 1] = request_url:gsub("^https://", "") .. "\n  " .. status
    end
    lines[#lines + 1] = "Tap Refresh now to retry."
    self:note(table.concat(lines, "\n"))
end

function Report:status()
    local mode = (self.wake:mode(true) == "rtc" and "hardware wake" or "awake timer")
        .. " · every " .. intervalText()
    local checked = self.last_check and (beijing(self.last_check, "%Y-%m-%d %H:%M:%S") .. " (Beijing)") or "never"
    local data = ""
    local spec = self:docSpec()
    -- 不能写成 `a and b or c`：新闻那边还没有数据时间时 b 是 nil，会回落到报告的时间。
    local stamp_setting, kind_setting
    if self:isBookMode() then
        stamp_setting, kind_setting = self.settings[spec.data_time], self.settings[spec.data_time_kind]
    elseif self:isStocksMode() then
        stamp_setting, kind_setting = self.settings.stocks_data_time, self.settings.stocks_data_time_kind
    else
        stamp_setting, kind_setting = self.settings.data_time, self.settings.data_time_kind
    end
    local stamp = tostring(stamp_setting or "")
    if stamp ~= "" then
        local date_only = kind_setting == "date"
        local then_time = isoSeconds(stamp)
        -- 只到日期时按当天 23:59:59 UTC 起算：报告当天晚上不该被显示成 stale。
        if date_only and then_time then then_time = then_time + 86399 end
        local age = nil
        if then_time then
            age = (os.time() - then_time) / 3600
            if date_only and age < 0 then age = 0 end
        end
        local shown
        if date_only then
            -- 不把补出来的午夜换算成「08:00:00 (Beijing)」这种看着精确的假时刻。
            shown = stamp:sub(1, 10) .. " (date only)"
        else
            shown = then_time and (beijing(then_time, "%Y-%m-%d %H:%M:%S") .. " (Beijing)") or stamp
        end
        data = "\nData: " .. shown
        if age and age >= 24 then
            data = data .. string.format("  (stale %.0fd)", age / 24)
        elseif age and age < 0 then
            data = data .. "  (future?)"
        end
    end
    local source
    if self:isBookMode() then
        source = (self:isNewsMode() and "news: " or "epub: ") .. tostring(self.settings[spec.manifest])
    elseif self:isStocksMode() then
        source = "stocks: " .. tostring(self.settings.stocks_url)
    elseif self:isImageMode() then
        source = "images: " .. tostring(self.settings.image_url)
    else
        source = "text: " .. self.settings.url
    end
    local page = ""
    if self:isBookMode() then
        page = "\n" .. spec.label .. ": " .. tostring(self.settings[spec.file_name] or "-")
            .. (self:docIsOpen(spec) and " (open)" or " (closed)")
        if self.last_attempts and #self.last_attempts > 1 then
            page = page .. string.format("\n  tried %d URLs, ok: %s", #self.last_attempts,
                tostring((self.last_via or "-"):gsub("^https://", "")))
        end
    elseif self:isStocksMode() then
        local stocks_spec = DOC_KINDS.stocks
        if self.settings.stocks_epub_file_name then
            local actual = self.ui and self.ui.document and self.ui.document.file
            local actual_name = actual and actual:match("([^/]+)$")
            local old_open = actual_name
                and actual_name:sub(1, #stocks_spec.prefix) == stocks_spec.prefix
                and not self:docIsOpen(stocks_spec)
            page = "\nSTOCKS: " .. tostring(self.settings.stocks_epub_file_name)
                .. (self:docIsOpen(stocks_spec) and " (open)"
                    or (old_open and " (OUTDATED EPUB STILL OPEN)" or " (closed)"))
        else
            page = "\nSTOCKS: text cache "
                .. (self:readStocksCache() and "(available)" or "(missing)")
        end
        page = page .. "\n  accepted: " .. tostring(self.settings.stocks_generated or "-")
            .. "\n  via: " .. tostring((self.settings.stocks_manifest_via or "-"):gsub("^https://", ""))
    elseif self:isImageMode() then
        page = "\nPage " .. tostring(self.page_index or 0) .. "/" .. tostring(self.page_count or 0)
        local pw, ph
        if self:hasPage(self.page_index) then
            pw, ph = self:pngSize(self:pagePath(self.page_index))
        end
        if pw then
            page = page .. "  (" .. tostring(pw) .. "x" .. tostring(ph)
                .. (pw == Screen:getWidth() and ph == Screen:getHeight() and ", 1:1" or ", scaled") .. ")"
        end
    end
    local auto = "\nAuto-start: " .. (self.settings.autostart and "on" or "off")
        .. (self.settings.emergency_stop and " · EMERGENCY STOP" or "")
        .. " · now: " .. (self.settings.enabled and "running" or "idle")
    self:note("004 v" .. PLUGIN_VERSION .. " (" .. self.settings.display .. ")\n" .. source .. "\n" .. mode .. page .. auto .. data ..
        "\nLast check: " .. checked .. "\n" .. (self.last_error or "No error"))
end

local URL_KINDS = {
    text  = { key = "url",       clear = "etag",
              title = "004 text report HTTPS URL" },
    image = { key = "image_url", clear = "image_sig",
              title = "004 page manifest HTTPS URL" },
    doc   = { key = "doc_url",   clear = "doc_sig",
              title = "004 EPUB manifest HTTPS URL" },
    news  = { key = "news_url",  clear = "news_sig",
              title = "004 daily news manifest HTTPS URL" },
    stocks = { key = "stocks_url", clear = "stocks_epub_sig",
              title = "THS stocks manifest HTTPS URL" },
}

function Report:setURL(kind)
    local spec = URL_KINDS[kind or "text"] or URL_KINDS.text
    local dialog
    dialog = InputDialog:new{
        title = spec.title,
        input = self.settings[spec.key] or "",
        buttons = { { { text = "Cancel", callback = function() UIManager:close(dialog) end },
            { text = "Save", callback = function()
                local url = dialog:getInputText():match("^%s*(.-)%s*$")
                if url ~= "" then
                    -- 可以写多个地址（空格/逗号分隔）：第一个不通会依次试后面的和镜像。
                    local pieces = 0
                    for piece in url:gmatch("[^%s,;]+") do
                        pieces = pieces + 1
                        if not piece:match("^https://") then
                            self:note("Every URL must be HTTPS"); return
                        end
                    end
                    if pieces == 0 then self:note("HTTPS URL required"); return end
                end
                self.settings[spec.key] = url
                -- clear 已经是这个来源自己的 sig 键，换 URL 就等于作废旧缓存。
                self.settings[spec.clear] = nil
                if kind == "stocks" then
                    -- 行情有 EPUB / TXT 两套签名，还有防倒退水位。用户主动换源时全部
                    -- 作废，否则新地址会被旧地址的 data_time 误判成“禁止倒退”。
                    self.settings.stocks_sig = nil
                    self.settings.stocks_generated = nil
                    self.settings.stocks_data_hash = nil
                    self.settings.stocks_data_time = nil
                    self.settings.stocks_data_time_kind = nil
                    self.settings.stocks_manifest_via = nil
                end
                self:save()
                UIManager:close(dialog)
            end } } },
    }
    UIManager:show(dialog); dialog:onShowKeyboard()
end

-- 自动阅读预设（默认取消）：关掉时立刻停止当前这一轮，并把 enabled 存成 false，
-- 下次启动 init() 不会再自动 start()。
function Report:toggleAutostart()
    self.settings.autostart = not self.settings.autostart
    if not self.settings.autostart and self.settings.enabled then self:stop() end
    self:save()
    self:note("004: auto-start " .. (self.settings.autostart and "on" or "off"))
end

-- 紧急停止：写进设置文件的 emergency_stop。打开时立刻 stop()，之后 init() 也不再
-- 自动开始；关闭就恢复正常。设备被困住又进不了插件菜单时，直接改文件这一个键也行。
function Report:toggleEmergencyStop()
    self.settings.emergency_stop = not self.settings.emergency_stop
    if self.settings.emergency_stop then
        self.settings.autostart = false
        self:stop(true)
    end
    self:save()
    self:note("004: emergency stop " .. (self.settings.emergency_stop and "on" or "off"))
end

function Report:setDisplay(mode)
    self.settings.display = mode
    self:save()
    if self.dashboard then
        UIManager:close(self.dashboard)
        self.dashboard, self.box, self.header, self.page_widget = nil, nil, nil, nil
    end
    if self:isBookMode() then
        self:refresh(true)
    elseif self:isStocksMode() then
        -- 有 EPUB 卡片版就交给阅读器排版，纯文本缓存只在回退时用。
        if not self.settings.stocks_epub_file_name then
            local cached = self:readStocksCache()
            if cached then self:showText(cached, false) end
        end
    elseif not self:isImageMode() then
        local cached = self:readCache()
        if cached then self:showText(cached, false) end
    elseif self:hasPage(self.page_index) then
        self:showPage(false)
    end
end

function Report:chooseDisplay()
    local dialog
    local function pick(mode)
        UIManager:close(dialog)
        self:setDisplay(mode)
        if self.settings.enabled then self:refresh(true) end
    end
    dialog = ButtonDialog:new{ buttons = {
        { { text = "Text report", callback = function() pick("text") end } },
        { { text = "EPUB (styled)", callback = function() pick("doc") end } },
        { { text = "News subscription", callback = function() pick("news") end } },
        { { text = "Stocks panel", callback = function() pick("stocks") end } },
        { { text = "Page images", callback = function() pick("image") end } },
    } }
    UIManager:show(dialog)
end

function Report:refreshNow()
    -- EPUB 交给 ReaderUI 后，点屏幕只会打开 KOReader 菜单，进不到自绘面板菜单；
    -- 因此主菜单也必须有 Refresh now。若当前 idle，这个按钮同时启动服务。
    if not self.settings.enabled then
        self.interactive_start = true
        self:start()
        self.interactive_start = nil
    else
        self:refresh(true)
    end
end

function Report:menu()
    local dialog
    dialog = ButtonDialog:new{ buttons = {
        { { text = "Next page", callback = function() UIManager:close(dialog); self:advance() end },
          { text = "Refresh now", callback = function() UIManager:close(dialog); self:refreshNow() end } },
        { { text = "Display mode", callback = function() UIManager:close(dialog); self:chooseDisplay() end },
          { text = "Status", callback = function() UIManager:close(dialog); self:status() end } },
        { { text = "Stop", callback = function() UIManager:close(dialog); self:stop() end } },
    } }
    UIManager:show(dialog)
end

function Report:addToMainMenu(items)
    items.octopus004 = { text = "004 Report", sorting_hint = "tools", sub_item_table = {
        { text_func = function() return self.settings.enabled and "Stop report" or "Show report" end,
          callback = function()
              if self.settings.enabled then self:stop()
              else self.interactive_start = true; self:start(); self.interactive_start = nil end
          end },
        { text = "Refresh now (starts if idle)", callback = function() self:refreshNow() end },
        { text_func = function()
              return "Auto-start on launch: " .. (self.settings.autostart and "on" or "off")
          end,
          callback = function() self:toggleAutostart() end },
        { text_func = function() return "Display mode: " .. displayName(self.settings.display) end,
          callback = function() self:chooseDisplay() end },
        { text_func = function() return "Report URL (text): " .. self.settings.url end,
          callback = function() self:setURL("text") end },
        { text_func = function()
              return "EPUB manifest URL: " .. (self.settings.doc_url ~= "" and self.settings.doc_url or "(not set)")
          end,
          callback = function() self:setURL("doc") end },
        { text_func = function()
              return "News manifest URL: " .. (self.settings.news_url ~= "" and self.settings.news_url or "(not set)")
          end,
          callback = function() self:setURL("news") end },
        { text_func = function()
              return "Stocks manifest URL: " .. (self.settings.stocks_url ~= "" and self.settings.stocks_url or "(not set)")
          end,
          callback = function() self:setURL("stocks") end },
        { text_func = function()
              return "Image manifest URL: " .. (self.settings.image_url ~= "" and self.settings.image_url or "(not set)")
          end,
          callback = function() self:setURL("image") end },
        { text_func = function()
              return "Stop everything (emergency): " .. (self.settings.emergency_stop and "on" or "off")
          end,
          callback = function() self:toggleEmergencyStop() end },
        { text = "Status", callback = function() self:status() end },
        { text = "Check URLs (network)", callback = function() self:diagnose() end },
    } }
end

return Report
