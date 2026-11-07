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
local MAX_BYTES = 2 * 1024 * 1024
local MANIFEST_MAX = 256 * 1024
local PAGE_MAX = 4 * 1024 * 1024
local PAGES_TOTAL_MAX = 64 * 1024 * 1024
local MAX_PAGES = 60
local DOC_MAX = 8 * 1024 * 1024
local DOC_PREFIX = "octopus004-report-"
-- 每日新闻是第二本书：同一个下载/校验/翻页/清理流程，换一套设置键与前缀，
-- 两份文档互不覆盖（各自的 sig 变了才重下，各自的旧文件各自清）。
local NEWS_PREFIX = "octopus004-news-"
local DOC_KINDS = {
    doc  = { manifest = "doc_url",  sig = "doc_sig",  seq = "doc_seq",  file = "doc_file",
             file_name = "doc_file_name", opened_at = "doc_opened_at", prefix = DOC_PREFIX,
             data_time = "data_time", data_time_kind = "data_time_kind",
             label = "EPUB" },
    news = { manifest = "news_url", sig = "news_sig", seq = "news_seq", file = "news_file",
             file_name = "news_file_name", opened_at = "news_opened_at", prefix = NEWS_PREFIX,
             data_time = "news_data_time", data_time_kind = "news_data_time_kind",
             label = "NEWS" },
}
local INTERVAL = 60
local WIFI_WAIT = 90
-- 设备上显示的时间统一为北京时间（UTC+8），与设备本地时区无关。
local BEIJING_OFFSET = 8 * 3600

local Report = WidgetContainer:extend{ name = "octopus004", is_doc_only = false }

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

function Report:init()
    self.store = LuaSettings:open(DataStorage:getSettingsDir() .. "/octopus004.lua")
    self.settings = self.store.data or {}
    if self.settings.url == nil then self.settings.url = DEFAULT_URL end
    if self.settings.image_url == nil then self.settings.image_url = "" end
    if self.settings.display == nil then self.settings.display = "text" end
    if self.settings.doc_url == nil then self.settings.doc_url = "" end
    if self.settings.news_url == nil then self.settings.news_url = "" end
    if self.settings.enabled == nil then self.settings.enabled = false end
    self.wake = Wake.new({ Device = Device, UIManager = UIManager, logger = logger })
    self.refresh_task = function() self:refresh(false) end
    self.cache_path = dataDir() .. "/octopus004-report.txt"
    self.image_dir = dataDir() .. "/octopus004-pages"
    self.page_index = 1
    self.page_count = tonumber(self.settings.image_count) or 0
    if self.ui and self.ui.menu then self.ui.menu:registerToMainMenu(self) end
    if self.settings.enabled then UIManager:scheduleIn(3, function() self:start() end) end
end

function Report:save()
    for k, v in pairs(self.settings) do self.store:saveSetting(k, v) end
    self.store:flush()
end

function Report:note(text) UIManager:show(InfoMessage:new{ text = text, timeout = 5 }) end

function Report:isImageMode() return self.settings.display == "image" end
function Report:isDocMode() return self.settings.display == "doc" end
function Report:isNewsMode() return self.settings.display == "news" end
-- 两种 EPUB 来源共用同一套「下载书 → 交给阅读器」的流程。
function Report:isBookMode() return self:isDocMode() or self:isNewsMode() end
function Report:docKind() return self:isNewsMode() and "news" or "doc" end
function Report:docSpec() return DOC_KINDS[self:docKind()] end

local function displayName(mode)
    if mode == "image" then return "page images" end
    if mode == "doc" then return "epub" end
    if mode == "news" then return "news" end
    return "text"
end

function Report:readCache()
    local f = io.open(self.cache_path, "rb")
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

function Report:start()
    self.settings.enabled = true; self:save(); self:holdScreen()
    if self.wake:mode(true) == "awake" then self.wake:hold_awake() end
    if self:isBookMode() then
        -- the reader shows the document; this widget stays out of the way
    elseif self:isImageMode() then
        if not self.dashboard and self:hasPage(self.page_index) then self:showPage(false) end
    else
        local cached = self:readCache()
        if cached and not self.dashboard then self:showText(cached, false) end
    end
    self:refresh(true)
end

function Report:stop()
    self.settings.enabled = false; self:save()
    self.gen = (self.gen or 0) + 1
    self.in_cycle = false
    self.wake:cancel(); self.wake:kindle_hold(false); self.wake:release_awake()
    self:releaseScreen()
    if self.dashboard then
        UIManager:close(self.dashboard)
        self.dashboard, self.box, self.header, self.page_widget = nil, nil, nil, nil
    end
    UIManager:setDirty("all", "full")
end

function Report:onCloseWidget()
    -- Preserve enabled state for next KOReader launch, but restore the user's screensaver.
    self.gen = (self.gen or 0) + 1
    self.wake:cancel(); self.wake:kindle_hold(false); self.wake:release_awake()
    self:releaseScreen()
    if self.dashboard then UIManager:close(self.dashboard); self.dashboard = nil end
end

function Report:onResume()
    if self.settings.enabled and self.next_due and os.time() >= self.next_due - 5 and not self.in_cycle then
        UIManager:scheduleIn(2, self.refresh_task)
    end
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
                " 页页面图片…\n首次需要下载全部页面，之后每 60 秒只检查是否有更新。", false)
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

function Report:refresh(interactive)
    if self.in_cycle or not self.settings.enabled then return end
    self.in_cycle = true; self.wake:cancel(); self.wake:kindle_hold(true)
    self.gen = (self.gen or 0) + 1
    local gen = self.gen
    local watchdog
    local function finish(error_message)
        if gen ~= self.gen then return end
        UIManager:unschedule(watchdog)
        self.gen = self.gen + 1; self.in_cycle = false; self.last_error = error_message
        if self.header then
            local state = error_message and "OFFLINE / CACHED" or "ONLINE"
            local checked = self.last_check and beijing(self.last_check, "%m-%d %H:%M") or "--"
            self.header:setText("004 | " .. state .. " | " .. checked .. " | tap")
            if self.dashboard then UIManager:setDirty(self.dashboard, "full") end
        end
        self.wake:kindle_hold(false)
        if error_message and (interactive or self:isImageMode() or self:isBookMode()) then
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
        -- Keep Wi-Fi on: reconnecting every minute is slow and may miss cycles.
        if self.wake:mode(true) == "rtc" and not interactive and not self:isBookMode() then
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
    local header = TextWidget:new{ text = "004  |  60s  |  tap for menu", face = Font:getFace("smallinfofont") }
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
    self.settings[spec.file] = path
    self.settings[spec.opened_at] = os.time()
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
    local ok, iter, state = pcall(lfs.dir, dir)
    if not ok or type(iter) ~= "function" then return end
    for entry in iter, state do
        if type(entry) == "string" and prefix and entry:sub(1, #prefix) == prefix
                and entry ~= keep then
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

-- 逐个地址单独试一遍（短超时），把结果摊在屏幕上：设备链路上到底哪个地址可用。
function Report:diagnose()
    local spec = self:docSpec()
    local urls = self:urlCandidates(self.settings[spec.manifest] or "")
    if #urls == 0 then self:note("Fill the manifest URL first"); return end
    local lines = { "Manifest: " .. tostring(self.settings[spec.manifest]) }
    for _, url in ipairs(urls) do
        local started = os.time()
        local body, err = self:httpGet(Net.bust(url), MANIFEST_MAX, nil, 10)
        local spent = os.time() - started
        local status = body and string.format("OK %d B", #body)
            or string.format("%s (%ds)", tostring(err), spent)
        lines[#lines + 1] = url:gsub("^https://", "") .. "\n  " .. status
    end
    lines[#lines + 1] = "Tap Refresh now to retry."
    self:note(table.concat(lines, "\n"))
end

function Report:status()
    local mode = self.wake:mode(true) == "rtc" and "hardware wake" or "awake timer"
    local checked = self.last_check and (beijing(self.last_check, "%Y-%m-%d %H:%M:%S") .. " (Beijing)") or "never"
    local data = ""
    local spec = self:docSpec()
    -- 不能写成 `a and b or c`：新闻那边还没有数据时间时 b 是 nil，会回落到报告的时间。
    local stamp_setting, kind_setting
    if self:isBookMode() then
        stamp_setting, kind_setting = self.settings[spec.data_time], self.settings[spec.data_time_kind]
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
    self:note("004 (" .. self.settings.display .. ")\n" .. source .. "\n" .. mode .. page .. data ..
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
                self:save()
                UIManager:close(dialog)
            end } } },
    }
    UIManager:show(dialog); dialog:onShowKeyboard()
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
        { { text = "Page images", callback = function() pick("image") end } },
    } }
    UIManager:show(dialog)
end

function Report:menu()
    local dialog
    dialog = ButtonDialog:new{ buttons = {
        { { text = "Next page", callback = function() UIManager:close(dialog); self:advance() end },
          { text = "Refresh now", callback = function() UIManager:close(dialog); self:refresh(true) end } },
        { { text = "Display mode", callback = function() UIManager:close(dialog); self:chooseDisplay() end },
          { text = "Status", callback = function() UIManager:close(dialog); self:status() end } },
        { { text = "Stop", callback = function() UIManager:close(dialog); self:stop() end } },
    } }
    UIManager:show(dialog)
end

function Report:addToMainMenu(items)
    items.octopus004 = { text = "004 Report", sorting_hint = "tools", sub_item_table = {
        { text_func = function() return self.settings.enabled and "Stop report" or "Show report" end,
          callback = function() if self.settings.enabled then self:stop() else self:start() end end },
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
              return "Image manifest URL: " .. (self.settings.image_url ~= "" and self.settings.image_url or "(not set)")
          end,
          callback = function() self:setURL("image") end },
        { text = "Status", callback = function() self:status() end },
        { text = "Check URLs (network)", callback = function() self:diagnose() end },
    } }
end

return Report
