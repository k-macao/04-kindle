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
local Screen = Device.screen
local DEFAULT_URL = "https://k-macao.github.io/004/kindle.txt"
local MAX_BYTES = 2 * 1024 * 1024
local MANIFEST_MAX = 256 * 1024
local PAGE_MAX = 4 * 1024 * 1024
local PAGES_TOTAL_MAX = 64 * 1024 * 1024
local MAX_PAGES = 60
local DOC_MAX = 8 * 1024 * 1024
local DOC_PREFIX = "octopus004-report-"
local INTERVAL = 60
local WIFI_WAIT = 90

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

local function displayName(mode)
    if mode == "image" then return "page images" end
    if mode == "doc" then return "epub" end
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
    if self:isDocMode() then
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
function Report:httpGet(url, max_bytes, extra_headers)
    if type(url) ~= "string" or not url:match("^https://") then return nil, "URL must use HTTPS", nil end
    local ca = dataDir() .. "/ca-bundle.crt"
    if lfs.attributes(ca, "mode") ~= "file" then return nil, "KOReader CA bundle not found", nil end
    local https = require("ssl.https")
    https.TIMEOUT = 25
    local headers = { ["User-Agent"] = "KOReader-004/0.2", ["Cache-Control"] = "no-cache" }
    if extra_headers then
        for k, v in pairs(extra_headers) do headers[k] = v end
    end
    local chunks, size, too_large = {}, 0, false
    local ok, code, response_headers = https.request{
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
    code = tonumber(code)
    if too_large then return nil, "Response exceeds limit", code end
    if not ok then return nil, tostring(code or "network failure"), code end
    if code ~= 200 then return nil, "HTTP " .. tostring(code), code, response_headers end
    return table.concat(chunks), nil, code, response_headers
end

-- Text mode: download the plain-text report, keep the last good copy on failure.
function Report:fetchText()
    local headers = {}
    if self.settings.etag and self:readCache() then headers["If-None-Match"] = self.settings.etag end
    local body, err, code, response_headers = self:httpGet(self.settings.url, MAX_BYTES, headers)
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
    self.settings.etag = response_headers and (response_headers.etag or response_headers.ETag) or nil
    return body, nil, true
end

-- Image mode: a JSON manifest lists device-sized page images.
function Report:fetchManifest()
    if self.settings.image_url == "" then return nil, "Set the image manifest URL first" end
    local body, err = self:httpGet(self.settings.image_url, MANIFEST_MAX)
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

function Report:fetchDocManifest()
    if self.settings.doc_url == "" then return nil, "Set the EPUB manifest URL first" end
    local body, err = self:httpGet(self.settings.doc_url, MANIFEST_MAX)
    if not body then return nil, err end
    local decoded
    local ok = pcall(function() decoded = require("rapidjson").decode(body) end)
    if not ok or type(decoded) ~= "table" then return nil, "Invalid manifest JSON" end
    if type(decoded.doc) ~= "string" or decoded.doc == "" then return nil, "Manifest has no doc entry" end
    if type(decoded.generated) ~= "string" or decoded.generated == "" then
        return nil, "Manifest has no generated stamp"
    end
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
            local checked = self.last_check and os.date("%m-%d %H:%M", self.last_check) or "--"
            self.header:setText("004 | " .. state .. " | " .. checked .. " | tap")
            if self.dashboard then UIManager:setDirty(self.dashboard, "full") end
        end
        self.wake:kindle_hold(false)
        if error_message and (interactive or self:isImageMode() or self:isDocMode()) then
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
        if self.wake:mode(true) == "rtc" and not interactive and not self:isDocMode() then
            self.wake:sleep_now()
        end
    end
    watchdog = function() finish("Wi-Fi connection timed out") end
    UIManager:scheduleIn(WIFI_WAIT, watchdog)
    local ok = NetworkMgr:turnOnWifiAndWaitForConnection(function()
        if gen ~= self.gen then return end
        local success, err = pcall(function()
            if self:isDocMode() then self:refreshDoc()
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
function Report:docPath(name)
    return dataDir() .. "/" .. (name or self.settings.doc_file_name or (DOC_PREFIX .. "1.epub"))
end

function Report:docIsOpen()
    local current = self.ui and self.ui.document and self.ui.document.file
    return current ~= nil and current == self.settings.doc_file
end

function Report:turnDocPage()
    if not self:docIsOpen() then return end
    -- Give the freshly opened book a full interval before the first turn.
    local opened = tonumber(self.settings.doc_opened_at)
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

function Report:openDoc(path)
    self.settings.doc_file = path
    self.settings.doc_opened_at = os.time()
    self:save()
    -- showReader is documented as the only safe way to build a new ReaderUI
    -- instance; it closes the current one, so our widget instance is replaced.
    require("apps/reader/readerui"):showReader(path)
end

function Report:refreshDoc()
    local manifest, err = self:fetchDocManifest()
    if not manifest then error(err) end
    local sig = manifest.generated
    if type(manifest.data_time) == "string" then self.settings.data_time = manifest.data_time end
    local current = self.settings.doc_file
    local have = current and lfs.attributes(current, "mode") == "file"
    self.last_check = os.time()
    if sig == tostring(self.settings.doc_sig or "") and have then
        self:save()
        self:turnDocPage()
        return
    end
    local url = resolveURL(self.settings.doc_url, manifest.doc)
    if not url:match("^https://") then error("Document URL is not HTTPS") end
    local body, download_error = self:httpGet(url, DOC_MAX)
    if not body then error(download_error) end
    if #body < 512 or body:sub(1, 2) ~= "PK" then error("Downloaded file is not an EPUB") end
    -- A fresh file name every version: KOReader then opens it at page 1 instead
    -- of resuming the previous position.
    local seq = (tonumber(self.settings.doc_seq) or 0) + 1
    local name = string.format("%s%d.epub", DOC_PREFIX, seq)
    local target = self:docPath(name)
    local temp = target .. ".tmp"
    local f = io.open(temp, "wb")
    if not f then error("Cannot write document cache") end
    local wrote = f:write(body)
    local closed = f:close()
    if not wrote or not closed or not os.rename(temp, target) then
        os.remove(temp); error("Cannot save document")
    end
    self.settings.doc_seq = seq
    self.settings.doc_file_name = name
    self.settings.doc_sig = sig
    self:pruneDocs(name)
    self:save()
    self:openDoc(target)
end

function Report:pruneDocs(keep)
    local dir = dataDir()
    local ok, iter, state = pcall(lfs.dir, dir)
    if not ok or type(iter) ~= "function" then return end
    for entry in iter, state do
        if type(entry) == "string" and entry:sub(1, #DOC_PREFIX) == DOC_PREFIX
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

-- "2026-11-02T10:09:05Z" -> epoch-ish seconds, or nil when unparsable.
local function isoSeconds(iso)
    local y, mo, d, h, mi, sec =
        tostring(iso):match("^(%d%d%d%d)-(%d%d)-(%d%d)T(%d%d):(%d%d):(%d%d)Z$")
    if not y then return nil end
    return os.time{ year = tonumber(y), month = tonumber(mo), day = tonumber(d),
                    hour = tonumber(h), min = tonumber(mi), sec = tonumber(sec) }
end

function Report:status()
    local mode = self.wake:mode(true) == "rtc" and "hardware wake" or "awake timer"
    local checked = self.last_check and os.date("%Y-%m-%d %H:%M:%S", self.last_check) or "never"
    local data = ""
    local stamp = tostring(self.settings.data_time or "")
    if stamp ~= "" then
        local then_time = isoSeconds(stamp)
        local age = nil
        if then_time then
            -- os.time(os.date("!*t")) is "now" in the same (local) scale.
            age = (os.time(os.date("!*t")) - then_time) / 3600
        end
        data = "\nData: " .. stamp:gsub("T", " "):gsub("Z$", " UTC")
        if age and age >= 24 then
            data = data .. string.format("  (stale %.0fd)", age / 24)
        elseif age and age < 0 then
            data = data .. "  (future?)"
        end
    end
    local source
    if self:isDocMode() then
        source = "epub: " .. tostring(self.settings.doc_url)
    elseif self:isImageMode() then
        source = "images: " .. tostring(self.settings.image_url)
    else
        source = "text: " .. self.settings.url
    end
    local page = ""
    if self:isDocMode() then
        page = "\nEPUB: " .. tostring(self.settings.doc_file_name or "-")
            .. (self:docIsOpen() and " (open)" or " (closed)")
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
                if url ~= "" and not url:match("^https://") then self:note("HTTPS URL required"); return end
                self.settings[spec.key] = url
                self.settings[spec.clear] = nil
                if kind == "doc" then self.settings.doc_sig = nil end
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
    if self:isDocMode() then
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
              return "Image manifest URL: " .. (self.settings.image_url ~= "" and self.settings.image_url or "(not set)")
          end,
          callback = function() self:setURL("image") end },
        { text = "Status", callback = function() self:status() end },
    } }
end

return Report
