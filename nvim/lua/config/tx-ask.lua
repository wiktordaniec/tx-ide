-- Ask a live tx llm session about the code under the cursor.
--
--   <leader>aC  queue a question here, to send with the rest later
--   <leader>ac  review queued questions, their source, and the target chat
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
local function read_record(id)
  local f = io.open(HOME .. "/.tx-ide/sessions/" .. id .. ".json")
  if not f then
    return nil
  end
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
      if rec then
        out[#out + 1] = rec
      end
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
        if vim.tbl_contains(my_tags, t) then
          shared = true
        end
      end
      list[#list + 1] = { name = r.name, tags = r.tags or {}, sibling = shared }
    end
  end
  table.sort(list, function(a, b)
    if a.sibling ~= b.sibling then
      return a.sibling
    end
    return a.name < b.name
  end)
  return list
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
  local buffer = vim.api.nvim_get_current_buf()
  local display_file = vim.fn.expand("%:.")
  local file = vim.fn.expand("%:p")
  if mode:match("[vV\022]") then
    local from, to = vim.fn.getpos("v"), vim.fn.getpos(".")
    local lines = vim.fn.getregion(from, to, { type = mode })
    local first, last = math.min(from[2], to[2]), math.max(from[2], to[2])
    -- "nx", not "n": without the x the <Esc> is only QUEUED, so it arrives after
    -- vim.ui.input has opened and knocks the prompt out of insert mode -- the
    -- operator's first keystrokes then go nowhere. x flushes it here instead.
    vim.api.nvim_feedkeys(vim.api.nvim_replace_termcodes("<Esc>", true, false, true), "nx", false)
    return {
      text = ("%s:%d-%d | %s"):format(display_file, first, last, table.concat(lines, "\\n")),
      display_file = display_file,
      buffer = buffer,
      file = file,
      line = first,
      end_line = last,
    }
  end
  -- expand("<cword>") RAISES E348 on a blank or whitespace-only line, which is most of the
  -- lines in a file -- there is no word to name, and file:line is context enough on its own.
  local found, word = pcall(vim.fn.expand, "<cword>")
  local line = vim.fn.line(".")
  local where = ("%s:%d"):format(display_file, line)
  if found and word ~= "" then
    where = where .. (" | word: %s"):format(word)
  end
  return { text = where, display_file = display_file, buffer = buffer, file = file, line = line }
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

local function clear_queue_marks()
  for _, buffer in ipairs(vim.api.nvim_list_bufs()) do
    if vim.api.nvim_buf_is_valid(buffer) then
      vim.api.nvim_buf_clear_namespace(buffer, namespace, 0, -1)
    end
  end
end

local function replace_queue(questions)
  clear_queue_marks()
  queue = questions
  for position, item in ipairs(queue) do
    mark_queued(item.context.buffer, item.context.line, position)
  end
end

local function wrap_question(question, width)
  local lines = {}
  local line = ""
  for word in question:gmatch("%S+") do
    local candidate = line == "" and word or line .. " " .. word
    if line ~= "" and vim.fn.strdisplaywidth(candidate) > width then
      lines[#lines + 1] = line
      line = word
    else
      line = candidate
    end
  end
  lines[#lines + 1] = line ~= "" and line or "Type a question in the prompt above."
  return lines
end

local function create_panel(title, filetype, options)
  options = options or {}
  return Snacks.win({
    show = false,
    enter = false,
    title = title,
    footer = options.footer,
    footer_pos = options.footer_pos,
    border = options.border or "rounded",
    focusable = options.focusable,
    bo = {
      buftype = "nofile",
      bufhidden = "wipe",
      swapfile = false,
      filetype = filetype,
    },
    wo = vim.tbl_extend("force", {
      cursorline = true,
      wrap = false,
      signcolumn = "no",
    }, options.window_options or {}),
  })
end

local active_layout

-- <leader>aC : bank a question about this spot to send with the others later.
vim.keymap.set({ "n", "x" }, "<leader>aC", function()
  local question_context = context()
  vim.ui.input({ prompt = ("Queue question (%d so far): "):format(#queue) }, function(question)
    -- Neither branch clears the queue. vim.ui.input passes nil on Esc and "" on an
    -- empty submit, and an earlier version read those as "empty means clear",
    -- which silently wiped a queue the operator had spent minutes building.
    if question == nil then
      return
    end
    question = vim.trim(question)
    if question == "" then
      return
    end
    queue[#queue + 1] = { question = question, context = question_context }
    mark_queued(question_context.buffer, question_context.line, #queue)
    vim.notify(("queued %d question%s"):format(#queue, #queue == 1 and "" or "s"))
  end)
end, { desc = "Queue a question for the tx target" })

-- <leader>ac : review a subset of queued questions and send them to one live chat.
vim.keymap.set({ "n", "x" }, "<leader>ac", function()
  if #queue == 0 then
    return vim.notify("no queued questions", vim.log.levels.WARN)
  end

  local target_list = targets()
  if #target_list == 0 then
    return vim.notify("no live llm sessions", vim.log.levels.WARN)
  end
  if active_layout and active_layout:valid() then
    active_layout:close()
  end

  local selected_questions = {}
  for position = 1, #queue do
    selected_questions[position] = true
  end

  local question_index = 1
  local chat_index = 1
  for position, target in ipairs(target_list) do
    if target.name == vim.g.tx_ask_target then
      chat_index = position
    end
  end

  local view_namespace = vim.api.nvim_create_namespace("tx_ask_view")
  local help_text = " Tab change item · Space select · Enter send · q close"

  local function send_questions(target, selected_items)
    local questions = {}
    local sent_queue_positions = {}
    for index, item in ipairs(selected_items) do
      questions[index] = ("Q%d: %s | ctx: %s"):format(index, item.question, item.context.text)
      sent_queue_positions[item.queue_position] = true
    end
    local body = table.concat(questions, "  ||  "):gsub("\n", "\\n")
    local count = #questions
    send(target, body, function()
      local remaining = {}
      for position, item in ipairs(queue) do
        if not sent_queue_positions[position] then
          remaining[#remaining + 1] = item
        end
      end
      replace_queue(remaining)
      vim.notify(("sent %d question%s to %s"):format(count, count == 1 and "" or "s", target))
    end)
  end

  local question_window = create_panel(" [1] Questions ", "markdown", {
    window_options = { wrap = true, linebreak = true },
  })
  local chat_window = create_panel(" [2] Chats ", "markdown")
  local preview_window = create_panel(" [3] Preview ", "", {
    footer = { { " " .. queue[1].context.display_file .. " ", "Comment" } },
    footer_pos = "center",
    window_options = { cursorline = false, number = true, relativenumber = false },
  })
  local help_window = create_panel("", "markdown", {
    border = "none",
    focusable = false,
    window_options = { cursorline = false, wrap = true, linebreak = true },
  })

  local layout
  layout = Snacks.layout.new({
    show = false,
    wins = {
      questions = question_window,
      chats = chat_window,
      preview = preview_window,
      help = help_window,
    },
    layout = {
      box = "vertical",
      width = 0.94,
      height = 0.84,
      border = "none",
      {
        box = "horizontal",
        {
          box = "vertical",
          width = 0.34,
          { win = "questions" },
          { win = "chats", height = 6 },
        },
        { win = "preview" },
      },
      {
        win = "help",
        height = function()
          return vim.fn.strdisplaywidth(help_text) + 2 <= math.floor(vim.o.columns * 0.94) and 1 or 2
        end,
      },
    },
    on_close = function()
      if active_layout == layout then
        active_layout = nil
      end
    end,
  })
  active_layout = layout
  layout:show()

  local function set_lines(window, lines)
    vim.bo[window.buf].modifiable = true
    vim.api.nvim_buf_set_lines(window.buf, 0, -1, false, lines)
    vim.bo[window.buf].modifiable = false
  end

  local function render_questions()
    local lines = {}
    for position, item in ipairs(queue) do
      lines[#lines + 1] = (" %s  %s"):format(selected_questions[position] and "●" or "○", item.question)
      lines[#lines + 1] = "      " .. item.context.display_file
      lines[#lines + 1] = ""
    end
    set_lines(question_window, lines)
    vim.api.nvim_buf_clear_namespace(question_window.buf, view_namespace, 0, -1)
    for position = 1, #queue do
      vim.api.nvim_buf_add_highlight(
        question_window.buf,
        view_namespace,
        selected_questions[position] and "DiagnosticOk" or "Comment",
        (position - 1) * 3,
        1,
        2
      )
      vim.api.nvim_buf_add_highlight(question_window.buf, view_namespace, "Comment", (position - 1) * 3 + 1, 0, -1)
    end
  end

  local function render_chats()
    local lines = {}
    for position, target in ipairs(target_list) do
      lines[position] = (" %s %s"):format(position == chat_index and "➜" or " ", target.name)
    end
    set_lines(chat_window, lines)
    vim.api.nvim_buf_clear_namespace(chat_window.buf, view_namespace, 0, -1)
    vim.api.nvim_buf_add_highlight(chat_window.buf, view_namespace, "DiagnosticInfo", chat_index - 1, 1, -1)
  end

  local function render_preview()
    local item = queue[question_index]
    local source_lines
    if vim.api.nvim_buf_is_valid(item.context.buffer) then
      source_lines = vim.api.nvim_buf_get_lines(item.context.buffer, 0, -1, false)
    elseif vim.fn.filereadable(item.context.file) == 1 then
      source_lines = vim.fn.readfile(item.context.file)
    else
      source_lines = { "Source is no longer available." }
    end

    vim.bo[preview_window.buf].modifiable = true
    vim.bo[preview_window.buf].filetype = vim.filetype.match({ filename = item.context.file }) or ""
    vim.api.nvim_buf_set_lines(preview_window.buf, 0, -1, false, source_lines)
    vim.bo[preview_window.buf].modifiable = false
    vim.api.nvim_buf_clear_namespace(preview_window.buf, view_namespace, 0, -1)

    local available_width = math.max(vim.api.nvim_win_get_width(preview_window.win) - 6, 20)
    local virtual_lines = {}
    for position, line in ipairs(wrap_question(item.question, available_width)) do
      virtual_lines[position] = {
        { position == 1 and "  ? " or "    ", "DiagnosticVirtualTextHint" },
        { line .. " ", "DiagnosticVirtualTextHint" },
      }
    end

    local preview_line = math.min(math.max(item.context.line, 1), vim.api.nvim_buf_line_count(preview_window.buf))
    vim.api.nvim_buf_set_extmark(preview_window.buf, view_namespace, preview_line - 1, 0, {
      virt_lines = virtual_lines,
      virt_lines_above = true,
      line_hl_group = "CursorLine",
      priority = 200,
    })
    preview_window.opts.footer = { { " " .. item.context.display_file .. " ", "Comment" } }
    preview_window:update()
    vim.api.nvim_win_set_cursor(preview_window.win, { preview_line, 0 })
    vim.api.nvim_win_call(preview_window.win, function()
      vim.cmd("normal! zt")
    end)
  end

  local panel_windows = { question_window, chat_window, preview_window }
  local function map_all(keys, callback, description)
    for _, window in ipairs(panel_windows) do
      vim.keymap.set("n", keys, callback, { buffer = window.buf, nowait = true, desc = description })
    end
  end

  map_all("1", function()
    question_window:focus()
  end, "Questions panel")
  map_all("2", function()
    chat_window:focus()
  end, "Chats panel")
  map_all("3", function()
    preview_window:focus()
  end, "Preview panel")
  map_all("q", function()
    layout:close()
  end, "Close question review")
  map_all("<Esc>", function()
    layout:close()
  end, "Close question review")

  local function move_question(direction)
    question_index = (question_index - 1 + direction) % #queue + 1
    vim.api.nvim_win_set_cursor(question_window.win, { (question_index - 1) * 3 + 1, 0 })
    render_preview()
  end
  for _, window in ipairs({ question_window, preview_window }) do
    vim.keymap.set("n", "<Tab>", function()
      move_question(1)
    end, {
      buffer = window.buf,
      nowait = true,
      desc = "Next question",
    })
    vim.keymap.set("n", "<S-Tab>", function()
      move_question(-1)
    end, {
      buffer = window.buf,
      nowait = true,
      desc = "Previous question",
    })
  end
  vim.keymap.set("n", "j", function()
    move_question(1)
  end, { buffer = question_window.buf, nowait = true })
  vim.keymap.set("n", "k", function()
    move_question(-1)
  end, { buffer = question_window.buf, nowait = true })
  vim.keymap.set("n", "<Space>", function()
    selected_questions[question_index] = not selected_questions[question_index]
    render_questions()
    render_preview()
  end, { buffer = question_window.buf, nowait = true, desc = "Toggle question" })

  local function move_chat(direction)
    chat_index = (chat_index - 1 + direction) % #target_list + 1
    render_chats()
    vim.api.nvim_win_set_cursor(chat_window.win, { chat_index, 0 })
  end
  vim.keymap.set("n", "<Tab>", function()
    move_chat(1)
  end, {
    buffer = chat_window.buf,
    nowait = true,
    desc = "Next chat",
  })
  vim.keymap.set("n", "<S-Tab>", function()
    move_chat(-1)
  end, {
    buffer = chat_window.buf,
    nowait = true,
    desc = "Previous chat",
  })
  vim.keymap.set("n", "j", function()
    move_chat(1)
  end, { buffer = chat_window.buf, nowait = true })
  vim.keymap.set("n", "k", function()
    move_chat(-1)
  end, { buffer = chat_window.buf, nowait = true })

  local function send_selected_questions()
    local selected_items = {}
    for position, item in ipairs(queue) do
      if selected_questions[position] then
        selected_items[#selected_items + 1] = {
          question = item.question,
          context = item.context,
          queue_position = position,
        }
      end
    end
    if #selected_items == 0 then
      return vim.notify("select at least one question", vim.log.levels.WARN)
    end

    local target = target_list[chat_index].name
    vim.g.tx_ask_target = target
    layout:close()
    send_questions(target, selected_items)
  end
  vim.keymap.set("n", "<CR>", send_selected_questions, {
    buffer = question_window.buf,
    nowait = true,
    desc = "Send selected questions",
  })
  vim.keymap.set("n", "<CR>", send_selected_questions, {
    buffer = chat_window.buf,
    nowait = true,
    desc = "Send selected questions",
  })

  render_questions()
  render_chats()
  set_lines(help_window, { help_text })
  render_preview()
  question_window:focus()
  vim.api.nvim_win_set_cursor(question_window.win, { 1, 0 })
end, { desc = "Review and send queued tx questions" })
