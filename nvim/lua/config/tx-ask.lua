-- Ask a live tx llm session about the code under the cursor.
--
--   <leader>at  pick the target session (tag siblings first, tags as chips)
--   <leader>ac  ask it, with file:line plus the visual selection or the cursor word
--
-- Delivery goes through `tx send-user-message`, which wraps the body in the
-- <from-user session="…"> operator envelope -- deliberately NOT `tx send-message`,
-- which hard-wraps <from-agent> and would misattribute the operator as a peer agent.
-- tx fills the session attribute from $TX_SESSION_ID (exported into every tx
-- session), so a question keeps the editor it was asked from rather than the
-- hosting view. The argv form of vim.system runs no shell, so a question
-- containing quotes or $ reaches the target verbatim.
--
-- Session discovery is a tmux sweep joined to the durable records by @tx_id only
-- because tx has no `sessions --json` verb yet; that read collapses to one call
-- when it lands.

local HOME = vim.env.HOME
local FALLBACK = "nvim-helper"

local function read_record(id)
  local f = io.open(HOME .. "/.tx-ide/sessions/" .. id .. ".json")
  if not f then return nil end
  local ok, rec = pcall(vim.json.decode, f:read("*a"))
  f:close()
  return ok and rec or nil
end

-- live tmux sessions -> durable records, keyed by @tx_id
local function live_records()
  local out = {}
  for _, s in ipairs(vim.fn.systemlist("tmux list-sessions -F '#S'")) do
    local id = (vim.fn.systemlist("tmux show-option -t " .. s .. " -qv @tx_id")[1] or "")
    if id ~= "" then
      local rec = read_record(id)
      if rec then out[#out + 1] = rec end
    end
  end
  return out
end

local function me()
  return vim.env.TX_SESSION_ID and read_record(vim.env.TX_SESSION_ID) or nil
end

-- llm sessions only (nvim/shell cannot read messages), self excluded,
-- sessions sharing one of my scope tags sorted first
local function targets()
  local self_rec = me() or {}
  local my_tags = self_rec.tags or {}
  local list = {}
  for _, r in ipairs(live_records()) do
    if r.role == "llm" and r.name ~= self_rec.name then
      local shared = false
      for _, t in ipairs(r.tags or {}) do
        if vim.tbl_contains(my_tags, t) then shared = true end
      end
      list[#list + 1] = { name = r.name, tags = r.tags or {}, sibling = shared }
    end
  end
  table.sort(list, function(a, b)
    if a.sibling ~= b.sibling then return a.sibling end
    return a.name < b.name
  end)
  return list
end

local function resolve_target()
  if vim.g.tx_ask_target then return vim.g.tx_ask_target end
  local t = targets()[1]
  if t and t.sibling then return t.name end   -- tag sibling = the worker this companion serves
  return FALLBACK
end

local function send(target, body)
  vim.system({ "tx", "send-user-message", target, body }, { text = true }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        local why = vim.trim(result.stderr or "")
        vim.notify("tx send-user-message failed: " .. why, vim.log.levels.ERROR)
      else
        vim.notify("sent to " .. target, vim.log.levels.INFO)
      end
    end)
  end)
end

-- <leader>at : choose the target session
vim.keymap.set("n", "<leader>at", function()
  local list = targets()
  if #list == 0 then return vim.notify("no live llm sessions", vim.log.levels.WARN) end
  vim.ui.select(list, {
    prompt = "Send questions to:",
    format_item = function(s)
      local chips = {}
      for _, t in ipairs(s.tags) do chips[#chips + 1] = "[" .. t .. "]" end
      return ("%s%-32s %s"):format(s.sibling and "▸ " or "  ", s.name, table.concat(chips, " "))
    end,
  }, function(choice)
    if not choice then return end
    vim.g.tx_ask_target = choice.name
    vim.notify("target: " .. choice.name, vim.log.levels.INFO)
  end)
end, { desc = "Pick tx session to ask" })

-- <leader>ac : ask the current target about cursor/selection context.
-- The prompt names the target on purpose -- no blind misfires.
vim.keymap.set({ "n", "x" }, "<leader>ac", function()
  local target = resolve_target()
  local mode = vim.fn.mode()
  local file = vim.fn.expand("%:.")
  local ctx
  if mode:match("[vV\022]") then
    local s, e = vim.fn.getpos("v"), vim.fn.getpos(".")
    local lines = vim.fn.getregion(s, e, { type = mode })
    ctx = ("%s:%d-%d | %s"):format(file, math.min(s[2], e[2]), math.max(s[2], e[2]), table.concat(lines, "\\n"))
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "n", false)
  else
    -- expand("<cword>") RAISES E348 on a blank or whitespace-only line, which is most of the
    -- lines in a file -- there is no word to name, and file:line is context enough on its own.
    local found, word = pcall(vim.fn.expand, "<cword>")
    ctx = ("%s:%d"):format(file, vim.fn.line("."))
    if found and word ~= "" then ctx = ctx .. (" | word: %s"):format(word) end
  end
  vim.ui.input({ prompt = ("Ask %s: "):format(target) }, function(q)
    if not q or q == "" then return end
    -- Messages are single-line (COMMON.md) -- any real newline becomes a literal \n.
    local body = ("Q: %s | ctx: %s"):format(q, ctx)
    send(target, (body:gsub("\n", "\\n")))
  end)
end, { desc = "Ask current tx target about this" })
