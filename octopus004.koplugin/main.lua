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
local InfoMessage = require("ui/widget/infomessage")
local ButtonDialog = require("ui/widget/buttondialog")
local InputDialog = require("ui/widget/inputdialog")
local WidgetContainer = require("ui/widget/container/widgetcontainer")
local logger = require("logger")
local lfs = require("libs/libkoreader-lfs")
local _ = require("gettext")
local dir = (debug.getinfo(1, "S").source:match("^@(.+)$") or ""):match("(.*/)") or "./"
local Wake = dofile(dir .. "wake.lua")
local Screen = Device.screen
local DEFAULT_URL = "https://k-macao.github.io/004/kindle.txt"
local MAX_BYTES = 2 * 1024 * 1024
local INTERVAL = 60
local WIFI_WAIT = 90

local Report = WidgetContainer:extend{ name = "octopus004", is_doc_only = false }
function Report:init()
    self.store = LuaSettings:open(DataStorage:getSettingsDir() .. "/octopus004.lua")
    self.settings = self.store.data or {}
    if self.settings.url == nil then self.settings.url = DEFAULT_URL end
    if self.settings.enabled == nil then self.settings.enabled = false end
    self.wake = Wake.new({ Device = Device, UIManager = UIManager, logger = logger })
    self.refresh_task = function() self:refresh(false) end
    self.cache_path = DataStorage:getDataDir() .. "/octopus004-report.txt"
    if self.ui and self.ui.menu then self.ui.menu:registerToMainMenu(self) end
    if self.settings.enabled then UIManager:scheduleIn(3, function() self:start() end) end
end
function Report:save()
    for k, v in pairs(self.settings) do self.store:saveSetting(k, v) end
    self.store:flush()
end
function Report:note(text) UIManager:show(InfoMessage:new{ text = text, timeout = 5 }) end
function Report:readCache()
    local f = io.open(self.cache_path, "rb")
    if not f then return nil end
    local s = f:read(MAX_BYTES + 1); f:close()
    if not s or #s == 0 or #s > MAX_BYTES then return nil end
    return s
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
    local cached = self:readCache()
    if cached and not self.dashboard then self:showText(cached, false) end
    self:refresh(true)
end
function Report:stop()
    self.settings.enabled = false; self:save()
    self.gen = (self.gen or 0) + 1
    self.in_cycle = false
    self.wake:cancel(); self.wake:kindle_hold(false); self.wake:release_awake()
    self:releaseScreen()
    if self.dashboard then UIManager:close(self.dashboard); self.dashboard = nil; self.box = nil; self.header = nil end
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

-- Download to memory with a strict cap; reject HTML error pages and keep the
-- last good report on failure. Never silently disable TLS verification.
function Report:fetch()
    if not self.settings.url:match("^https://") then return nil, "URL must use HTTPS" end
    local ca = DataStorage:getDataDir() .. "/ca-bundle.crt"
    if lfs.attributes(ca, "mode") ~= "file" then return nil, "KOReader CA bundle not found" end
    local https = require("ssl.https")
    https.TIMEOUT = 25
    local chunks, size, too_large = {}, 0, false
    local headers = { ["User-Agent"] = "KOReader-004/0.1", ["Cache-Control"] = "no-cache" }
    if self.settings.etag and self:readCache() then headers["If-None-Match"] = self.settings.etag end
    local ok, code, response_headers = https.request{
        url = self.settings.url, method = "GET", headers = headers,
        protocol = "any", options = { "all", "no_sslv2", "no_sslv3", "no_tlsv1", "no_tlsv1_1" },
        verify = "peer", cafile = ca, redirect = false,
        sink = function(chunk)
            if chunk then
                size = size + #chunk
                if size > MAX_BYTES then too_large = true; return nil, "report too large" end
                chunks[#chunks + 1] = chunk
            end
            return 1
        end,
    }
    if too_large then return nil, "Report exceeds 2 MiB" end
    if not ok then return nil, tostring(code or "network failure") end
    code = tonumber(code)
    if code == 304 then
        local cached = self:readCache()
        if cached then return cached, nil, false end
        self.settings.etag = nil
        return nil, "Cache missing; retrying"
    end
    if code ~= 200 then return nil, "HTTP " .. tostring(code) end
    local body = table.concat(chunks)
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
        if error_message and interactive then self:note("004: " .. error_message) end
        if error_message and self.box then self:advance() end
        self.next_due = os.time() + INTERVAL
        self.wake:schedule(INTERVAL, self.refresh_task, self.wake:mode(true))
        -- Keep Wi-Fi on: reconnecting every minute is slow and may miss cycles.
        if self.wake:mode(true) == "rtc" and not interactive then self.wake:sleep_now() end
    end
    watchdog = function() finish("Wi-Fi connection timed out") end
    UIManager:scheduleIn(WIFI_WAIT, watchdog)
    local ok = NetworkMgr:turnOnWifiAndWaitForConnection(function()
        if gen ~= self.gen then return end
        local success, err = pcall(function()
            local body, fetch_error = self:fetch()
            if not body then error(fetch_error) end
            self.last_check = os.time(); self:save()
            if not self.box then self:showText(body, false)
            elseif self.current_text ~= body then self:showText(body, true)
            else self:advance() end
        end)
        if success then finish(nil) else finish(tostring(err)) end
    end)
    if ok == false then finish("Wi-Fi could not be started") end
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
    local plugin = self
    local screen = InputContainer:new{ dimen = Geom:new{ x = 0, y = 0, w = w, h = h }, frame }
    screen.ges_events = { Tap = { GestureRange:new{ ges = "tap", range = Geom:new{ x = 0, y = 0, w = w, h = h } } } }
    screen.onTap = function() plugin:menu() return true end
    self.box, self.dashboard, self.current_text, self.header = box, screen, text, header
    UIManager:show(screen); UIManager:setDirty(screen, "full")
    if old then UIManager:close(old) end
end
function Report:advance()
    if not self.box or not self.dashboard then return end
    if self.box.virtual_line_num + self.box:getVisLineCount() > self.box:getAllLineCount() then
        self.box:scrollToTop()
    else self.box:scrollDown() end
    UIManager:setDirty(self.dashboard, "full")
end
function Report:status()
    local mode = self.wake:mode(true) == "rtc" and "hardware wake" or "awake timer"
    local checked = self.last_check and os.date("%Y-%m-%d %H:%M:%S", self.last_check) or "never"
    self:note("004\n" .. self.settings.url .. "\n" .. mode .. "\nLast check: " .. checked .. "\n" .. (self.last_error or "No error"))
end
function Report:setURL()
    local dialog
    dialog = InputDialog:new{
        title = "004 report HTTPS URL", input = self.settings.url,
        buttons = { { { text = "Cancel", callback = function() UIManager:close(dialog) end },
            { text = "Save", callback = function()
                local url = dialog:getInputText():match("^%s*(.-)%s*$")
                if not url:match("^https://") then self:note("HTTPS URL required"); return end
                self.settings.url = url; self.settings.etag = nil; self:save()
                UIManager:close(dialog)
            end } } },
    }
    UIManager:show(dialog); dialog:onShowKeyboard()
end
function Report:menu()
    local dialog
    dialog = ButtonDialog:new{ buttons = {
        { { text = "Next page", callback = function() UIManager:close(dialog); self:advance() end },
          { text = "Refresh now", callback = function() UIManager:close(dialog); self:refresh(true) end } },
        { { text = "Status", callback = function() UIManager:close(dialog); self:status() end },
          { text = "Stop", callback = function() UIManager:close(dialog); self:stop() end } },
    } }
    UIManager:show(dialog)
end
function Report:addToMainMenu(items)
    items.octopus004 = { text = "004 Report", sorting_hint = "tools", sub_item_table = {
        { text_func = function() return self.settings.enabled and "Stop report" or "Show report" end,
          callback = function() if self.settings.enabled then self:stop() else self:start() end end },
        { text = "Report URL", callback = function() self:setURL() end },
        { text = "Status", callback = function() self:status() end },
    } }
end
return Report
