-- SPDX-License-Identifier: AGPL-3.0-or-later
-- GitHub raw ↔ jsDelivr 的互相映射、多地址解析、错误翻译。
--
-- 为什么需要它：本仓库没有开 GitHub Pages，公开可取的只有
-- `raw.githubusercontent.com/<owner>/<repo>/<ref>/<path>`；这个域名在部分网络里
-- 连不上（DNS 污染 / 直连被拦），设备上就只看到一句 "network failure"。
--
-- 同一份文件还有两条路，按顺序兜底：
--     https://<owner>.github.io/<repo>/<path>          （仓库开了 Pages 才有；和 004 的
--                                                      报告同一个域名，能读 004 就说明这条路通）
--     https://cdn.jsdelivr.net/gh/<owner>/<repo>@<ref>/<path>   （外加 fastly/gcore/testingcf）
--
-- 反过来填 jsDelivr 或 Pages 地址时，也会回退到 raw。
--
-- 本模块是纯函数、不 require 任何东西，测试（tests/test_net_lua.py）直接 dofile 它。

local M = {}

local JSDELIVR_HOSTS = {
    "cdn.jsdelivr.net",
    "fastly.jsdelivr.net",
    "gcore.jsdelivr.net",
    "testingcf.jsdelivr.net",
}

-- 一次刷新最多试几个地址：镜像 4 个 + Pages + 用户自己写的地址，太多会把一轮刷新拖长。
M.MAX_MIRRORS = 6

local function stripQuery(url)
    return (url:gsub("%?.*$", ""))
end

local function splitPath(url)
    if type(url) ~= "string" then return nil end
    local host, path = url:match("^https://([^/]+)(/.*)$")
    if not host then return nil end
    return host:lower(), stripQuery(path)
end

--- 把 raw 或 jsDelivr 地址拆成 {owner, repo, ref, path}；别的地址返回 nil。
function M.githubParts(url)
    local host, path = splitPath(url)
    if not host then return nil end
    if host == "raw.githubusercontent.com" then
        -- /owner/repo/ref/剩下的路径
        local owner, repo, ref, rest = path:match("^/([^/]+)/([^/]+)/([^/]+)/(.*)$")
        if not owner then
            owner, repo, ref = path:match("^/([^/]+)/([^/]+)/([^/]+)$")
            rest = ""
        end
        if not owner then return nil end
        return { owner = owner, repo = repo, ref = ref, path = rest }
    end
    if host:match("jsdelivr%.net$") then
        -- /gh/owner/repo@ref/剩下的路径
        local owner, repo, ref, rest = path:match("^/gh/([^/]+)/([^/@]+)@([^/]+)/(.*)$")
        if not owner then
            owner, repo, ref = path:match("^/gh/([^/]+)/([^/@]+)@([^/]+)$")
            rest = ""
        end
        if not owner then return nil end
        return { owner = owner, repo = repo, ref = ref, path = rest }
    end
    return nil
end

local function rawURL(parts)
    return string.format("https://raw.githubusercontent.com/%s/%s/%s/%s",
        parts.owner, parts.repo, parts.ref, parts.path)
end

local function jsdelivrURL(host, parts)
    return string.format("https://%s/gh/%s/%s@%s/%s", host, parts.owner, parts.repo, parts.ref, parts.path)
end

-- 仓库的 Pages 地址。`owner.github.io` 这种用户站没有仓库名这一段。
local function pagesURL(parts)
    if parts.repo:lower() == (parts.owner:lower() .. ".github.io") then
        return string.format("https://%s.github.io/%s", parts.owner, parts.path)
    end
    return string.format("https://%s.github.io/%s/%s", parts.owner, parts.repo, parts.path)
end

--- 同一份文件的所有候选地址：原地址在最前，镜像随后（顺序＝尝试顺序）。
function M.mirrors(url)
    local list = {}
    if type(url) ~= "string" or url == "" then return list end
    list[1] = url
    local parts = M.githubParts(url)
    if not parts then return list end
    local host = select(1, splitPath(url))
    local isJsdelivr = host and host:match("jsdelivr%.net$")
    local candidates = {}
    if not isJsdelivr then
        -- Pages 排在 jsDelivr 前面：它和 004 的报告同一个域名，能读报告就说明这条路通；
        -- 没开 Pages 时只会是一次很快的 404。
        candidates[#candidates + 1] = pagesURL(parts)
        for _, candidate in ipairs(JSDELIVR_HOSTS) do
            candidates[#candidates + 1] = jsdelivrURL(candidate, parts)
        end
    else
        candidates[1] = rawURL(parts)
        candidates[2] = pagesURL(parts)
        for _, candidate in ipairs(JSDELIVR_HOSTS) do
            if candidate ~= host then candidates[#candidates + 1] = jsdelivrURL(candidate, parts) end
        end
    end
    local seen = { [url] = true }
    for _, candidate in ipairs(candidates) do
        if not seen[candidate] and #list < M.MAX_MIRRORS then
            seen[candidate] = true
            list[#list + 1] = candidate
        end
    end
    return list
end

--- 真正要试的地址序列：用户写的先试（按写的顺序），镜像随后兜底；去重并封顶。
function M.candidates(configured)
    local declared = M.splitURLs(configured)
    local list, seen = {}, {}
    local function add(url)
        if type(url) == "string" and url ~= "" and not seen[url] and #list < M.MAX_MIRRORS then
            seen[url] = true
            list[#list + 1] = url
        end
    end
    for _, url in ipairs(declared) do add(url) end
    for _, url in ipairs(declared) do
        for _, mirror in ipairs(M.mirrors(url)) do add(mirror) end
    end
    return list
end

--- 设置里可以写多个地址：空格、逗号、分号、换行都算分隔（只留 https）。
function M.splitURLs(text)
    local urls = {}
    for piece in tostring(text or ""):gmatch("[^%s,;]+") do
        if piece:match("^https://") then urls[#urls + 1] = piece end
    end
    return urls
end

--- jsDelivr 会缓存分支上的文件，加一个每分钟变一次的查询串尽量绕开。
--- 只加在真正发请求的地址上；记录的地址仍然是干净的，免得污染相对路径解析。
function M.bust(url, epoch)
    if type(url) ~= "string" or not url:match("^https://[^/]*jsdelivr%.net/") then return url end
    local stamp = math.floor((epoch or os.time()) / 60)
    return stripQuery(url) .. "?v=" .. tostring(stamp)
end

--- stocks.json 是网页全量数据；Kindle 多镜像探测改读同目录的小清单，省流量。
--- 自定义文件名保持原样，兼容用户自己的行情服务。
function M.compactManifest(url)
    if type(url) ~= "string" then return url end
    local clean = stripQuery(url)
    if clean:match("/stocks%.json$") then
        return (clean:gsub("/stocks%.json$", "/stocks-manifest.json"))
    end
    return url
end

-- UTC ISO-8601 秒数。这里重复一份小型 days-from-civil，保持 net.lua 仍是纯模块，
-- 测试不需要加载 KOReader。清单新鲜度比较绝不能依赖 Kindle 的本地时区。
local function utcEpoch(y, mo, d, h, mi, sec)
    y = (mo <= 2) and (y - 1) or y
    local era = math.floor(y / 400)
    local yoe = y - era * 400
    local mp = (mo + 9) % 12
    local doy = math.floor((153 * mp + 2) / 5) + d - 1
    local doe = yoe * 365 + math.floor(yoe / 4) - math.floor(yoe / 100) + doy
    return (era * 146097 + doe - 719468) * 86400 + h * 3600 + mi * 60 + sec
end

function M.isoSeconds(value)
    local y, mo, d, h, mi, sec = tostring(value or ""):match(
        "^(%d%d%d%d)-(%d%d)-(%d%d)T(%d%d):(%d%d):(%d%d)Z$")
    if not y then return nil end
    y, mo, d, h, mi, sec = tonumber(y), tonumber(mo), tonumber(d),
                           tonumber(h), tonumber(mi), tonumber(sec)
    if mo < 1 or mo > 12 or h > 23 or mi > 59 or sec > 59 then return nil end
    local month_days = { 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31 }
    if mo == 2 and (y % 400 == 0 or (y % 4 == 0 and y % 100 ~= 0)) then
        month_days[2] = 29
    end
    if d < 1 or d > month_days[mo] then return nil end
    return utcEpoch(y, mo, d, h, mi, sec)
end

--- 比较两份清单的新鲜度：先比真实数据时间，再比生成时间。
--- 返回 1 = a 更新，-1 = b 更新，0 = 无法区分。生成得晚但数据更旧不能胜出。
function M.compareFreshness(a, b)
    a, b = a or {}, b or {}
    local ad, bd = M.isoSeconds(a.data_time), M.isoSeconds(b.data_time)
    if ad and bd and ad ~= bd then return ad > bd and 1 or -1 end
    if ad and not bd then return 1 end
    if bd and not ad then return -1 end
    local ag, bg = M.isoSeconds(a.generated), M.isoSeconds(b.generated)
    if ag and bg and ag ~= bg then return ag > bg and 1 or -1 end
    if ag and not bg then return 1 end
    if bg and not ag then return -1 end
    return 0
end

--- 候选是否会让本地已接受快照倒退。floor 可只带 data_time/generated。
function M.isRollback(candidate, floor)
    if type(floor) ~= "table" then return false end
    local has_floor = M.isoSeconds(floor.data_time) or M.isoSeconds(floor.generated)
    if not has_floor then return false end
    return M.compareFreshness(candidate, floor) < 0
end

--- 把底层错误翻成一句能照着查的话；认不出就原样返回。
function M.hint(err)
    local text = tostring(err or ""):lower()
    if text == "" or text == "nil" then
        return "没有拿到任何响应：先看 Wi-Fi 是否真的连通，再看 KOReader 的 ca-bundle.crt 是否存在"
    end
    if text:match("timeout") then
        return "请求超时：换一个镜像地址试试（jsDelivr 或 raw 互换）"
    end
    if text:match("network failure") or text:match("refused") or text:match("reset")
            or text:match("unreachable") or text:match("resolve") or text:match("name") then
        return "连不上这个域名：通常是 DNS 被污染或该域名被拦，插件已自动改试镜像"
    end
    if text:match("certificate") or text:match("handshake") or text:match("verify")
            or text:match("tls") then
        return "TLS/证书失败：检查设备时间是否准确，以及 ca-bundle.crt 的日期"
    end
    if text:match("http 404") or text:match("http 403") or text:match("http 4") then
        return "服务器拒绝：URL 写错、文件还没发布，或被限流"
    end
    if text:match("http 5") then
        return "服务器错误：稍后会自动重试"
    end
    return err
end

return M
