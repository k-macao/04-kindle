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
