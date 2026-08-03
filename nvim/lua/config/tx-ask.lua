-- Ask a live tx llm session about the code under the cursor.
--
--   <leader>at  pick the target session (tag siblings first, tags as chips)
--   <leader>aC  queue a question here, to send with the rest later
--   <leader>ac  ask it, with file:line plus the visual selection or the cursor word
--               -- and any queued questions ahead of it
--
-- Queued questions are ephemeral: a virtual "? Qn queued" marker sits at the end
-- of the line, and nothing is ever written to the file -- that is what AINotes are
-- for. Sending is the ONLY thing that empties the queue, so a mistyped or
-- abandoned prompt can never cost the operator questions they had banked.
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

-- on_sent runs only when tx accepted the message, so a failed send leaves the
-- queue intact rather than swallowing questions the operator has to retype
local function send(target, body, on_sent)
  vim.system({ "tx", "send-user-message", target, body }, { text = true }, function(result)
    vim.schedule(function()
      if result.code ~= 0 then
        local why = vim.trim(result.stderr or "")
        vim.notify("tx send-user-message failed: " .. why, vim.log.levels.ERROR)
        return
      end
      on_sent()
    end)
  end)
end

-- The context line for a question: file:line plus the cursor word, or the visual
-- range plus the selected text.
local function context()
  local mode = vim.fn.mode()
  local file = vim.fn.expand("%:.")
  if mode:match("[vV\022]") then
    local from, to = vim.fn.getpos("v"), vim.fn.getpos(".")
    local lines = vim.fn.getregion(from, to, { type = mode })
    local first, last = math.min(from[2], to[2]), math.max(from[2], to[2])
    -- "nx", not "n": without the x the <Esc> is only QUEUED, so it arrives after
    -- vim.ui.input has opened and knocks the prompt out of insert mode -- the
    -- operator's first keystrokes then go nowhere. x flushes it here instead.
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "nx", false)
    return ("%s:%d-%d | %s"):format(file, first, last, table.concat(lines, "\\n")), first
  end
  -- expand("<cword>") RAISES E348 on a blank or whitespace-only line, which is most of the
  -- lines in a file -- there is no word to name, and file:line is context enough on its own.
  local found, word = pcall(vim.fn.expand, "<cword>")
  local line = vim.fn.line(".")
  local where = ("%s:%d"):format(file, line)
  if found and word ~= "" then where = where .. (" | word: %s"):format(word) end
  return where, line
end

-- The queue: questions banked with <leader>aC, flushed by <leader>ac.
local namespace = vim.api.nvim_create_namespace("tx_ask_queue")
local queue = {}

local function mark_queued(buffer, line, position)
  -- the line can be gone by now if the buffer was edited after queueing
  pcall(vim.api.nvim_buf_set_extmark, buffer, namespace, line - 1, 0, {
    virt_text = { { ("  ? Q%d queued"):format(position), "DiagnosticVirtualTextHint" } },
    virt_text_pos = "eol",
  })
end

local function clear_queue()
  queue = {}
  for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buffer) then
      vim.api.nvim_buf_clear_namespace(buffer, namespace, 0, -1)
    end
  end
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

-- <leader>aC : bank a question about this spot to send with the others later.
vim.keymap.set({ "n", "x" }, "<leader>aC", function()
  local where, line = context()
  local buffer = vim.api.nvim_get_current_buf()
  vim.ui.input({ prompt = ("Queue question (%d so far): "):format(#queue) }, function(question)
    -- Neither branch clears the queue. vim.ui.input passes nil on Esc and "" on an
    -- empty submit, and an earlier version read those as "empty means clear",
    -- which silently wiped a queue the operator had spent minutes building.
    if question == nil then return end
    question = vim.trim(question)
    if question == "" then return end
    queue[#queue + 1] = { question = question, context = where }
    mark_queued(buffer, line, #queue)
    vim.notify(("queued %d question%s"):format(#queue, #queue == 1 and "" or "s"))
  end)
end, { desc = "Queue a question for the tx target" })

-- <leader>ac : send -- the queued questions, plus whatever is typed now.
-- The prompt names the target on purpose -- no blind misfires.
vim.keymap.set({ "n", "x" }, "<leader>ac", function()
  local target = resolve_target()
  local where = context()
  local prompt = #queue > 0 and ("Ask %s (+%d queued, empty sends queue): "):format(target, #queue)
    or ("Ask %s: "):format(target)
  vim.ui.input({ prompt = prompt }, function(question)
    if question == nil then return end -- Esc cancels the send; the queue survives
    question = vim.trim(question)
    if question == "" and #queue == 0 then return end

    local questions = {}
    for index, item in ipairs(queue) do
      questions[index] = ("Q%d: %s | ctx: %s"):format(index, item.question, item.context)
    end
    if question ~= "" then
      questions[#questions + 1] = ("Q%d: %s | ctx: %s"):format(#questions + 1, question, where)
    end
    -- Messages are single-line (COMMON.md) -- any real newline becomes a literal \n.
    local body = table.concat(questions, "  ||  "):gsub("\n", "\\n")
    local count = #questions
    send(target, body, function()
      clear_queue()
      vim.notify(("sent %d question%s to %s"):format(count, count == 1 and "" or "s", target))
    end)
  end)
end, { desc = "Ask current tx target (sends queued questions too)" })
