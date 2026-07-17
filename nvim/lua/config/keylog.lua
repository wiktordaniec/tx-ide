-- Keymap usage telemetry -> ~/.local/share/nvim/keylog.jsonl (append-only).
-- Captures: resolved keymap invocations, : commands (+ failures), which-key
-- hesitations, macro recordings, and a per-launch snapshot of defined maps.
-- Content-sensitive data (typed text, search patterns, registers) is never logged.

local M = {}

local LOG = vim.fn.stdpath("data") .. "/keylog.jsonl"
local HASH_STATE = vim.fn.stdpath("data") .. "/keylog.snapshot-hash"
local MODES = { "n", "v", "x", "o" }

local pid = vim.fn.getpid()
local session = ""
local buf_lines = {}
local maps = {} -- [mode .. lhs] = desc
local prefixes = {} -- set of every proper prefix of every lhs
local pending = ""
local last_key_ns = 0

local emit -- forward declaration; defined below

-- single-key remaps of built-ins (LazyVim maps j->gj etc.) and mouse noise:
-- logging these as "keymap usage" would drown the signal
local IGNORE = { j = true, k = true, gj = true, gk = true, t = true, f = true, T = true, F = true, [";"] = true, [","] = true }
-- mouse: count click bursts (no positions); scroll/drag/release stay ignored
local clicks, click_timer = 0, nil
local function note_click()
  clicks = clicks + 1
  if not click_timer then
    click_timer = vim.uv.new_timer()
  end
  click_timer:stop()
  click_timer:start(3000, 0, vim.schedule_wrap(function()
    if clicks > 0 then
      emit({ type = "mouse", clicks = clicks, ft = vim.bo.filetype })
      clicks = 0
    end
  end))
end

local function is_noise(key)
  if key:find("LeftMouse") or key:find("RightMouse") or key:find("MiddleMouse") then
    if not key:find("Release") and not key:find("Drag") then note_click() end
    return true
  end
  return key:find("Mouse") or key:find("Scroll") or key:find("Drag") or key:find("Release")
end

-- raw normal-mode key stream (run of motions/operators between pauses);
-- lets an analyzer spot inefficiencies like jjjjjj or repeated dd
local raw = ""
local function flush_raw()
  if #raw >= 6 then
    emit({ type = "keys", seq = raw, ft = vim.bo.filetype })
  end
  raw = ""
end

emit = function(rec)
  rec.ts = os.time()
  rec.pid = pid
  rec.session = session
  rec.cwd = vim.fn.getcwd()
  buf_lines[#buf_lines + 1] = vim.json.encode(rec)
end

local function flush()
  if #buf_lines == 0 then return end
  local f = io.open(LOG, "a")
  if not f then return end
  f:write(table.concat(buf_lines, "\n"), "\n")
  f:close()
  buf_lines = {}
end

local function norm(lhs)
  return vim.fn.keytrans(vim.api.nvim_replace_termcodes(lhs, true, true, true))
end

local function build_maps()
  maps, prefixes = {}, {}
  for _, mode in ipairs(MODES) do
    for _, m in ipairs(vim.api.nvim_get_keymap(mode)) do
      local lhs = norm(m.lhs)
      maps[mode .. lhs] = m.desc or (m.rhs or "")
      local acc = ""
      -- prefixes at keytrans-token granularity (<...> tokens stay whole)
      local i = 1
      while i <= #lhs do
        local tok = lhs:match("^<[^<>]+>", i) or lhs:sub(i, i)
        acc = acc .. tok
        i = i + #tok
        if i <= #lhs then prefixes[mode .. acc] = true end
      end
    end
  end
end

local function snapshot()
  local list = {}
  for _, mode in ipairs(MODES) do
    for _, m in ipairs(vim.api.nvim_get_keymap(mode)) do
      list[#list + 1] = { m = mode, lhs = norm(m.lhs), desc = m.desc }
    end
  end
  table.sort(list, function(a, b) return (a.m .. a.lhs) < (b.m .. b.lhs) end)
  local payload = vim.json.encode(list)
  local hash = vim.fn.sha256(payload)
  local prev = ""
  local f = io.open(HASH_STATE, "r")
  if f then prev = f:read("*l") or ""; f:close() end
  if hash == prev then
    emit({ type = "maps_snapshot_ref", hash = hash, count = #list })
  else
    emit({ type = "maps_snapshot", hash = hash, maps = list })
    local w = io.open(HASH_STATE, "w")
    if w then w:write(hash); w:close() end
  end
end

local function on_key(_, typed)
  if typed == nil or typed == "" then return end
  local mode = vim.api.nvim_get_mode().mode:sub(1, 1)
  if mode ~= "n" and mode ~= "v" and mode ~= "x" and mode ~= "o" then
    pending = ""
    return
  end
  if mode == "v" then mode = "x" end
  local now = vim.uv.hrtime()
  if now - last_key_ns > 2e9 then
    pending = ""
    flush_raw()
  end
  last_key_ns = now
  local key = vim.fn.keytrans(typed)
  if is_noise(key) then return end
  if mode == "n" then
    raw = raw .. key
    if #raw > 300 then flush_raw() end
  end
  pending = pending .. key
  if maps[mode .. pending] or (mode == "x" and maps["v" .. pending]) then
    local desc = maps[mode .. pending] or maps["v" .. pending]
    if not IGNORE[pending] then
      emit({ type = "map", key = pending, desc = desc, mode = mode, ft = vim.bo.filetype })
    end
    pending = ""
  elseif not (prefixes[mode .. pending] or (mode == "x" and prefixes["v" .. pending])) then
    -- not a prefix of anything: maybe this key starts a new sequence
    pending = (prefixes[mode .. key] or maps[mode .. key]) and key or ""
    if maps[mode .. pending] then
      emit({ type = "map", key = pending, desc = maps[mode .. pending], mode = mode, ft = vim.bo.filetype })
      pending = ""
    end
  end
end

function M.setup()
  if vim.env.TMUX then
    local out = vim.fn.systemlist("tx whoami 2>/dev/null || tmux display-message -p '#S'")
    if vim.v.shell_error == 0 and out[1] then session = out[1] end
  end

  M.stats = { seen = 0, errors = 0, last_error = nil }
  local ok = pcall(vim.on_key, function(k, t)
    M.stats.seen = M.stats.seen + 1
    local fine, err = pcall(on_key, k, t)
    if not fine then
      M.stats.errors = M.stats.errors + 1
      M.stats.last_error = tostring(err)
      if M.stats.errors <= 3 then
        -- record the failure in the log itself; never kill the logger
        buf_lines[#buf_lines + 1] = vim.json.encode({ type = "error", ts = os.time(), pid = pid, err = tostring(err) })
      end
    end
  end, vim.api.nvim_create_namespace("keylog"))
  if not ok then return end

  local aug = vim.api.nvim_create_augroup("keylog", { clear = true })

  -- : commands + failure detection
  vim.api.nvim_create_autocmd("CmdlineLeave", {
    group = aug,
    callback = function()
      if vim.v.event.abort or vim.fn.getcmdtype() ~= ":" then return end
      local cmd = vim.fn.getcmdline()
      if cmd == "" or #cmd < 2 then return end
      vim.v.errmsg = ""
      vim.schedule(function()
        emit({ type = "cmd", cmd = cmd, failed = vim.v.errmsg ~= "" or nil, ft = vim.bo.filetype })
      end)
    end,
  })

  -- which-key hesitation: the panel opening means the prefix wasn't muscle memory
  vim.api.nvim_create_autocmd("FileType", {
    group = aug,
    pattern = { "which_key", "WhichKey", "wk" },
    callback = function()
      emit({ type = "whichkey", pending = pending })
    end,
  })

  -- macro recordings: repeated similar macros = a workflow wanting a mapping
  vim.api.nvim_create_autocmd("RecordingLeave", {
    group = aug,
    callback = function()
      local reg = vim.fn.reg_recording()
      if reg ~= "" then
        emit({ type = "macro", reg = reg, len = #vim.fn.getreg(reg) })
      end
    end,
  })

  vim.api.nvim_create_autocmd("VimLeavePre", { group = aug, callback = function()
    flush_raw()
    flush()
  end })
  -- close a raw run when leaving normal mode (entering insert, cmdline, etc.)
  vim.api.nvim_create_autocmd("ModeChanged", {
    group = aug,
    pattern = "n:*",
    callback = flush_raw,
  })

  -- build map table + snapshot once plugins have registered their keymaps
  vim.defer_fn(function()
    build_maps()
    snapshot()
  end, 2000)

  -- periodic: flush the buffer, refresh maps (lazy-loaded plugins add maps late)
  local timer = vim.uv.new_timer()
  timer:start(5000, 15000, vim.schedule_wrap(function()
    -- close out an idle raw run (no keypress will come to trigger it)
    if raw ~= "" and vim.uv.hrtime() - last_key_ns > 2e9 then
      flush_raw()
    end
    build_maps()
    flush()
  end))
end

return M
